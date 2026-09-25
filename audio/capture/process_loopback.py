"""Tier 2：Process Loopback（行程級）。見 ARCHITECTURE.md §6 Tier 2 ★核心差異化。

這是真正的「只翻譯媒體聲音」——只抓指定行程（含其子行程樹）播放的
聲音，不會混進 Discord 語音、通知音效這些同時在系統上發聲的其他來源。
技術基礎是 M0 Spike B 驗證過、用兩個獨立行程同時出聲測過隔離性的 COM
呼叫鏈（見 `_win32_process_loopback_com.py`），這支檔案補上：

  - 正式的 `CaptureBackend` 介面（跟 Tier 1 的 `WasapiLoopbackCapture`
    遵守同一個介面，`audio/service.py` 換 Tier 完全不用改）
  - PID 存活監看：目標行程結束後串流不會自己收掉（見 §6 陷阱 4），
    這裡定期檢查，行程死了就主動收掉舊串流
  - 自動重建：行程結束後，記住的是「行程名稱」不是「PID」，一旦偵測到
    同名的新行程出現（例如使用者關掉瀏覽器又重開），自動對新 PID
    重新啟用 loopback，不需要使用者手動介入
"""

from __future__ import annotations

import logging
import time
from ctypes import POINTER, byref, cast

import numpy as np

from audio.capture import _win32_process_loopback_com as com
from audio.capture.base import AudioFormat
from audio.capture.enumerate import find_pid_by_process_name, get_process_name, is_process_alive
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_CHANNELS = 2
_LIVENESS_CHECK_INTERVAL_S = 1.0  # 多久檢查一次目標行程還活不活著


class ProcessLoopbackCapture:
    """`CaptureBackend` 的 Tier 2 實作。"""

    def __init__(self) -> None:
        com.ensure_mta()
        self._format = AudioFormat(sample_rate=_SAMPLE_RATE, channels=_CHANNELS)
        self._fmt_struct = com.make_float_stereo_format(_SAMPLE_RATE, _CHANNELS)

        self._opened = False  # open() 呼叫過一次就是 True，close() 前不會變回 False
        self._audio_client = None
        self._capture_client = None
        self._pid: int | None = None
        self._process_name: str | None = None
        self._include_process_tree = True
        self._last_liveness_check = 0.0

    def open(self, target: CaptureTarget) -> None:
        if target.kind != CaptureTargetKind.PROCESS:
            raise ValueError(
                f"ProcessLoopbackCapture 只接受 CaptureTargetKind.PROCESS，收到 {target.kind}"
            )
        self._include_process_tree = target.include_process_tree
        self._process_name = get_process_name(target.pid)
        if self._process_name is None:
            raise ValueError(f"找不到 PID {target.pid} 對應的行程，可能已經結束或權限不足")

        self._activate(target.pid)
        self._opened = True

    def _activate(self, pid: int) -> None:
        audio_client = com.activate_process_loopback_audio_client(
            pid, include_process_tree=self._include_process_tree
        )
        audio_client.Initialize(
            com.AUDCLNT_SHAREMODE_SHARED,
            com.AUDCLNT_STREAMFLAGS_LOOPBACK,
            com.REFTIMES_PER_SEC,
            0,
            byref(self._fmt_struct),
            None,
        )
        capture_ptr = audio_client.GetService(byref(com.IID_IAudioCaptureClient))
        capture_client = cast(capture_ptr, POINTER(com.IAudioCaptureClient))
        audio_client.Start()

        self._audio_client = audio_client
        self._capture_client = capture_client
        self._pid = pid
        self._last_liveness_check = time.monotonic()
        logger.info("process loopback 已對 PID %d (%s) 啟動", pid, self._process_name)

    def read(self) -> np.ndarray | None:
        if not self._opened:
            raise RuntimeError("read() 前必須先呼叫 open()")

        self._maybe_check_liveness()

        if self._capture_client is None:
            # 目標行程已結束、還在等同名行程重新出現——回傳 None（「這次
            # 沒有」），不是錯誤，呼叫端（audio/service.py）本來就要能
            # 處理 read() 回傳 None 的情況（見 base.py 的介面說明）。
            return None

        raw, num_frames, is_silent = com.read_capture_packet(self._capture_client, self._fmt_struct)
        if raw is None or num_frames == 0:
            return None
        if is_silent:
            # §6 陷阱 3：目標沒出聲時拿到的是靜音而非「無資料」。
            return np.zeros((num_frames, self._format.channels), dtype=np.float32)
        return com.bytes_to_frames(raw, num_frames, self._format.channels)

    def _maybe_check_liveness(self) -> None:
        now = time.monotonic()
        if now - self._last_liveness_check < _LIVENESS_CHECK_INTERVAL_S:
            return
        self._last_liveness_check = now

        if self._pid is not None and is_process_alive(self._pid):
            return  # 還活著（或者上次重建失敗、目前處在等待狀態，_pid 為 None 時跳過這個分支）

        if self._pid is not None:
            logger.warning("目標行程 PID %d (%s) 已結束，嘗試自動重建...", self._pid, self._process_name)
            self._teardown_stream()

        new_pid = find_pid_by_process_name(self._process_name) if self._process_name else None
        if new_pid is None:
            return  # 還沒等到同名行程重新出現，下次 liveness check 再試

        try:
            self._activate(new_pid)
            logger.info("自動重建成功，新 PID=%d", new_pid)
        except Exception:
            logger.exception("自動重建失敗，%.1fs 後再試", _LIVENESS_CHECK_INTERVAL_S)

    def _teardown_stream(self) -> None:
        if self._audio_client is not None:
            try:
                self._audio_client.Stop()
            except Exception:
                logger.debug("停止舊串流時發生例外（可忽略，行程可能已經不在了）", exc_info=True)
        self._audio_client = None
        self._capture_client = None
        self._pid = None

    @property
    def format(self) -> AudioFormat:
        return self._format

    def close(self) -> None:
        self._teardown_stream()
        self._opened = False
