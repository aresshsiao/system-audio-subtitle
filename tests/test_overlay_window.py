"""ui/overlay/window.py 的測試：真的開一個 Qt 視窗，用 Win32 API 檢查原生
視窗樣式旗標——這裡不是隨便寫著好玩，是因為 M2 開發時實測踩過一個坑：
`Qt.WA_TransparentForMouseEvents` 這個 Qt 屬性**不會**自動轉成原生的
`WS_EX_TRANSPARENT`，只憑這個屬性寫出來的點擊穿透，實際上完全沒有跨
應用程式穿透的效果，肉眼在畫面上也看不出差異，非常容易在不知情的情況下
上線一個「看起來對、實際上點不穿」的浮層。這支測試直接查詢原生視窗
樣式位元，不能被這種表面上正常的假象騙過去。
"""

from __future__ import annotations

import ctypes

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlay.window import OverlayWindow

pytestmark = pytest.mark.slow  # 真的開 Qt/Win32 視窗

GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_CAPTION = 0x00C00000

_user32 = ctypes.windll.user32


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _ex_style(window: OverlayWindow) -> int:
    return _user32.GetWindowLongW(int(window.winId()), GWL_EXSTYLE)


def _style(window: OverlayWindow) -> int:
    return _user32.GetWindowLongW(int(window.winId()), GWL_STYLE)


def test_default_flags_are_correct(qapp) -> None:
    window = OverlayWindow(QWidget())
    try:
        window.show()
        qapp.processEvents()
        qapp.processEvents()

        ex = _ex_style(window)
        assert ex & WS_EX_TOOLWINDOW, "沒進工作列/Alt-Tab 這個旗標沒設到"
        assert ex & WS_EX_NOACTIVATE, "不搶焦點這個旗標沒設到"
        assert ex & WS_EX_TOPMOST, "置頂這個旗標沒設到"
        assert not (_style(window) & WS_CAPTION), "應該是無邊框，卻有標題列樣式"
    finally:
        window.close()


def test_click_through_enabled_by_default(qapp) -> None:
    """預設狀態（非編輯模式）滑鼠事件應該真的穿透到底下的其他視窗——
    查原生 WS_EX_TRANSPARENT 位元，不能只看 Qt 屬性表面上有沒有設。
    """
    window = OverlayWindow(QWidget())
    try:
        window.show()
        qapp.processEvents()
        qapp.processEvents()

        assert _ex_style(window) & WS_EX_TRANSPARENT
        assert window.edit_mode is False
    finally:
        window.close()


def test_edit_mode_disables_native_click_through(qapp) -> None:
    window = OverlayWindow(QWidget())
    try:
        window.show()
        qapp.processEvents()
        qapp.processEvents()
        assert _ex_style(window) & WS_EX_TRANSPARENT  # 起始狀態：穿透

        window.set_edit_mode(True)
        qapp.processEvents()
        assert not (_ex_style(window) & WS_EX_TRANSPARENT), "編輯模式下不該還是穿透"
        assert window.edit_mode is True

        window.set_edit_mode(False)
        qapp.processEvents()
        assert _ex_style(window) & WS_EX_TRANSPARENT, "退出編輯模式後應該恢復穿透"
        assert window.edit_mode is False
    finally:
        window.close()


def test_set_edit_mode_is_idempotent_noop_when_unchanged(qapp) -> None:
    window = OverlayWindow(QWidget())
    try:
        window.show()
        qapp.processEvents()
        window.set_edit_mode(False)  # 已經是 False，設同樣的值不該出錯
        assert window.edit_mode is False
    finally:
        window.close()
