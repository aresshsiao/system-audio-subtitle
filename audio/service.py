"""PROCESS 1 進入點：audio-service。見 ARCHITECTURE.md §3、§6、§7。

擷取 → 重採樣 → VAD → segmenter 的主迴圈。這支進程刻意保持單純：純 CPU、
沒有重運算、永遠優先服務擷取迴圈——任何會讓這個迴圈延遲的操作（例如
GPU 推論）都不該出現在這裡，那是 inference-service 的事（見 §2 的
GIL 爭用說明）。

M1 現況：只支援 Tier 1（WASAPI 端點 loopback）。Tier 2（行程級）在 M4
補上時，只需要換 `CaptureTarget` 的 kind 並換一個 `CaptureBackend`
實作，這支主迴圈完全不用改。
"""

from __future__ import annotations

import logging
import signal
import time

from audio.capture.wasapi_loopback import WasapiLoopbackCapture
from audio.resample import StreamResampler
from audio.ringbuffer import RingBufferWriter
from audio.segmenter import Segmenter
from audio.vad import VadStream
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget
from contracts.topics import (
    AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    AUDIO_RING_BUFFER_NAME,
    AUDIO_UTTERANCE,
    BUS_ENDPOINTS,
)
from runtime.bus import Publisher
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

# 沒有音訊可讀時，短暫讓出 CPU 再重試——不能 busy loop 空轉，
# 但也不能睡太久（會拖累 §10 延遲預算裡的擷取緩衝那一格）。
_IDLE_SLEEP_S = 0.005


def main() -> None:
    setup_logging("audio-service")
    logger.info("starting")

    capture = WasapiLoopbackCapture()
    capture.open(CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="default"))
    logger.info("capture opened: %s", capture.format)

    resampler = StreamResampler(src_rate=capture.format.sample_rate)
    vad = VadStream()
    segmenter = Segmenter()
    ring_writer = RingBufferWriter(
        AUDIO_RING_BUFFER_NAME, AUDIO_RING_BUFFER_CAPACITY_SAMPLES, create=True
    )
    publisher = Publisher(BUS_ENDPOINTS[AUDIO_UTTERANCE])
    logger.info(
        "ring buffer '%s' 建立完成，容量 %d 樣本",
        AUDIO_RING_BUFFER_NAME,
        AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    )

    stop_requested = False

    def _handle_stop_signal(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    # Windows 上 Popen.terminate() 實際呼叫 TerminateProcess，不會觸發這裡
    # （見 runtime/_stub_common.py 的同樣說明）；這裡主要是讓手動 Ctrl+C
    # 跑這支進程時能乾淨關閉，供開發/手動測試使用。
    try:
        signal.signal(signal.SIGINT, _handle_stop_signal)
        signal.signal(signal.SIGTERM, _handle_stop_signal)
    except (ValueError, OSError):
        pass

    try:
        while not stop_requested:
            chunk = capture.read()
            if chunk is None:
                time.sleep(_IDLE_SLEEP_S)
                continue

            mono_16k = resampler.push(chunk)
            if len(mono_16k) == 0:
                continue

            ring_writer.write(mono_16k)

            for prob in vad.push(mono_16k):
                event = segmenter.process_frame(prob)
                if event is None:
                    continue
                publisher.publish(AUDIO_UTTERANCE, event)
                logger.info(
                    "%s utt=%s span=[%d,%d] (%.2fs)",
                    "CLOSE" if event.closed else "OPEN ",
                    event.utt_id[:8],
                    event.span.start_sample,
                    event.span.end_sample,
                    event.span.duration_seconds,
                )
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("shutting down")
        capture.close()
        publisher.close()
        ring_writer.close()
        ring_writer.unlink()  # audio-service 是建立者，負責釋放共享記憶體


if __name__ == "__main__":
    main()
