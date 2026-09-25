"""PROCESS 1 進入點：audio-service。見 ARCHITECTURE.md §3、§6、§7。

擷取 → 重採樣 → VAD → segmenter 的主迴圈。這支進程刻意保持單純：純 CPU、
沒有重運算、永遠優先服務擷取迴圈——任何會讓這個迴圈延遲的操作（例如
GPU 推論）都不該出現在這裡，那是 inference-service 的事（見 §2 的
GIL 爭用說明）。

M4 起支援 Tier 1（WASAPI 端點）與 Tier 2（行程級）。啟動時的初始來源由
`SAS_CAPTURE_TARGET` 環境變數決定，之後 UI 音源選擇器透過 REQ/REP 控制通道
（`CONTROL_AUDIO_CAPTURE_TARGET`）送 `CaptureTarget` 進來即可在不重啟的情況下
切換——主迴圈完全不知道底下是哪一層，這正是 `CaptureBackend` 抽象存在的理由。
"""

from __future__ import annotations

import logging
import os
import signal
import time

import numpy as np

from audio.capture_controller import CaptureController, parse_target_spec
from audio.resample import StreamResampler
from audio.ringbuffer import RingBufferWriter
from audio.segmenter import Segmenter
from audio.vad import FRAME_SAMPLES, VadStream
from contracts.messages import CaptureTarget, Utterance
from contracts.topics import (
    AUDIO_RING_BUFFER_CAPACITY_SAMPLES,
    AUDIO_RING_BUFFER_NAME,
    AUDIO_UTTERANCE,
    BUS_ENDPOINTS,
    CONTROL_AUDIO_CAPTURE_TARGET,
)
from runtime.bus import Publisher, Replier
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

# 沒有音訊可讀時，短暫讓出 CPU 再重試——不能 busy loop 空轉，
# 但也不能睡太久（會拖累 §10 延遲預算裡的擷取緩衝那一格）。
_IDLE_SLEEP_S = 0.005


def realign_pipeline(
    vad: VadStream, segmenter: Segmenter, ring_writer: RingBufferWriter
) -> Utterance | None:
    """換擷取來源時，把 VAD/segmenter 收乾淨，並讓時間軸繼續連續。

    時間軸（樣本數）不能歸零：ring buffer 的寫入位置一路遞增，字幕的 span
    是指向它的——歸零會讓新來源的 span 指到舊來源的資料。做法：

      1. VAD 手上有不足一框（<512 樣本）的殘料已經寫進 ring 但還沒被算過，
         補零湊滿一框（零也寫進 ring，兩邊位置才會一致）交給 segmenter
      2. `segmenter.flush()` 收掉進行中的 utterance——舊來源說到一半的那句話
         不能被接到新來源的聲音上
      3. VAD 遞迴狀態清掉（新來源的聲音跟舊的無關）

    回傳 flush 出來的收句事件（沒有進行中的句子就是 None），由呼叫端發布。
    """
    pad = (-vad.pending_samples) % FRAME_SAMPLES
    if vad.pending_samples and pad:
        zeros = np.zeros(pad, dtype=np.float32)
        ring_writer.write(zeros)
        for prob in vad.push(zeros):
            segmenter.process_frame(prob)
    event = segmenter.flush()
    vad.reset()
    return event


def main() -> None:
    setup_logging("audio-service")
    logger.info("starting")

    # 必須在任何 PyAudio（Tier 1）初始化之前：讓這個執行緒一開始就是 MTA，
    # 之後 Tier 1 ↔ Tier 2 即時切換才不會互相破壞 COM apartment。
    # 見 _win32_process_loopback_com.ensure_mta 的說明。
    from audio.capture import _win32_process_loopback_com as _com

    _com.ensure_mta()

    controller = CaptureController()
    ack = controller.open_initial(parse_target_spec(os.environ.get("SAS_CAPTURE_TARGET")))
    if ack.warning:
        logger.warning(ack.warning)
    capture = controller.backend
    logger.info("capture opened (%s): %s", ack.description, capture.format)

    resampler = StreamResampler(src_rate=capture.format.sample_rate)
    vad = VadStream()
    segmenter = Segmenter()
    ring_writer = RingBufferWriter(
        AUDIO_RING_BUFFER_NAME, AUDIO_RING_BUFFER_CAPACITY_SAMPLES, create=True
    )
    publisher = Publisher(BUS_ENDPOINTS[AUDIO_UTTERANCE])
    control = Replier(BUS_ENDPOINTS[CONTROL_AUDIO_CAPTURE_TARGET])
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
            request = control.poll_request(CaptureTarget, timeout_ms=0)
            if request is not None:
                previous = controller.backend
                ack = controller.switch(request)
                if ack.success and controller.backend is not previous:
                    capture = controller.backend
                    closing = realign_pipeline(vad, segmenter, ring_writer)
                    if closing is not None:
                        publisher.publish(AUDIO_UTTERANCE, closing)
                    resampler = StreamResampler(src_rate=capture.format.sample_rate)
                    logger.info("capture switched -> %s", ack.description)
                if ack.warning:
                    logger.warning(ack.warning)
                control.reply(ack)

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
        controller.close()
        control.close()
        publisher.close()
        ring_writer.close()
        ring_writer.unlink()  # audio-service 是建立者，負責釋放共享記憶體


if __name__ == "__main__":
    main()
