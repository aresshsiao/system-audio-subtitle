"""主控視窗：讓整個服務「有一個看得到的視窗」。關閉這個視窗 = 結束全部服務
（launcher 會在 UI 結束後收掉 audio / inference / gateway）。最小化則縮到工作列。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class ControlWindow(QWidget):
    def __init__(
        self,
        actions: list[tuple[str, Callable[[], None]]],
        on_close: Callable[[], None],
        icon: QIcon | None = None,
    ) -> None:
        super().__init__()
        self._on_close = on_close
        self.setWindowTitle("System Audio Subtitle")
        if icon is not None:
            self.setWindowIcon(icon)
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("服務執行中。關閉此視窗會結束所有服務。"))
        for label, callback in actions:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, cb=callback: cb())
            layout.addWidget(button)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt 覆寫
        event.accept()
        self._on_close()
