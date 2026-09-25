"""音源選擇面板。見 ARCHITECTURE.md §6、§16。

三種來源，對應 `CaptureBackend` 三層：

  - 整個輸出裝置（Tier 1）：擷取某個喇叭/耳機端點的全部混音
  - 單一應用程式（Tier 2，只翻譯媒體聲音的關鍵）：只抓選定行程（含子行程）
  - 虛擬音效裝置（Tier 3）：偵測 VB-CABLE，Tier 2 用不了的機器的後備

按「套用」送 `CaptureTarget` 給 audio-service（REQ/REP），不需要重啟。
Tier 2 失敗時 audio-service 會自動退回 Tier 1 並在 ack 帶 `warning`，
這裡把它用醒目的顏色顯示——不能靜默退回，否則使用者以為還在行程級隔離，
實際上 Discord 語音也在被翻譯。

面板開啟時列舉一次目前的裝置/行程，「重新整理」可再掃一次（使用者常常是
先開著這個面板才去開影片）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from audio.capture.enumerate import (
    AudioProcessInfo,
    LoopbackDeviceInfo,
    list_loopback_devices,
    list_windowed_processes,
)
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget, SetCaptureTargetAck
from contracts.topics import BUS_ENDPOINTS, CONTROL_AUDIO_CAPTURE_TARGET
from runtime.bus import Requester

logger = logging.getLogger(__name__)

# 切換要開新的擷取、再關舊的，Tier 2 啟用大約幾百 ms，留足餘裕
_REQUEST_TIMEOUT_MS = 8000

_WARNING_STYLE = "color: #b45309;"  # 琥珀色：成功但有但書
_ERROR_STYLE = "color: #b91c1c;"
_OK_STYLE = "color: #15803d;"


def _default_cable_finder() -> str | None:
    from audio.capture.virtual_cable import find_virtual_cable_device

    device = find_virtual_cable_device()
    return device.device_id if device else None


class AudioSourceDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        device_lister: Callable[[], list[LoopbackDeviceInfo]] = list_loopback_devices,
        process_lister: Callable[[], list[AudioProcessInfo]] = list_windowed_processes,
        cable_finder: Callable[[], str | None] = _default_cable_finder,
        request_fn: Callable[[CaptureTarget], SetCaptureTargetAck | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("音源選擇")
        self.resize(460, 340)

        self._device_lister = device_lister
        self._process_lister = process_lister
        self._cable_finder = cable_finder
        self._request_fn = request_fn or self._send_request

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("要翻譯哪裡來的聲音？（切換即時生效，不需要重啟）"))

        self._device_radio = QRadioButton("整個輸出裝置（所有應用程式的聲音混在一起）")
        self._device_combo = QComboBox()
        self._process_radio = QRadioButton("單一應用程式（只翻譯它的聲音，推薦）")
        self._process_combo = QComboBox()
        self._tree_check = QCheckBox("包含子行程（瀏覽器的音訊在子行程，通常要勾）")
        self._tree_check.setChecked(True)
        self._cable_radio = QRadioButton("虛擬音效裝置（VB-CABLE，後備方案）")
        self._cable_label = QLabel("")
        self._cable_label.setWordWrap(True)

        layout.addWidget(self._process_radio)
        layout.addWidget(self._process_combo)
        layout.addWidget(self._tree_check)
        layout.addWidget(self._device_radio)
        layout.addWidget(self._device_combo)
        layout.addWidget(self._cable_radio)
        layout.addWidget(self._cable_label)

        refresh_row = QHBoxLayout()
        self._refresh_button = QPushButton("重新整理清單")
        self._refresh_button.clicked.connect(self.refresh)
        refresh_row.addWidget(self._refresh_button)
        refresh_row.addStretch(1)
        layout.addLayout(refresh_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()
        self._process_radio.setChecked(self._process_combo.count() > 0)
        if not self._process_radio.isChecked():
            self._device_radio.setChecked(True)

    def refresh(self) -> None:
        self._device_combo.clear()
        for device in self._device_lister():
            label = device.name + ("（系統預設）" if device.is_default else "")
            self._device_combo.addItem(label, userData=device.device_id)
            if device.is_default:
                self._device_combo.setCurrentIndex(self._device_combo.count() - 1)

        self._process_combo.clear()
        for proc in self._process_lister():
            title = proc.window_title if len(proc.window_title) <= 40 else proc.window_title[:40] + "…"
            self._process_combo.addItem(f"{proc.name} — {title}", userData=proc.pid)

        self._cable_id = self._cable_finder()
        if self._cable_id is None:
            from audio.capture.virtual_cable import guidance_text

            self._cable_label.setText(guidance_text())
            self._cable_radio.setEnabled(False)
        else:
            self._cable_label.setText("已偵測到虛擬音效裝置。")
            self._cable_radio.setEnabled(True)

        self._device_radio.setEnabled(self._device_combo.count() > 0)
        self._process_radio.setEnabled(self._process_combo.count() > 0)

    def build_target(self) -> CaptureTarget | None:
        if self._process_radio.isChecked() and self._process_combo.count() > 0:
            return CaptureTarget(
                kind=CaptureTargetKind.PROCESS,
                pid=int(self._process_combo.currentData()),
                include_process_tree=self._tree_check.isChecked(),
            )
        if self._cable_radio.isChecked() and self._cable_id is not None:
            return CaptureTarget(kind=CaptureTargetKind.VIRTUAL_CABLE, device_id=self._cable_id)
        if self._device_radio.isChecked() and self._device_combo.count() > 0:
            return CaptureTarget(
                kind=CaptureTargetKind.ENDPOINT,
                device_id=str(self._device_combo.currentData()),
            )
        return None

    def _send_request(self, target: CaptureTarget) -> SetCaptureTargetAck | None:
        requester = Requester(
            BUS_ENDPOINTS[CONTROL_AUDIO_CAPTURE_TARGET], timeout_ms=_REQUEST_TIMEOUT_MS
        )
        try:
            return requester.request(target, SetCaptureTargetAck)
        finally:
            requester.close()

    def _set_status(self, text: str, style: str) -> None:
        self._status_label.setStyleSheet(style)
        self._status_label.setText(text)

    def _apply(self) -> None:
        target = self.build_target()
        if target is None:
            self._set_status("沒有可套用的選項。", _ERROR_STYLE)
            return

        try:
            ack = self._request_fn(target)
        except Exception as e:  # noqa: BLE001 — 連線層任何失敗都當同一種「套用失敗」
            logger.warning("送出音源切換失敗: %s", e)
            self._set_status(f"連線失敗，audio-service 可能還沒啟動：{e}", _ERROR_STYLE)
            return

        if ack is None:
            self._set_status(
                f"逾時：audio-service 沒有回應（{_REQUEST_TIMEOUT_MS // 1000} 秒內）", _ERROR_STYLE
            )
        elif not ack.success:
            self._set_status(f"切換失敗（維持原來的音源）：{ack.error}", _ERROR_STYLE)
        elif ack.warning:
            self._set_status(f"已切換到：{ack.description}\n⚠ {ack.warning}", _WARNING_STYLE)
        else:
            self._set_status(f"已切換到：{ack.description}", _OK_STYLE)
