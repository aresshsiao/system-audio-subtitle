"""語言包管理面板。見 ARCHITECTURE.md §13.4。

  - **選擇**：列出內建 + 匯入的語言包。兩種模式——「自動偵測」（可複選多個包，依
    偵測到的語言自動路由）與「鎖定單一包」（強制所有語音都當作該語言處理，關掉
    自動偵測；只啟用一個包在 inference-service 端本來就是這個語意）
  - **匯入**：資料夾或 `.langpack.zip`，流程是驗證 → 複製進使用者目錄 → 出現在
    清單（見 `inference/langpack_import.py`）。錯誤（格式不合法、含可執行內容、
    zip-slip…）用對話框明確顯示原因，不是崩潰、也不是靜默失敗
  - **開啟時查詢** inference-service 目前實際啟用哪些包（`SetActiveLangPacks`
    的 `pack_ids=None` 查詢），不再是 M3 時「預設全選」的近似做法

按「套用」時一律帶 `reload=True`：剛匯入的包 inference-service 端也要重新掃描才看得到。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from contracts.messages import SetActiveLangPacks, SetActiveLangPacksAck
from contracts.topics import BUS_ENDPOINTS, CONTROL_LANGPACK_RELOAD
from inference.langpack import LangPackRegistry
from inference.langpack_import import LangPackImportError, import_langpack, remove_user_langpack
from runtime.bus import Requester
from utils.paths import user_langpacks_dir

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_MS = 3000


def _send_request(message: SetActiveLangPacks) -> SetActiveLangPacksAck | None:
    requester = Requester(BUS_ENDPOINTS[CONTROL_LANGPACK_RELOAD], timeout_ms=_REQUEST_TIMEOUT_MS)
    try:
        return requester.request(message, SetActiveLangPacksAck)
    finally:
        requester.close()


def _pick_folder(parent: QWidget) -> str | None:
    path = QFileDialog.getExistingDirectory(parent, "選擇語言包資料夾")
    return path or None


def _pick_zip(parent: QWidget) -> str | None:
    path, _ = QFileDialog.getOpenFileName(
        parent, "選擇語言包壓縮檔", "", "語言包 (*.zip *.langpack.zip)"
    )
    return path or None


class LangPackManagerDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        request_fn: Callable[[SetActiveLangPacks], SetActiveLangPacksAck | None] = _send_request,
        pick_folder_fn: Callable[[QWidget], str | None] = _pick_folder,
        pick_zip_fn: Callable[[QWidget], str | None] = _pick_zip,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("語言包設定")
        self.resize(460, 460)
        self._request_fn = request_fn
        self._pick_folder_fn = pick_folder_fn
        self._pick_zip_fn = pick_zip_fn
        self._registry = LangPackRegistry()
        self._checkboxes: dict[str, QCheckBox] = {}
        self._active_ids: list[str] = []
        self._query_failed = False

        layout = QVBoxLayout(self)

        self._auto_radio = QRadioButton("自動偵測語言（可複選多個包，自動路由）")
        self._lock_radio = QRadioButton("鎖定單一語言包（強制當作該語言處理，不自動偵測）")
        self._lock_combo = QComboBox()
        layout.addWidget(self._auto_radio)
        self._rows_container = QVBoxLayout()
        layout.addLayout(self._rows_container)
        layout.addWidget(self._lock_radio)
        layout.addWidget(self._lock_combo)
        self._auto_radio.toggled.connect(self._sync_mode_widgets)

        import_row = QHBoxLayout()
        for label, handler in (("匯入資料夾...", self._import_folder), ("匯入 zip...", self._import_zip)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            import_row.addWidget(button)
        import_row.addStretch(1)
        layout.addLayout(import_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._query_active()
        self._rebuild()

    # -- 資料同步 --

    def _query_active(self) -> None:
        """問 inference-service 現在實際啟用哪些包。問不到就退回「全部啟用」的近似
        （inference-service 未設定時的預設行為），並在狀態列說明。"""
        try:
            ack = self._request_fn(SetActiveLangPacks(pack_ids=None))
        except Exception as e:  # noqa: BLE001
            logger.warning("查詢語言包狀態失敗: %s", e)
            ack = None
        if ack is not None and ack.success:
            self._active_ids = list(ack.active_pack_ids)
            self._query_failed = False
        else:
            self._active_ids = []
            self._query_failed = True

    def _rebuild(self) -> None:
        self._registry.reload()
        packs = self._registry.all_packs()
        user_dir = user_langpacks_dir().resolve()

        while self._rows_container.count():
            item = self._rows_container.takeAt(0)
            if item.layout() is not None:
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w is not None:
                        w.deleteLater()
            elif item.widget() is not None:
                item.widget().deleteLater()
        self._checkboxes = {}
        self._lock_combo.clear()

        if not packs:
            self._rows_container.addWidget(QLabel("（找不到任何語言包）"))

        known_ids = {p.id for p in packs}
        active = [i for i in self._active_ids if i in known_ids]
        checked_default = set(active) if not self._query_failed else known_ids

        for pack in packs:
            is_user = user_dir in Path(pack.pack_dir).resolve().parents
            row = QHBoxLayout()
            checkbox = QCheckBox(f"{pack.display_name}  [{pack.id}]" + ("（匯入）" if is_user else ""))
            checkbox.setChecked(pack.id in checked_default)
            row.addWidget(checkbox, 1)
            if is_user:
                remove = QPushButton("移除")
                remove.clicked.connect(lambda _=False, pid=pack.id: self._remove(pid))
                row.addWidget(remove)
            self._rows_container.addLayout(row)
            self._checkboxes[pack.id] = checkbox
            self._lock_combo.addItem(pack.display_name, userData=pack.id)

        if self._query_failed:
            self._set_status("連不上 inference-service，顯示的是預設（全部啟用）；按「套用」時才會真的送出。")
        # 只啟用一個包 = 鎖定模式
        if len(active) == 1 and not self._query_failed:
            self._lock_radio.setChecked(True)
            self._lock_combo.setCurrentIndex(max(0, self._lock_combo.findData(active[0])))
        else:
            self._auto_radio.setChecked(True)
        self._sync_mode_widgets()

    def _sync_mode_widgets(self) -> None:
        auto = self._auto_radio.isChecked()
        for checkbox in self._checkboxes.values():
            checkbox.setEnabled(auto)
        self._lock_combo.setEnabled(not auto)

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    # -- 匯入 / 移除 --

    def _import_folder(self) -> None:
        path = self._pick_folder_fn(self)
        if path:
            self._do_import(path)

    def _import_zip(self) -> None:
        path = self._pick_zip_fn(self)
        if path:
            self._do_import(path)

    def _do_import(self, path: str) -> None:
        try:
            result = import_langpack(path)
        except LangPackImportError as e:
            self._set_status(f"匯入失敗：{e}")
            QMessageBox.warning(self, "匯入語言包失敗", str(e))
            return
        self._rebuild()
        verb = "已更新" if result.replaced else "已匯入"
        self._set_status(f"{verb}語言包「{result.pack.display_name}」。勾選後按「套用」即可使用。")

    def _remove(self, pack_id: str) -> None:
        if remove_user_langpack(pack_id):
            self._active_ids = [i for i in self._active_ids if i != pack_id]
            self._rebuild()
            self._set_status(f"已移除 {pack_id}。按「套用」讓 inference-service 也更新。")

    # -- 套用 --

    def selected_pack_ids(self) -> list[str]:
        if self._lock_radio.isChecked():
            pid = self._lock_combo.currentData()
            return [pid] if pid else []
        return [pack_id for pack_id, cb in self._checkboxes.items() if cb.isChecked()]

    def _apply(self) -> None:
        selected = self.selected_pack_ids()
        if not selected:
            QMessageBox.warning(self, "語言包設定", "至少要選一個語言包，否則不會有任何字幕。")
            return

        try:
            ack = self._request_fn(SetActiveLangPacks(pack_ids=selected, reload=True))
        except Exception as e:  # noqa: BLE001 — 連線層的任何失敗都當同一種「套用失敗」處理
            logger.warning("送出語言包設定失敗: %s", e)
            self._set_status(f"連線失敗，inference-service 可能還沒啟動：{e}")
            return

        if ack is None:
            self._set_status("逾時：inference-service 沒有回應（3 秒內）")
        elif ack.success:
            mode = "鎖定" if len(ack.active_pack_ids) == 1 else "自動路由"
            self._set_status(f"已套用（{mode}），下一句字幕就會生效：{', '.join(ack.active_pack_ids)}")
        else:
            self._set_status(f"套用失敗：{ack.error}")
