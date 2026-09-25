"""audio/capture/process_loopback.py 的測試。

COM 層（真的呼叫 ActivateAudioInterfaceAsync）已經用兩個真實行程同時
出聲的隔離測試驗證過（見 ROADMAP.md M4，跟 M0 Spike B 用同一套方法，
只是改成呼叫正式的 `ProcessLoopbackCapture` 類別）。這裡測的是**自動
重建的決策邏輯**：行程死掉要不要收掉串流、找不找得到新 PID、找到後
有沒有真的重新啟用——用 mock 掉 `_activate()`、`is_process_alive`、
`find_pid_by_process_name`，不需要真的開 COM 物件，避免測試環境裡
剛好有其他同名行程造成的歧義（這是實測跑真實重建情境時踩到的坑：
這台開發機同時有好幾個 python.exe，「挑最小 PID」的重建目標選擇邏輯
選到了不相關、沒在出聲的那個——機制本身有觸發，只是選錯行程，見
ROADMAP.md M4 的記錄）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from audio.capture.process_loopback import ProcessLoopbackCapture
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget


@pytest.fixture
def cap(monkeypatch) -> ProcessLoopbackCapture:
    """建立一個「已經開啟」的 ProcessLoopbackCapture，但完全不碰真的 COM——
    `_activate()` 換成假的，只記錄呼叫、不真的做任何 Win32 呼叫。
    """
    monkeypatch.setattr("audio.capture.process_loopback.com.ensure_mta", MagicMock())
    monkeypatch.setattr(
        "audio.capture.process_loopback.get_process_name", lambda pid: "chrome.exe"
    )

    instance = ProcessLoopbackCapture()
    instance._activate = MagicMock(side_effect=lambda pid: _fake_activate(instance, pid))
    instance.open(CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=1000, include_process_tree=True))
    return instance


def _fake_activate(instance: ProcessLoopbackCapture, pid: int) -> None:
    """模擬真正 `_activate()` 會設定的欄位，不真的碰 COM。"""
    instance._audio_client = MagicMock()
    instance._capture_client = MagicMock()
    instance._pid = pid
    import time

    instance._last_liveness_check = time.monotonic()


def test_open_calls_activate_with_target_pid(cap: ProcessLoopbackCapture) -> None:
    cap._activate.assert_called_once_with(1000)
    assert cap._pid == 1000
    assert cap._process_name == "chrome.exe"


def test_read_before_open_raises() -> None:
    fresh = ProcessLoopbackCapture.__new__(ProcessLoopbackCapture)
    fresh._opened = False
    with pytest.raises(RuntimeError):
        fresh.read()


def test_liveness_check_skipped_within_interval(cap: ProcessLoopbackCapture, monkeypatch) -> None:
    """還沒到檢查間隔時，不該去查 is_process_alive（省掉沒必要的 Win32 呼叫）。"""
    checked = MagicMock(return_value=True)
    monkeypatch.setattr("audio.capture.process_loopback.is_process_alive", checked)

    cap._maybe_check_liveness()  # 剛 open 完，還沒過 _LIVENESS_CHECK_INTERVAL_S
    checked.assert_not_called()


def test_dead_process_triggers_teardown(cap: ProcessLoopbackCapture, monkeypatch) -> None:
    monkeypatch.setattr("audio.capture.process_loopback.is_process_alive", lambda pid: False)
    monkeypatch.setattr(
        "audio.capture.process_loopback.find_pid_by_process_name", lambda name: None
    )
    cap._last_liveness_check = 0.0  # 強制讓下次檢查一定觸發

    cap._maybe_check_liveness()

    assert cap._capture_client is None
    assert cap._pid is None
    # 還沒找到同名新行程，不該呼叫 _activate 第二次（第一次是 open() 時那次）
    assert cap._activate.call_count == 1


def test_dead_process_rebuilds_when_new_pid_found(cap: ProcessLoopbackCapture, monkeypatch) -> None:
    monkeypatch.setattr("audio.capture.process_loopback.is_process_alive", lambda pid: False)
    monkeypatch.setattr(
        "audio.capture.process_loopback.find_pid_by_process_name", lambda name: 2000
    )
    cap._last_liveness_check = 0.0

    cap._maybe_check_liveness()

    cap._activate.assert_called_with(2000)
    assert cap._pid == 2000  # 假的 _activate 有把新 pid 設回去
    assert cap._capture_client is not None  # 重建成功，串流恢復


def test_read_returns_none_while_waiting_for_rebuild(cap: ProcessLoopbackCapture, monkeypatch) -> None:
    """行程死掉、還沒找到新的同名行程時，read() 該回傳 None（「這次沒有」），
    不是丟例外——呼叫端（audio/service.py）本來就要能處理這種情況。
    """
    monkeypatch.setattr("audio.capture.process_loopback.is_process_alive", lambda pid: False)
    monkeypatch.setattr(
        "audio.capture.process_loopback.find_pid_by_process_name", lambda name: None
    )
    cap._last_liveness_check = 0.0

    result = cap.read()
    assert result is None


def test_alive_process_does_not_trigger_rebuild(cap: ProcessLoopbackCapture, monkeypatch) -> None:
    monkeypatch.setattr("audio.capture.process_loopback.is_process_alive", lambda pid: True)
    find_mock = MagicMock()
    monkeypatch.setattr("audio.capture.process_loopback.find_pid_by_process_name", find_mock)
    cap._last_liveness_check = 0.0

    cap._maybe_check_liveness()

    find_mock.assert_not_called()  # 還活著就不該去找新的
    assert cap._activate.call_count == 1  # 沒有多餘的重建


def test_open_rejects_non_process_target() -> None:
    from audio.capture.base import AudioFormat  # noqa: F401  (僅避免匯入順序警告)

    fresh = ProcessLoopbackCapture.__new__(ProcessLoopbackCapture)
    fresh._opened = False
    from contracts.messages import CaptureTarget as CT

    with pytest.raises(ValueError):
        fresh.open(CT(kind=CaptureTargetKind.ENDPOINT, device_id="0"))


def test_close_is_idempotent(cap: ProcessLoopbackCapture) -> None:
    cap.close()
    cap.close()  # 第二次不該丟例外
    assert cap._opened is False


def test_com_module_import_leaves_thread_in_mta_even_after_pyaudio() -> None:
    """迴歸測試（M4 UI 即時切換時實測踩到）：先 import COM 模組再用 PyAudio
    再 import 一次，apartment 都必須維持 MTA，Tier 1 ↔ Tier 2 切換才不會破壞
    COM 狀態。跑在獨立子行程，因為 COM apartment 是進程/執行緒層級的全域狀態。"""
    import subprocess
    import sys

    code = (
        "from audio.capture import _win32_process_loopback_com as com\n"
        "com.ensure_mta()\n"
        "import pyaudiowpatch as pa\n"
        "p = pa.PyAudio(); p.terminate()\n"
        "com.ensure_mta()\n"
        "raise SystemExit(0 if com._current_apartment_is_mta() else 1)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
