"""PROCESS 2 進入點：inference-service。見 ARCHITECTURE.md §3、§8、§9、§13.3。

訂閱 audio-service 的 Utterance 事件，從共享環形緩衝讀音訊，跑
ASR→語言包路由→翻譯（`inference/pipeline.py`），發布 `Subtitle`
（含譯文，UI 顯示用）。同時開一個 REQ/REP 控制通道，讓 UI 可以即時
切換啟用哪些語言包，不需要重啟這支進程（見 §13.4）。

M3 現況：
  - 語言強制指定改成「只啟用一個語言包 = 鎖定該語言」（見 §13.4 單包
    鎖定模式），不再用環境變數指定語言——語言包本身就宣告了 src_lang。
  - 雲端精修（llm-api）還沒實作，M5 才會補上；語言包裡有些寫
    `cloud_engine: llm-api` 的欄位目前完全沒用到。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np

from audio.ringbuffer import RingBufferReader
from contracts.enums import EngineKind, SubtitleState
from contracts.messages import AudioSpan, SetActiveLangPacks, SetActiveLangPacksAck, Subtitle, Utterance
from contracts.topics import (
    AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    AUDIO_RING_BUFFER_NAME,
    AUDIO_UTTERANCE,
    BUS_ENDPOINTS,
    CONTROL_LANGPACK_RELOAD,
    INFERENCE_SUBTITLE,
)
from inference.asr.faster_whisper_engine import FasterWhisperEngine
from inference.langpack import LangPackRegistry
from inference.pipeline import DecodeResult, Pipeline, TranslatorRegistry
from inference.stabilizer import Stabilizer
from inference.translate.context import TranslationContext
from runtime.bus import Publisher, Replier, Subscriber
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

DRAFT_INTERVAL_S = 0.25  # 見 ARCHITECTURE.md §8：暫定稿每 250ms 滑動視窗
_RING_BUFFER_ATTACH_RETRY_S = 0.5
_RING_BUFFER_ATTACH_TIMEOUT_S = 30.0
_ENGINE_NAME_TO_KIND = {
    "ct2-nllb": EngineKind.TRANSLATE_CT2_NLLB,
    "llm-api": EngineKind.TRANSLATE_LLM_API,
}


@dataclass
class _ActiveUtterance:
    utt_id: str
    start_sample: int
    stabilizer: Stabilizer
    revision: int = 0
    last_draft_at: float = field(default_factory=lambda: 0.0)


def _attach_ring_buffer_with_retry() -> RingBufferReader:
    deadline = time.monotonic() + _RING_BUFFER_ATTACH_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return RingBufferReader(AUDIO_RING_BUFFER_NAME, AUDIO_RING_BUFFER_CAPACITY_SAMPLES)
        except FileNotFoundError as e:
            last_error = e
            logger.info("等待 audio-service 建立 ring buffer...")
            time.sleep(_RING_BUFFER_ATTACH_RETRY_S)
    raise TimeoutError(
        f"{_RING_BUFFER_ATTACH_TIMEOUT_S}s 內沒等到 audio-service 建立 ring buffer"
    ) from last_error


def _build_translator_registry() -> TranslatorRegistry:
    """`ct2-nllb` 引擎需要轉換好的模型檔（`scripts/setup_models.py`）才能
    載入。模型不存在時記警告、不中止整支進程——語言包還是能運作，只是
    `pipeline.py` 找不到對應 Translator 時會原樣顯示原文（見 pipeline.py
    的 `_translate_with_glossary`），不翻譯不代表整個系統不能用。
    """
    translators: dict = {}
    try:
        from inference.translate.ct2_nllb import Ct2NllbTranslator

        translators["ct2-nllb"] = Ct2NllbTranslator()
        logger.info("NLLB 翻譯引擎載入完成")
    except FileNotFoundError as e:
        logger.warning("NLLB 模型未就緒，翻譯功能停用，只會顯示原文: %s", e)
    return TranslatorRegistry(translators)


def _initial_enabled_pack_ids(registry: LangPackRegistry) -> list[str]:
    """啟動時預設啟用哪些語言包。`SAS_ENABLED_LANGPACKS` 是逗號分隔的
    pack id 清單，留空/不設代表全部啟用（見 §13.4「多包並存自動路由」）。
    之後不管有沒有設，都可以透過控制通道（SetActiveLangPacks）即時切換。
    """
    env_value = os.environ.get("SAS_ENABLED_LANGPACKS")
    all_ids = [p.id for p in registry.all_packs()]
    if not env_value:
        return all_ids
    requested = [pid.strip() for pid in env_value.split(",") if pid.strip()]
    unknown = [pid for pid in requested if pid not in all_ids]
    if unknown:
        logger.warning("SAS_ENABLED_LANGPACKS 有未知的 pack id，已忽略: %s", unknown)
    return [pid for pid in requested if pid in all_ids]


def main() -> None:
    setup_logging("inference-service")
    logger.info("starting")

    registry = LangPackRegistry()
    registry.reload()
    registry.set_enabled(_initial_enabled_pack_ids(registry))
    logger.info("已啟用語言包: %s", [p.id for p in registry.enabled_packs])

    logger.info("載入 ASR 引擎 (faster-whisper large-v3, cuda, int8_float16)...")
    asr_engine = FasterWhisperEngine()
    logger.info("ASR 引擎載入完成")

    translator_registry = _build_translator_registry()
    pipeline = Pipeline(asr_engine, registry, translator_registry)

    ring_reader = _attach_ring_buffer_with_retry()
    logger.info("ring buffer 已連接")

    subscriber = Subscriber(BUS_ENDPOINTS[AUDIO_UTTERANCE], topics=[AUDIO_UTTERANCE])
    publisher = Publisher(BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    control = Replier(BUS_ENDPOINTS[CONTROL_LANGPACK_RELOAD])

    # 只啟用剛好一個語言包時，視為「單包鎖定模式」（見 §13.4）：強制 ASR
    # 用該語言解碼，不再依賴自動偵測——自動偵測在只有一種可能語言時
    # 反而偶爾會誤判，鎖定可以避免這個問題，且使用者明確選了一個包，
    # 就是在表達「我知道現在是這個語言」的意圖。
    def _forced_language() -> str | None:
        enabled = registry.enabled_packs
        return enabled[0].src_lang if len(enabled) == 1 else None

    active: _ActiveUtterance | None = None
    context = TranslationContext(max_sentences=5)

    try:
        while True:
            control_req = control.poll_request(SetActiveLangPacks, timeout_ms=0)
            if control_req is not None:
                _handle_control_request(control_req, registry, control)

            result = subscriber.recv(timeout_ms=50)
            if result is not None:
                _topic, payload = result
                utt = Utterance.decode(payload)
                active = _handle_utterance_event(
                    utt,
                    active,
                    ring_reader=ring_reader,
                    pipeline=pipeline,
                    forced_language=_forced_language(),
                    context=context,
                    publisher=publisher,
                )

            if active is not None:
                now = time.monotonic()
                if now - active.last_draft_at >= DRAFT_INTERVAL_S:
                    active.last_draft_at = now
                    _run_draft_pass(
                        active,
                        ring_reader=ring_reader,
                        pipeline=pipeline,
                        forced_language=_forced_language(),
                        publisher=publisher,
                    )
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("shutting down")
        subscriber.close()
        publisher.close()
        control.close()
        ring_reader.close()


def _handle_control_request(
    req: SetActiveLangPacks, registry: LangPackRegistry, control: Replier
) -> None:
    try:
        registry.set_enabled(req.pack_ids)
        logger.info("語言包啟用集合已更新: %s", req.pack_ids)
        control.reply(
            SetActiveLangPacksAck(success=True, active_pack_ids=req.pack_ids, error=None)
        )
    except KeyError as e:
        logger.warning("SetActiveLangPacks 失敗: %s", e)
        control.reply(
            SetActiveLangPacksAck(
                success=False,
                active_pack_ids=[p.id for p in registry.enabled_packs],
                error=str(e),
            )
        )


def _make_subtitle(
    utt_id: str, revision: int, state: SubtitleState, span: AudioSpan, decoded: DecodeResult
) -> Subtitle:
    target_text = decoded.target_text if decoded.target_text is not None else decoded.source_text
    engine = _ENGINE_NAME_TO_KIND.get(decoded.engine_name or "", EngineKind.NONE)
    return Subtitle(
        utt_id=utt_id,
        revision=revision,
        state=state,
        span=span,
        src_lang=decoded.src_lang,
        tgt_lang=decoded.tgt_lang or decoded.src_lang,
        pack_id=decoded.pack_id or "",
        source_text=decoded.source_text,
        target_text=target_text,
        engine=engine,
    )


def _handle_utterance_event(
    utt: Utterance,
    active: _ActiveUtterance | None,
    *,
    ring_reader: RingBufferReader,
    pipeline: Pipeline,
    forced_language: str | None,
    context: TranslationContext,
    publisher: Publisher,
) -> _ActiveUtterance | None:
    if not utt.closed:
        logger.info("utt=%s OPEN", utt.utt_id[:8])
        # 這時候還沒解碼過、不知道是哪個語言包，粒度先用預設值 "word"，
        # 第一次解碼完會在 _run_draft_pass 裡動態校正（見 Pipeline.
        # granularity_for 的說明）。
        return _ActiveUtterance(
            utt_id=utt.utt_id,
            start_sample=utt.span.start_sample,
            stabilizer=Stabilizer(),
        )

    audio = ring_reader.peek(utt.span.start_sample, utt.span.end_sample)
    decoded = pipeline.process_final(audio, forced_language=forced_language, context=context)
    if decoded is not None:
        revision = active.revision + 1 if active is not None and active.utt_id == utt.utt_id else 0
        subtitle = _make_subtitle(utt.utt_id, revision, SubtitleState.FINAL, utt.span, decoded)
        publisher.publish(INFERENCE_SUBTITLE, subtitle)
        logger.info(
            "utt=%s CLOSE [FINAL] %s -> %s", utt.utt_id[:8], decoded.source_text, subtitle.target_text
        )
    else:
        logger.info("utt=%s CLOSE (濾掉：疑似幻覺或空音訊)", utt.utt_id[:8])

    return None if (active is None or active.utt_id == utt.utt_id) else active


def _run_draft_pass(
    active: _ActiveUtterance,
    *,
    ring_reader: RingBufferReader,
    pipeline: Pipeline,
    forced_language: str | None,
    publisher: Publisher,
) -> None:
    audio = ring_reader.peek(
        active.start_sample, active.start_sample + AUDIO_RING_BUFFER_CAPACITY_SAMPLES
    )
    if len(audio) == 0:
        return
    end_sample = active.start_sample + len(audio)

    decoded = pipeline.process_draft(
        audio, forced_language=forced_language, stabilizer=active.stabilizer
    )
    if decoded is None:
        return

    # 這次解碼已經用了呼叫前的粒度；這裡校正是為了「下一次」呼叫，
    # 見 Pipeline.granularity_for 的說明。
    active.stabilizer.granularity = pipeline.granularity_for(decoded.pack_id)

    active.revision += 1
    span = AudioSpan(active.start_sample, end_sample)
    subtitle = _make_subtitle(active.utt_id, active.revision, SubtitleState.DRAFT, span, decoded)
    publisher.publish(INFERENCE_SUBTITLE, subtitle)
    logger.info(
        "utt=%s DRAFT (stable=%d) %s -> %s",
        active.utt_id[:8],
        decoded.stable_chars,
        decoded.source_text,
        subtitle.target_text,
    )


if __name__ == "__main__":
    main()
