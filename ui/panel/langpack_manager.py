"""語言包管理面板（最小版）。見 ARCHITECTURE.md §13.4。

**M3 只做「列出已知的語言包、複選啟用、送出去」**。完整匯入 UI（拖曳
資料夾/`.langpack.zip`、驗證錯誤訊息、單包鎖定模式獨立開關）留到 M6，
那時候的畫面會取代這支檔案的內容，但送出設定走的控制通道
（`SetActiveLangPacks` REQ/REP）不會變。

**已知限制**：這個面板開啟時沒辦法真的問到 inference-service 目前實際
啟用哪些包（控制通道目前只有「設定」沒有「查詢」），所以預設全部勾選
——這通常剛好符合 inference-service 自己的預設行為（啟動時沒特別設定
就是全部啟用），使用者要縮小範圍再自己取消勾選。要做到「開啟時準確
反映目前狀態」需要在控制通道加一個查詢用的訊息型別，這支檔案先不做，
避免在還沒有真正的匯入流程之前，為了一個小細節就擴充契約層。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from contracts.messages import SetActiveLangPacks, SetActiveLangPacksAck
from contracts.topics import BUS_ENDPOINTS, CONTROL_LANGPACK_RELOAD
from inference.langpack import LangPackRegistry
from runtime.bus import Requester

logger = logging.getLogger(__name__)


class LangPackManagerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("語言包設定")
        self.resize(360, 280)

        registry = LangPackRegistry()
        registry.reload()
        packs = registry.all_packs()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("勾選要啟用的語言包（可複選，自動依偵測到的語言路由）："))

        self._checkboxes: dict[str, QCheckBox] = {}
        if not packs:
            layout.addWidget(QLabel("（找不到任何語言包，檢查 config/langpacks/）"))
        for pack in packs:
            checkbox = QCheckBox(f"{pack.display_name}  [{pack.id}]")
            checkbox.setChecked(True)  # 見檔案開頭的「已知限制」說明
            layout.addWidget(checkbox)
            self._checkboxes[pack.id] = checkbox

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
        )
        apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self) -> None:
        selected = [pack_id for pack_id, cb in self._checkboxes.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "語言包設定", "至少要選一個語言包，否則不會有任何字幕。")
            return

        try:
            requester = Requester(BUS_ENDPOINTS[CONTROL_LANGPACK_RELOAD], timeout_ms=3000)
            try:
                ack = requester.request(SetActiveLangPacks(pack_ids=selected), SetActiveLangPacksAck)
            finally:
                requester.close()
        except Exception as e:  # noqa: BLE001 — 連線層的任何失敗都當同一種「套用失敗」處理
            logger.warning("送出語言包設定失敗: %s", e)
            self._status_label.setText(f"連線失敗，inference-service 可能還沒啟動：{e}")
            return

        if ack is None:
            self._status_label.setText("逾時：inference-service 沒有回應（3 秒內）")
        elif ack.success:
            self._status_label.setText(f"已套用，下一句字幕就會生效：{', '.join(ack.active_pack_ids)}")
        else:
            self._status_label.setText(f"套用失敗：{ack.error}")
