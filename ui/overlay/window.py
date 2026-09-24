"""無邊框、置頂、點擊穿透的字幕浮層視窗。見 ARCHITECTURE.md §12。

Windows 上這幾個屬性要疊在一起才會是正確的行為：

  - `Qt.FramelessWindowHint`      無邊框
  - `Qt.WindowStaysOnTopHint`     永遠置頂（蓋在播放器之上）
  - `Qt.Tool`                     不進工作列、不進 Alt-Tab
  - `Qt.WindowDoesNotAcceptFocus` 點擊字幕不會把播放器的鍵盤焦點搶走
                                  （對應 Win32 的 WS_EX_NOACTIVATE）
  - `WA_TranslucentBackground`    背景真的透明，不是純色假透明

**點擊穿透踩到的坑（實測發現，不是理論推測）**：Qt 的
`WA_TransparentForMouseEvents` 屬性**不會**自動轉成原生的
`WS_EX_TRANSPARENT` 擴充樣式——它只影響 Qt 自己內部的事件分派（同一個
Qt 應用程式內，疊在一起的 widget 之間的滑鼠事件穿透），對「滑鼠事件
穿透到底下其他應用程式的視窗」（例如播放器）完全沒有效果。真正跨應用
程式穿透，必須直接用 `ctypes` 呼叫 `SetWindowLongW` 修改原生的
`WS_EX_TRANSPARENT` 位元，這支檔案的 `_set_native_click_through()`
就是在做這件事。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

_user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000


def _set_native_click_through(hwnd: int, enabled: bool) -> None:
    """直接操作原生擴充視窗樣式，讓滑鼠事件真的穿透到底下其他應用程式
    的視窗（見上方模組 docstring 的踩坑說明）。`WS_EX_LAYERED` 一併確保
    有設起來——半透明視窗要正確合成，需要這個旗標搭配存在。
    """
    ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style |= WS_EX_LAYERED
    if enabled:
        ex_style |= WS_EX_TRANSPARENT
    else:
        ex_style &= ~WS_EX_TRANSPARENT
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)


class OverlayWindow(QWidget):
    def __init__(self, content: QWidget) -> None:
        super().__init__()
        self._edit_mode = False

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 這個 Qt 屬性留著（對 Qt 自己內部的事件分派仍有意義），但真正
        # 讓滑鼠穿透到「其他應用程式視窗」的是 showEvent 裡呼叫的
        # _set_native_click_through()。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._content = content
        self._content.setParent(self)

        self._drag_origin = None  # 編輯模式下拖曳用

    def showEvent(self, event) -> None:  # noqa: N802
        # winId() 必須在原生視窗真的建立之後才有效——show() 之前呼叫會
        # 強迫提前建立、但屬性可能還沒完全套用，所以統一在 showEvent
        # （視窗確定已經 show 出來）裡設定原生點擊穿透樣式，每次
        # 顯示（包含 set_edit_mode 觸發的 hide()→show()）都會重新套用。
        _set_native_click_through(int(self.winId()), not self._edit_mode)
        super().showEvent(event)

    def set_edit_mode(self, enabled: bool) -> None:
        """編輯模式：關閉滑鼠穿透，讓使用者可以拖曳字幕到想要的位置。"""
        if enabled == self._edit_mode:
            return
        self._edit_mode = enabled
        if self.isVisible():
            _set_native_click_through(int(self.winId()), not enabled)

    @property
    def edit_mode(self) -> bool:
        return self._edit_mode

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名慣例)
        self._content.resize(self.size())
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._edit_mode and event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._edit_mode and self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(event)
