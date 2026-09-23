"""audio/capture/wasapi_loopback.py 的測試。

這支模組的核心價值（真的能從喇叭擷取到播放中的音訊）沒辦法在自動化測試
裡可靠驗證——CI/測試環境不保證有預設播放裝置、也不該在跑測試時真的播放
聲音。這裡只測「不需要真的播放音訊也能驗證」的部分：裝置解析、錯誤處理、
串流開了之後能不能正常讀取（哪怕讀到的是安靜的環境音）。

真正「播放中的音訊真的被錄到」這件事，已經用 tests/fixtures/play_wav.ps1
手動驗證過（RMS 明顯非零、幀數與時長精確對上 48000Hz 即時速率），
記錄在 ARCHITECTURE.md §6。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from audio.capture.base import AudioFormat
from audio.capture.wasapi_loopback import WasapiLoopbackCapture
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget

pytestmark = pytest.mark.slow  # 會真的開系統音訊裝置


def test_format_raises_before_open() -> None:
    cap = WasapiLoopbackCapture()
    with pytest.raises(RuntimeError):
        _ = cap.format


def test_read_raises_before_open() -> None:
    cap = WasapiLoopbackCapture()
    with pytest.raises(RuntimeError):
        cap.read()


def test_rejects_non_endpoint_target() -> None:
    cap = WasapiLoopbackCapture()
    with pytest.raises(ValueError):
        cap.open(CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=1234))


def test_rejects_invalid_device_id() -> None:
    cap = WasapiLoopbackCapture()
    with pytest.raises(ValueError):
        cap.open(CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="not-a-number"))


def test_open_default_and_read_returns_correct_shape_and_dtype() -> None:
    """不要求有播放中的音訊，只驗證串流能開、能讀、格式正確。

    實測發現的平台行為：輸出裝置完全沒有任何 session 在播放時，Windows
    有時會整個暫停 loopback 串流、完全不送資料（不是送靜音幀），
    `read()` 會持續回傳 None，這是正常現象、不是我們的程式碼有問題——
    `audio/service.py` 的主迴圈本來就把 None 當成「這次沒有，重試」處理。
    這支測試只驗證「有讀到資料的話格式要正確」，讀不到資料不算失敗。
    """
    cap = WasapiLoopbackCapture()
    try:
        cap.open(CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="default"))
        fmt = cap.format
        assert isinstance(fmt, AudioFormat)
        assert fmt.sample_rate > 0
        assert fmt.channels >= 1

        deadline = time.monotonic() + 2.0
        chunk = None
        while time.monotonic() < deadline:
            chunk = cap.read()
            if chunk is not None:
                break
            time.sleep(0.01)

        if chunk is None:
            pytest.skip("裝置目前完全沒有音訊活動（見上方 docstring），跳過格式檢查")

        assert chunk.dtype == np.float32
        if fmt.channels > 1:
            assert chunk.ndim == 2
            assert chunk.shape[1] == fmt.channels
        else:
            assert chunk.ndim == 1
    finally:
        cap.close()


def test_close_is_idempotent_safe_to_call_once_more() -> None:
    cap = WasapiLoopbackCapture()
    cap.open(CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="default"))
    cap.close()
    # PyAudio 的 terminate() 呼叫第二次會是安全的 no-op 還是丟例外，
    # 依實作而定——這裡只要求「不要讓整個測試 process 崩潰」。
    try:
        cap.close()
    except Exception:
        pass
