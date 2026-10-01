"""首次啟動精靈 / 環境檢查面板。見 ROADMAP.md M6。

第一次啟動（使用者資料目錄裡沒有標記檔）自動跳出，之後從系統匣「環境檢查...」
隨時可以重跑。內容是 `runtime/preflight.py` 的檢查清單：每一項是 ✔/⚠/✖，
有問題的附「怎麼修」。另外讓使用者順手選一個使用情境（Profile）當初始設定。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from runtime.preflight import CheckResult, Status, run_preflight, summarize
from runtime.profiles import Profile
from utils.paths import user_data_dir

logger = logging.getLogger(__name__)

_MARKER_NAME = "first_run_done"
_ICONS = {Status.OK: "✔", Status.WARN: "⚠", Status.FAIL: "✖"}
_SUMMARY = {
    Status.OK: "環境檢查全部通過，可以開始使用。",
    Status.WARN: "可以使用，但有幾項建議處理（見下方）。",
    Status.FAIL: "有必要項目沒通過，請先依「怎麼修」處理，否則可能無法運作。",
}


def should_show_first_run() -> bool:
    return not (user_data_dir() / _MARKER_NAME).exists()


def mark_first_run_done() -> None:
    path = user_data_dir() / _MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


class FirstRunDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profiles: list[Profile] | None = None,
        on_profile: Callable[[Profile], None] | None = None,
        check_fn: Callable[[], list[CheckResult]] = run_preflight,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("環境檢查")
        self.resize(640, 480)
        self._check_fn = check_fn
        self._profiles = profiles or []
        self._on_profile = on_profile

        layout = QVBoxLayout(self)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["", "項目", "結果 / 怎麼修"])
        self._table.setColumnWidth(0, 30)
        self._table.setColumnWidth(1, 190)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setWordWrap(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, 1)

        if self._profiles:
            row = QHBoxLayout()
            row.addWidget(QLabel("初始使用情境："))
            self._profile_combo = QComboBox()
            for p in self._profiles:
                self._profile_combo.addItem(p.display_name, userData=p)
            row.addWidget(self._profile_combo, 1)
            apply_button = QPushButton("套用")
            apply_button.clicked.connect(self._apply_profile)
            row.addWidget(apply_button)
            layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        recheck = buttons.addButton("重新檢查", QDialogButtonBox.ButtonRole.ActionRole)
        recheck.clicked.connect(self.run_checks)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.run_checks()

    def run_checks(self) -> None:
        results = self._check_fn()
        self._table.setRowCount(len(results))
        for row, r in enumerate(results):
            text = r.detail if r.status == Status.OK else f"{r.detail}\n→ {r.fix}"
            for col, cell in enumerate([_ICONS[r.status], r.name, text]):
                self._table.setItem(row, col, QTableWidgetItem(cell))
        self._table.resizeRowsToContents()
        self._summary.setText(_SUMMARY[summarize(results)])

    def _apply_profile(self) -> None:
        profile = self._profile_combo.currentData()
        if profile is not None and self._on_profile is not None:
            self._on_profile(profile)

    def done(self, result: int) -> None:
        mark_first_run_done()  # 看過一次就不再自動跳出（之後可從選單重開）
        super().done(result)
