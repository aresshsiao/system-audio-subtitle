"""PROCESS 2 進入點：inference-service。見 ARCHITECTURE.md §3、§8、§9、§11、§13.3、§15。

訂閱 audio-service 的 Utterance 事件，從共享環形緩衝讀音訊，跑
ASR→語言包路由→翻譯（`inference/pipeline.py`），發布 `Subtitle`
（含譯文，UI 顯示用）。同時開兩個 REQ/REP 控制通道：語言包即時切換
（見 §13.4）、雲端精修開關/狀態。

M5 加入的三件事（都不改變 M3 的字幕主路徑）：
  - **降級階梯**（`inference/degrade.py`）：暫定稿間隔跟不上就逐級降品質，
    不排隊、不無限累積延遲
  - **雲端精修**（`inference/translate/llm_api.py`）：預設關閉；背景執行緒，
    結果以 POLISHED 靜默替換，雲端壞掉只是沒有潤飾
  - **可觀測性**（`utils/metrics.py`）：每秒發布 `MetricsSnapshot`

語言強制指定：只啟用一個語言包 = 鎖定該語言（見 §13.4 單包鎖定模式）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from audio.ringbuffer import RingBufferReader
from contracts.enums import EngineKind, SubtitleState
from contracts.messages import (
    AudioSpan,
    CloudPolishStatus,
    MetricsSnapshot,
    SetActiveLangPacks,
    SetActiveLangPacksAck,
    SetCloudPolish,
    Subtitle,
    Utterance,
)
from contracts.topics import (
    AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    AUDIO_RING_BUFFER_NAME,
    AUDIO_UTTERANCE,
    BUS_ENDPOINTS,
    CONTROL_CLOUD_POLISH,
    CONTROL_LANGPACK_RELOAD,
    INFERENCE_METRICS,
    INFERENCE_SUBTITLE,
)
from inference.degrade import DegradeController, DegradeParams
from inference.langpack import LangPackRegistry
from inference.pipeline import DecodeResult, Pipeline, TranslatorRegistry
from inference.stabilizer import Stabilizer
from inference.translate.context import TranslationContext
from inference.translate.llm_api import (
    LlmClient,
    LlmConfig,
    PolishItem,
    PolishResult,
    PolishWorker,
)
from runtime.bus import Publisher, Replier, Subscriber
from utils.logging import setup_logging
from utils.metrics import Metrics

logger = logging.getLogger(__name__)

_RING_BUFFER_ATTACH_RETRY_S = 0.5
_RING_BUFFER_ATTACH_TIMEOUT_S = 30.0
_METRICS_PUBLISH_INTERVAL_S = 1.0
# L5（丟棄最舊未處理段落）：一句話說完之後過了這麼久才輪到處理，就放棄它——
# 對「即時字幕」而言，晚 8 秒才出現的字幕已經沒有意義，繼續處理它只會讓
# 後面所有句子一起變慢。
STALE_LAG_S = 8.0
_SAMPLE_RATE = 16000
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
    last_draft_started_at: float | None = None


@dataclass
class _Runtime:
    """主迴圈用到的所有協作物件。用一個物件傳，而不是十幾個參數。"""

    ring_reader: RingBufferReader
    pipeline: Pipeline
    publisher: Publisher
    context: TranslationContext
    metrics: Metrics
    degrade: DegradeController
    polish: PolishWorker
    engines: "_EngineSwitcher"
    llm_config: LlmConfig | None


class _EngineSwitcher:
    """降級階梯 L4：主模型 ↔ 備援模型。備援模型第一次需要時才載入（且只在
    已快取的情況下才會走到這裡，見 `DegradeController.l4_available`）。"""

    def __init__(self, primary, fallback_factory) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback = None

    def select(self, *, use_fallback: bool):
        if not use_fallback:
            return self._primary
        if self._fallback is None:
            logger.warning("降級階梯 L4：載入備援模型（會短暫停頓）...")
            self._fallback = self._fallback_factory()
        return self._fallback


def apply_degrade_params(rt: _Runtime, params: DegradeParams) -> None:
    """把降級階梯決定的參數套到實際執行的東西上。"""
    rt.pipeline.final_beam_size = params.final_beam_size
    rt.polish.allowed_by_degrade = params.cloud_polish_allowed
    rt.pipeline.set_asr_engine(rt.engines.select(use_fallback=params.use_fallback_model))
    rt.metrics.set_gauge("draft_interval_target_s", params.draft_interval_s)


def _on_level_change(rt: _Runtime, new_level) -> None:
    if new_level is not None:
        apply_degrade_params(rt, rt.degrade.params)
        rt.metrics.incr("degrade_transitions_total")


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

    from inference.asr.faster_whisper_engine import FasterWhisperEngine, is_model_cached

    logger.info("載入 ASR 引擎 (faster-whisper large-v3, cuda, int8_float16)...")
    asr_engine = FasterWhisperEngine()
    logger.info("ASR 引擎載入完成")

    fallback_model = os.environ.get("SAS_DEGRADE_MODEL", "large-v3-turbo")
    l4_available = is_model_cached(fallback_model)
    logger.info(
        "降級階梯 L4 備援模型 %s：%s",
        fallback_model,
        "已快取，可用" if l4_available else "未快取，L4 停用（跑 scripts/setup_models.py --degrade-model 預先下載）",
    )

    metrics = Metrics()
    llm_config = LlmConfig.from_env()
    polish = PolishWorker(LlmClient(llm_config) if llm_config else None, metrics)
    polish.start()
    if llm_config is None:
        logger.info("雲端精修未設定（SAS_LLM_BASE_URL / SAS_LLM_MODEL），維持純本地")

    ring_reader = _attach_ring_buffer_with_retry()
    logger.info("ring buffer 已連接")

    rt = _Runtime(
        ring_reader=ring_reader,
        pipeline=Pipeline(asr_engine, registry, _build_translator_registry(), metrics),
        publisher=Publisher(BUS_ENDPOINTS[INFERENCE_SUBTITLE]),
        context=TranslationContext(max_sentences=5),
        metrics=metrics,
        degrade=DegradeController(l4_available=l4_available),
        polish=polish,
        engines=_EngineSwitcher(asr_engine, lambda: FasterWhisperEngine(fallback_model)),
        llm_config=llm_config,
    )
    apply_degrade_params(rt, rt.degrade.params)

    subscriber = Subscriber(BUS_ENDPOINTS[AUDIO_UTTERANCE], topics=[AUDIO_UTTERANCE])
    langpack_control = Replier(BUS_ENDPOINTS[CONTROL_LANGPACK_RELOAD])
    polish_control = Replier(BUS_ENDPOINTS[CONTROL_CLOUD_POLISH])

    # 只啟用剛好一個語言包時，視為「單包鎖定模式」（見 §13.4）：強制 ASR
    # 用該語言解碼，不再依賴自動偵測——自動偵測在只有一種可能語言時
    # 反而偶爾會誤判，鎖定可以避免這個問題，且使用者明確選了一個包，
    # 就是在表達「我知道現在是這個語言」的意圖。
    def _forced_language() -> str | None:
        enabled = registry.enabled_packs
        return enabled[0].src_lang if len(enabled) == 1 else None

    active: _ActiveUtterance | None = None
    last_metrics_at = 0.0

    try:
        while True:
            control_req = langpack_control.poll_request(SetActiveLangPacks, timeout_ms=0)
            if control_req is not None:
                langpack_control.reply(handle_langpack_request(control_req, registry, rt.pipeline))

            polish_req = polish_control.poll_request(SetCloudPolish, timeout_ms=0)
            if polish_req is not None:
                polish_control.reply(handle_cloud_polish_request(polish_req, rt))

            for polished in rt.polish.poll_results():
                _publish_polished(rt, polished)

            result = subscriber.recv(timeout_ms=50)
            if result is not None:
                _topic, payload = result
                active = _handle_utterance_event(
                    Utterance.decode(payload), active, rt, forced_language=_forced_language()
                )

            if active is not None:
                now = time.monotonic()
                interval = rt.degrade.params.draft_interval_s
                if active.last_draft_started_at is None or now - active.last_draft_started_at >= interval:
                    _run_draft_pass(active, rt, forced_language=_forced_language())

            now = time.monotonic()
            if now - last_metrics_at >= _METRICS_PUBLISH_INTERVAL_S:
                last_metrics_at = now
                _publish_metrics(rt)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("shutting down")
        polish.stop()
        subscriber.close()
        rt.publisher.close()
        langpack_control.close()
        polish_control.close()
        ring_reader.close()


def handle_langpack_request(
    req: SetActiveLangPacks, registry: LangPackRegistry, pipeline: Pipeline | None = None
) -> SetActiveLangPacksAck:
    """語言包控制請求：`reload` 先重掃目錄（剛匯入的新包才看得到），`pack_ids`
    為 None 只查詢目前啟用集合。"""
    try:
        if req.reload:
            registry.reload()
            if pipeline is not None:
                pipeline.clear_caches()
        if req.pack_ids is not None:
            registry.set_enabled(req.pack_ids)
            logger.info("語言包啟用集合已更新: %s", req.pack_ids)
        return SetActiveLangPacksAck(
            success=True, active_pack_ids=[p.id for p in registry.enabled_packs], error=None
        )
    except KeyError as e:
        logger.warning("SetActiveLangPacks 失敗: %s", e)
        return SetActiveLangPacksAck(
            success=False, active_pack_ids=[p.id for p in registry.enabled_packs], error=str(e)
        )


def cloud_polish_status(rt: _Runtime, *, error: str | None = None) -> CloudPolishStatus:
    return CloudPolishStatus(
        success=error is None,
        enabled=rt.polish.enabled,
        configured=rt.polish.configured,
        endpoint_host=rt.llm_config.host if rt.llm_config else None,
        breaker_open=rt.polish.breaker_open,
        blocked_by_degrade=not rt.polish.allowed_by_degrade,
        error=error,
    )


def handle_cloud_polish_request(req: SetCloudPolish, rt: _Runtime) -> CloudPolishStatus:
    if req.enabled is None:
        return cloud_polish_status(rt)
    if req.enabled and not rt.polish.configured:
        return cloud_polish_status(
            rt,
            error="雲端精修尚未設定：請在啟動 inference-service 前設定環境變數 "
            "SAS_LLM_BASE_URL、SAS_LLM_MODEL（與 SAS_LLM_API_KEY）",
        )
    rt.polish.enabled = req.enabled
    if req.enabled:
        logger.warning("雲端精修已開啟：原文將送往第三方 %s", rt.llm_config.host)
    else:
        logger.info("雲端精修已關閉")
    return cloud_polish_status(rt)


def _publish_metrics(rt: _Runtime) -> None:
    rt.metrics.set_gauge("degrade_level", float(rt.degrade.level))
    rt.metrics.set_gauge("degrade_transitions_up", float(rt.degrade.transitions_up))
    rt.metrics.set_gauge("degrade_transitions_down", float(rt.degrade.transitions_down))
    rt.metrics.set_gauge("polish_enabled", 1.0 if rt.polish.enabled else 0.0)
    rt.metrics.set_gauge("polish_circuit_open", 1.0 if rt.polish.breaker_open else 0.0)
    rt.metrics.set_gauge(
        "ring_overrun_samples", float(rt.ring_reader.total_overrun_samples)
    )
    snap = rt.metrics.snapshot()
    rt.publisher.publish(
        INFERENCE_METRICS,
        MetricsSnapshot(
            degrade_level=int(rt.degrade.level),
            histograms=snap["histograms"],
            counters=snap["counters"],
            gauges=snap["gauges"],
        ),
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


def _publish_polished(rt: _Runtime, result: PolishResult) -> None:
    item = result.item
    subtitle = Subtitle(
        utt_id=item.utt_id,
        revision=item.revision + 1,
        state=SubtitleState.POLISHED,
        span=item.span,
        src_lang=item.src_lang,
        tgt_lang=item.tgt_lang,
        pack_id=item.pack_id,
        source_text=item.source_text,
        target_text=result.polished_text,
        engine=EngineKind.TRANSLATE_LLM_API,
    )
    rt.publisher.publish(INFERENCE_SUBTITLE, subtitle)
    logger.info("utt=%s [POLISHED] %s -> %s", item.utt_id[:8], item.target_text, result.polished_text)


def _handle_utterance_event(
    utt: Utterance,
    active: _ActiveUtterance | None,
    rt: _Runtime,
    *,
    forced_language: str | None,
) -> _ActiveUtterance | None:
    if not utt.closed:
        logger.info("utt=%s OPEN", utt.utt_id[:8])
        # 這時候還沒解碼過、不知道是哪個語言包，粒度先用預設值 "word"，
        # 第一次解碼完會在 _run_draft_pass 裡動態校正（見 Pipeline.
        # granularity_for 的說明）。
        return _ActiveUtterance(
            utt_id=utt.utt_id, start_sample=utt.span.start_sample, stabilizer=Stabilizer()
        )

    lag_s = (rt.ring_reader.write_total - utt.span.end_sample) / _SAMPLE_RATE
    rt.metrics.observe("final_queue_lag_ms", lag_s * 1000.0)
    _on_level_change(rt, rt.degrade.observe_final_lag(lag_s))
    remaining = None if (active is None or active.utt_id == utt.utt_id) else active

    if rt.degrade.params.drop_stale and lag_s > STALE_LAG_S:
        # L5：這句話已經太舊了，丟掉，把算力留給還來得及的句子。
        rt.metrics.incr("dropped_utterances_total")
        logger.warning("utt=%s CLOSE（L5 丟棄：已落後 %.1fs）", utt.utt_id[:8], lag_s)
        return remaining

    audio = rt.ring_reader.peek(utt.span.start_sample, utt.span.end_sample)
    decoded = rt.pipeline.process_final(
        audio, forced_language=forced_language, context=rt.context
    )
    if decoded is not None:
        revision = active.revision + 1 if active is not None and active.utt_id == utt.utt_id else 0
        subtitle = _make_subtitle(utt.utt_id, revision, SubtitleState.FINAL, utt.span, decoded)
        rt.publisher.publish(INFERENCE_SUBTITLE, subtitle)
        logger.info(
            "utt=%s CLOSE [FINAL] %s -> %s", utt.utt_id[:8], decoded.source_text, subtitle.target_text
        )
        _maybe_submit_polish(rt, utt, revision, decoded)
    else:
        logger.info("utt=%s CLOSE (濾掉：疑似幻覺或空音訊)", utt.utt_id[:8])

    return remaining


def _maybe_submit_polish(
    rt: _Runtime, utt: Utterance, revision: int, decoded: DecodeResult
) -> None:
    if decoded.target_text is None or decoded.tgt_lang is None:
        return
    terms = rt.pipeline.cloud_polish_terms(decoded.pack_id)
    if terms is None:
        return
    rt.polish.submit(
        PolishItem(
            utt_id=utt.utt_id,
            revision=revision,
            span=utt.span,
            pack_id=decoded.pack_id or "",
            src_lang=decoded.src_lang,
            tgt_lang=decoded.tgt_lang,
            source_text=decoded.source_text,
            target_text=decoded.target_text,
            glossary=terms,
        )
    )


def _run_draft_pass(
    active: _ActiveUtterance, rt: _Runtime, *, forced_language: str | None
) -> None:
    started = time.monotonic()
    interval_s = (
        started - active.last_draft_started_at if active.last_draft_started_at is not None else None
    )
    active.last_draft_started_at = started

    audio = rt.ring_reader.peek(
        active.start_sample, active.start_sample + AUDIO_RING_BUFFER_CAPACITY_SAMPLES
    )
    if len(audio) == 0:
        return
    end_sample = active.start_sample + len(audio)

    decoded = rt.pipeline.process_draft(
        audio, forced_language=forced_language, stabilizer=active.stabilizer
    )
    decode_s = time.monotonic() - started

    if interval_s is not None:
        rt.metrics.observe("draft_interval_ms", interval_s * 1000.0)
        _on_level_change(rt, rt.degrade.observe_draft(interval_s=interval_s, decode_s=decode_s))

    if decoded is None:
        return

    # 這次解碼已經用了呼叫前的粒度；這裡校正是為了「下一次」呼叫，
    # 見 Pipeline.granularity_for 的說明。
    active.stabilizer.granularity = rt.pipeline.granularity_for(decoded.pack_id)

    active.revision += 1
    span = AudioSpan(active.start_sample, end_sample)
    subtitle = _make_subtitle(active.utt_id, active.revision, SubtitleState.DRAFT, span, decoded)
    rt.publisher.publish(INFERENCE_SUBTITLE, subtitle)
    logger.info(
        "utt=%s DRAFT (stable=%d) %s -> %s",
        active.utt_id[:8],
        decoded.stable_chars,
        decoded.source_text,
        subtitle.target_text,
    )


if __name__ == "__main__":
    main()
