"""ui/panel/audio_source.py 的測試：用假的列舉函式與假的請求函式，只測
面板的決策（組出什麼 CaptureTarget、怎麼呈現 ack）。真實切換另有整合驗證
（見 ROADMAP.md M4）。"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from audio.capture.enumerate import AudioProcessInfo, LoopbackDeviceInfo
from contracts.enums import CaptureTargetKind
from contracts.messages import SetCaptureTargetAck
from ui.panel.audio_source import AudioSourceDialog

pytestmark = pytest.mark.slow  # 需要 QApplication


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


DEVICES = [
    LoopbackDeviceInfo("19", "Speakers", False),
    LoopbackDeviceInfo("20", "Headphones", True),
]
PROCS = [AudioProcessInfo(100, "chrome.exe", "Some Video"), AudioProcessInfo(200, "vlc.exe", "x")]


def make(qapp, *, devices=DEVICES, procs=PROCS, cable="30", ack=None, sent=None):
    def request_fn(target):
        if sent is not None:
            sent.append(target)
        return ack

    return AudioSourceDialog(
        device_lister=lambda: devices,
        process_lister=lambda: procs,
        cable_finder=lambda: cable,
        request_fn=request_fn,
    )


def test_defaults_to_process_mode_when_processes_exist(qapp) -> None:
    target = make(qapp).build_target()
    assert target.kind == CaptureTargetKind.PROCESS and target.pid == 100
    assert target.include_process_tree is True


def test_falls_back_to_device_mode_when_no_windowed_processes(qapp) -> None:
    target = make(qapp, procs=[]).build_target()
    assert target.kind == CaptureTargetKind.ENDPOINT
    assert target.device_id == "20"  # 預設裝置被預選


def test_device_selection_builds_endpoint_target(qapp) -> None:
    d = make(qapp)
    d._device_radio.setChecked(True)
    d._device_combo.setCurrentIndex(0)
    target = d.build_target()
    assert (target.kind, target.device_id) == (CaptureTargetKind.ENDPOINT, "19")


def test_cable_radio_disabled_and_guidance_shown_when_missing(qapp) -> None:
    d = make(qapp, cable=None)
    assert not d._cable_radio.isEnabled()
    assert "VB-CABLE" in d._cable_label.text()


def test_cable_selection_builds_virtual_cable_target(qapp) -> None:
    d = make(qapp)
    d._cable_radio.setChecked(True)
    target = d.build_target()
    assert (target.kind, target.device_id) == (CaptureTargetKind.VIRTUAL_CABLE, "30")


def test_apply_sends_target_and_shows_success(qapp) -> None:
    sent = []
    ack = SetCaptureTargetAck(
        success=True, active_kind=CaptureTargetKind.PROCESS, description="行程 chrome.exe (PID 100)"
    )
    d = make(qapp, ack=ack, sent=sent)
    d._apply()
    assert len(sent) == 1 and sent[0].pid == 100
    assert "chrome.exe" in d._status_label.text()


def test_apply_shows_fallback_warning_prominently(qapp) -> None:
    ack = SetCaptureTargetAck(
        success=True,
        active_kind=CaptureTargetKind.ENDPOINT,
        description="預設輸出裝置",
        warning="行程級擷取失敗（boom），已退回整個輸出裝置的擷取",
    )
    d = make(qapp, ack=ack)
    d._apply()
    text = d._status_label.text()
    assert "已退回" in text and "預設輸出裝置" in text
    assert "b45309" in d._status_label.styleSheet()  # 警告色，不是成功色


def test_apply_shows_error_and_timeout_without_crashing(qapp) -> None:
    d = make(qapp, ack=SetCaptureTargetAck(success=False, error="nope"))
    d._apply()
    assert "nope" in d._status_label.text()

    d = make(qapp, ack=None)
    d._apply()
    assert "逾時" in d._status_label.text()


def test_apply_survives_request_exception(qapp) -> None:
    d = make(qapp)

    def boom(_target):
        raise OSError("down")

    d._request_fn = boom
    d._apply()
    assert "連線失敗" in d._status_label.text()
