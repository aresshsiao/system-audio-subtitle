"""多螢幕與混合 DPI 定位。見 ARCHITECTURE.md §12：per-monitor DPI aware v2，
螢幕切換時重算。

Qt 從 6.0 開始，`QScreen` 給的座標與尺寸都已經是「該螢幕自己的邏輯像素」
（Qt 內部處理掉了實體像素 ↔ 邏輯像素的轉換），所以這裡不需要自己算 DPI
縮放比例——只要跟著 `devicePixelRatio()` 走字體大小的相對縮放，視窗位置
與大小維持用邏輯座標即可。真正的陷阱是「使用者把視窗拖到另一個螢幕」
這個動作發生時，Qt 不會自動幫字型重新縮放，要自己監聽 `screenChanged`
去重算。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QWidget

# 基準字體大小是在 96 DPI（devicePixelRatio=1.0）下設計的；縮放比例
# 直接乘上 QScreen.devicePixelRatio()。
BASE_FONT_POINT_SIZE = 28

# 字幕底部要離螢幕底緣多遠（邏輯像素），避免蓋到工作列或播放器控制列。
BOTTOM_MARGIN = 80
OVERLAY_HEIGHT_RATIO = 0.25  # 浮層視窗高度佔螢幕高度的比例（給多行字幕空間）


def compute_geometry(screen: QScreen) -> QRect:
    """給定一個 QScreen，算出浮層視窗該放的位置與大小（邏輯座標）。

    浮層鋪滿螢幕寬度、貼底部一小段高度——不是只做成貼合文字大小的小視窗，
    因為文字長度會變，視窗大小如果跟著文字忽大忽小，`WA_TransparentForMouseEvents`
    的穿透區域也會跟著變，使用者會覺得「點擊穿透」時好時壞。固定一塊
    區域，文字在裡面置中/貼底，穿透行為才穩定可預期。
    """
    avail = screen.availableGeometry()  # 排除工作列的可用區域
    height = int(avail.height() * OVERLAY_HEIGHT_RATIO)
    return QRect(avail.left(), avail.bottom() - height - BOTTOM_MARGIN + 1, avail.width(), height)


def font_point_size_for_screen(screen: QScreen) -> int:
    return round(BASE_FONT_POINT_SIZE * screen.devicePixelRatio())


class ScreenTracker(QObject):
    """監看浮層視窗目前在哪個螢幕，螢幕改變（使用者拖到別的螢幕，或該
    螢幕的 DPI 設定被改變）時發出信號，呼叫端重新套用該螢幕的幾何與字體。
    """

    screen_changed = Signal(QScreen)

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._current_screen: QScreen | None = None
        # `screenChanged` 是 QWindow 的 signal，不是 QWidget 的——QWidget 只有
        # 在真的被 show() 過、有底層原生視窗（windowHandle()）之後才存在這個
        # signal 可以接，show() 之前 windowHandle() 是 None（實測踩到的坑）。
        # 所以 `start()` 必須在呼叫端 show() 之後才呼叫，這裡才接得上。

    def start(self) -> None:
        handle = self._window.windowHandle()
        if handle is None:
            raise RuntimeError("ScreenTracker.start() 必須在視窗 show() 之後呼叫")
        handle.screenChanged.connect(self._on_screen_changed)

        screen = self._window.screen()
        if screen is not None:
            self._on_screen_changed(screen)

    def _on_screen_changed(self, screen: QScreen) -> None:
        if screen is self._current_screen:
            return
        self._current_screen = screen
        if screen is not None:
            screen.geometryChanged.connect(lambda _rect: self._on_screen_changed(screen))
            self.screen_changed.emit(screen)
