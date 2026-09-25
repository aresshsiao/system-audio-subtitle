"""雲端精修設定面板。見 ARCHITECTURE.md §9。

**預設關閉**。開啟 = 字幕的原文（與前文、術語表）會被送到第三方 API，
所以：

  - 勾選開啟時，一定先跳確認對話框，**明講資料會送去哪個主機**
    （主機名稱由 inference-service 回報，來自它的環境變數設定）
  - 面板上開啟期間持續顯示同樣的提示，不是點過一次同意就消失
  - API 金鑰不經過這個面板，只存在 inference-service 的環境變數裡

精修後的字幕是靜默替換的，這個面板只負責開關與顯示狀態（成功/熔斷/被降級
階梯暫停）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from contracts.messages import CloudPolishStatus, SetCloudPolish
from contracts.topics import BUS_ENDPOINTS, CONTROL_CLOUD_POLISH
from runtime.bus import Requester

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_MS = 3000
_WARNING_STYLE = "color: #b45309;"
_ERROR_STYLE = "color: #b91c1c;"
_OK_STYLE = "color: #15803d;"


def _send_request(message: SetCloudPolish) -> CloudPolishStatus | None:
    requester = Requester(BUS_ENDPOINTS[CONTROL_CLOUD_POLISH], timeout_ms=_REQUEST_TIMEOUT_MS)
    try:
        return requester.request(message, CloudPolishStatus)
    finally:
        requester.close()


class CloudPolishDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        request_fn: Callable[[SetCloudPolish], CloudPolishStatus | None] = _send_request,
        confirm_fn: Callable[[QWidget, str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("雲端精修")
        self.resize(440, 260)
        self._request_fn = request_fn
        self._confirm_fn = confirm_fn or self._ask_consent
        self._host: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "雲端精修會把最近幾句字幕連同前文送給 LLM 重新翻譯，翻好後靜默替換畫面上的字幕。\n"
                "關閉時完全使用本地翻譯，不會有任何資料離開這台電腦。"
            )
        )
        layout.itemAt(0).widget().setWordWrap(True)

        self._checkbox = QCheckBox("啟用雲端精修")
        self._checkbox.clicked.connect(self._on_clicked)
        layout.addWidget(self._checkbox)

        self._privacy_label = QLabel("")
        self._privacy_label.setWordWrap(True)
        self._privacy_label.setStyleSheet(_WARNING_STYLE)
        layout.addWidget(self._privacy_label)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()

    # -- 狀態同步 --

    def refresh(self) -> None:
        self._show_status(self._query(SetCloudPolish(enabled=None)))

    def _query(self, message: SetCloudPolish) -> CloudPolishStatus | None:
        try:
            status = self._request_fn(message)
        except Exception as e:  # noqa: BLE001 — 連線層任何失敗都當同一種「連不上」
            logger.warning("雲端精修請求失敗: %s", e)
            self._set_status(f"連線失敗，inference-service 可能還沒啟動：{e}", _ERROR_STYLE)
            return None
        if status is None:
            self._set_status("逾時：inference-service 沒有回應", _ERROR_STYLE)
        return status

    def _show_status(self, status: CloudPolishStatus | None) -> None:
        if status is None:
            self._checkbox.setEnabled(False)
            return

        self._host = status.endpoint_host
        self._checkbox.setEnabled(status.configured)
        self._checkbox.setChecked(status.enabled)
        self._privacy_label.setText(
            f"⚠ 已開啟：字幕原文正送往第三方服務 {status.endpoint_host}。" if status.enabled else ""
        )

        if not status.configured:
            self._set_status(
                "尚未設定雲端服務。啟動 inference-service 前請先設定環境變數：\n"
                "SAS_LLM_BASE_URL（OpenAI 相容端點）、SAS_LLM_MODEL、SAS_LLM_API_KEY",
                _WARNING_STYLE,
            )
        elif status.error:
            self._set_status(status.error, _ERROR_STYLE)
        elif not status.enabled:
            self._set_status("已關閉（純本地）。", _OK_STYLE)
        elif status.breaker_open:
            self._set_status(
                "雲端暫時無法使用（斷網或額度用盡），已自動退回純本地字幕；恢復後會自動繼續。",
                _WARNING_STYLE,
            )
        elif status.blocked_by_degrade:
            self._set_status(
                "系統負載過高，已暫停精修以保住即時性；負載回落後自動恢復。", _WARNING_STYLE
            )
        else:
            self._set_status("運作中。", _OK_STYLE)

    def _set_status(self, text: str, style: str) -> None:
        self._status_label.setStyleSheet(style)
        self._status_label.setText(text)

    # -- 使用者操作 --

    def _ask_consent(self, parent: QWidget, host: str) -> bool:
        answer = QMessageBox.question(
            parent,
            "確認送出資料到第三方",
            f"開啟後，字幕的原文（與前幾句前文）會被送到：\n\n    {host}\n\n"
            "請確認你信任這個服務、且影片內容可以外送。要開啟嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_clicked(self, checked: bool) -> None:
        if checked and not self._confirm_fn(self, self._host or "（未知主機）"):
            self._checkbox.setChecked(False)
            return
        self._show_status(self._query(SetCloudPolish(enabled=checked)))
