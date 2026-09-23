"""PROCESS 2 進入點：inference-service。見 ARCHITECTURE.md §3、§8、§9。

訂閱 audio-service 的 Utterance 事件，從共享環形緩衝讀音訊，跑 ASR，
用 LocalAgreement-2 穩定化暫定稿，過濾幻覺，發布 Transcript。

M1 現況：
  - 還沒有語言包路由（M3），語言用環境變數 `SAS_ASR_LANGUAGE` 指定，
    預設 None（Whisper 自動偵測）。
  - 還沒有翻譯（M3），Transcript 就是最終產出，還沒有 Subtitle。
  - 還沒有降級階梯（M5），跟不上時單純讓暫定稿變慢，不會主動降級。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np

from audio.ringbuffer import RingBufferReader
from contracts.enums import SubtitleState
from contracts.messages import AudioSpan, Transcript, Utterance
from contracts.topics import (
    AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    AUDIO_RING_BUFFER_NAME,
    AUDIO_UTTERANCE,
    BUS_ENDPOINTS,
    INFERENCE_TRANSCRIPT,
)
from inference.asr.faster_whisper_engine import FasterWhisperEngine
from inference.asr.hallucination import is_hallucination, load_blacklist
from inference.stabilizer import Stabilizer
from runtime.bus import Publisher, Subscriber
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

DRAFT_INTERVAL_S = 0.25  # 見 ARCHITECTURE.md §8：暫定稿每 250ms 滑動視窗
DRAFT_BEAM_SIZE = 1
FINAL_BEAM_SIZE = 5

_RING_BUFFER_ATTACH_RETRY_S = 0.5
_RING_BUFFER_ATTACH_TIMEOUT_S = 30.0


@dataclass
class _ActiveUtterance:
    utt_id: str
    start_sample: int
    stabilizer: Stabilizer
    revision: int = 0
    last_draft_at: float = field(default_factory=lambda: 0.0)


def _attach_ring_buffer_with_retry() -> RingBufferReader:
    """audio-service 要先建立共享記憶體區塊，inference-service 才 attach 得上——
    兩個進程各自獨立啟動，誰先誰後不保證，這裡用重試等待，而不是直接失敗。
    """
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


def _decode(
    engine: FasterWhisperEngine,
    audio: np.ndarray,
    *,
    language: str | None,
    beam_size: int,
    blacklist: frozenset[str],
) -> tuple[str, str, float] | None:
    """回傳 (text, language, no_speech_prob)；幻覺或空音訊回傳 None。"""
    if len(audio) == 0:
        return None
    result = engine.transcribe(audio, language=language, beam_size=beam_size)
    if is_hallucination(result.text, result.no_speech_prob, blacklist=blacklist):
        return None
    return result.text, result.language, result.no_speech_prob


def main() -> None:
    setup_logging("inference-service")
    logger.info("starting")

    language = os.environ.get("SAS_ASR_LANGUAGE") or None
    blacklist_path = os.environ.get("SAS_HALLUCINATION_BLACKLIST")
    blacklist = load_blacklist(blacklist_path)
    stabilizer_granularity = os.environ.get("SAS_STABILIZER_GRANULARITY", "word")
    if stabilizer_granularity not in ("word", "char"):
        raise ValueError(f"SAS_STABILIZER_GRANULARITY 必須是 word 或 char: {stabilizer_granularity}")

    logger.info("載入 ASR 引擎 (faster-whisper large-v3, cuda, int8_float16)...")
    engine = FasterWhisperEngine()
    logger.info("ASR 引擎載入完成")

    ring_reader = _attach_ring_buffer_with_retry()
    logger.info("ring buffer 已連接")

    subscriber = Subscriber(BUS_ENDPOINTS[AUDIO_UTTERANCE], topics=[AUDIO_UTTERANCE])
    publisher = Publisher(BUS_ENDPOINTS[INFERENCE_TRANSCRIPT])

    active: _ActiveUtterance | None = None

    try:
        while True:
            result = subscriber.recv(timeout_ms=50)
            if result is not None:
                _topic, payload = result
                utt = Utterance.decode(payload)
                active = _handle_utterance_event(
                    utt,
                    active,
                    ring_reader=ring_reader,
                    engine=engine,
                    language=language,
                    blacklist=blacklist,
                    stabilizer_granularity=stabilizer_granularity,
                    publisher=publisher,
                )

            if active is not None:
                now = time.monotonic()
                if now - active.last_draft_at >= DRAFT_INTERVAL_S:
                    active.last_draft_at = now
                    _run_draft_pass(
                        active,
                        ring_reader=ring_reader,
                        engine=engine,
                        language=language,
                        blacklist=blacklist,
                        publisher=publisher,
                    )
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("shutting down")
        subscriber.close()
        publisher.close()
        ring_reader.close()


def _handle_utterance_event(
    utt: Utterance,
    active: _ActiveUtterance | None,
    *,
    ring_reader: RingBufferReader,
    engine: FasterWhisperEngine,
    language: str | None,
    blacklist: frozenset[str],
    stabilizer_granularity: str,
    publisher: Publisher,
) -> _ActiveUtterance | None:
    if not utt.closed:
        logger.info("utt=%s OPEN", utt.utt_id[:8])
        return _ActiveUtterance(
            utt_id=utt.utt_id,
            start_sample=utt.span.start_sample,
            stabilizer=Stabilizer(granularity=stabilizer_granularity),
        )

    # 收句：不管有沒有追蹤到 open 事件，都直接用這則訊息自帶的 span 做定稿
    # 解碼——不依賴本地的 `active` 狀態，避免因為漏收 open 事件而整句丟失。
    audio = ring_reader.peek(utt.span.start_sample, utt.span.end_sample)
    decoded = _decode(engine, audio, language=language, beam_size=FINAL_BEAM_SIZE, blacklist=blacklist)
    if decoded is not None:
        text, detected_lang, no_speech_prob = decoded
        revision = active.revision + 1 if active is not None and active.utt_id == utt.utt_id else 0
        transcript = Transcript(
            utt_id=utt.utt_id,
            revision=revision,
            state=SubtitleState.FINAL,
            span=utt.span,
            text=text,
            src_lang=detected_lang,
            lang_locked=language is not None,
            stable_chars=len(text),
            no_speech_prob=no_speech_prob,
        )
        publisher.publish(INFERENCE_TRANSCRIPT, transcript)
        logger.info("utt=%s CLOSE [FINAL] %s", utt.utt_id[:8], text)
    else:
        logger.info("utt=%s CLOSE (濾掉：疑似幻覺或空音訊)", utt.utt_id[:8])

    return None if (active is None or active.utt_id == utt.utt_id) else active


def _run_draft_pass(
    active: _ActiveUtterance,
    *,
    ring_reader: RingBufferReader,
    engine: FasterWhisperEngine,
    language: str | None,
    blacklist: frozenset[str],
    publisher: Publisher,
) -> None:
    # 結束範圍給一個遠大於實際可能長度的上限，peek() 內部會自動夾到
    # 「目前實際寫入到哪」，不需要我們自己知道 write_total——但 peek()
    # 回傳的陣列長度就是「目前真的讀到多少」，拿來反推這次暫定稿涵蓋的
    # 實際結尾樣本數，不要用 ring_reader.read_total（那是 read() 的循序
    # 游標，這裡完全沒用到 read()，read_total 永遠是 0，用了就是錯的）。
    audio = ring_reader.peek(
        active.start_sample, active.start_sample + AUDIO_RING_BUFFER_CAPACITY_SAMPLES
    )
    if len(audio) == 0:
        return
    end_sample = active.start_sample + len(audio)

    decoded = _decode(engine, audio, language=language, beam_size=DRAFT_BEAM_SIZE, blacklist=blacklist)
    if decoded is None:
        return

    text, detected_lang, no_speech_prob = decoded
    stable_chars = active.stabilizer.update(text)
    active.revision += 1

    transcript = Transcript(
        utt_id=active.utt_id,
        revision=active.revision,
        state=SubtitleState.DRAFT,
        span=AudioSpan(active.start_sample, end_sample),
        text=text,
        src_lang=detected_lang,
        lang_locked=language is not None,
        stable_chars=stable_chars,
        no_speech_prob=no_speech_prob,
    )
    publisher.publish(INFERENCE_TRANSCRIPT, transcript)
    logger.info(
        "utt=%s DRAFT (stable=%d/%d) %s", active.utt_id[:8], stable_chars, len(text), text
    )


if __name__ == "__main__":
    main()
