"""PROCESS 3（前端半部）進入點：Qt 應用程式 + 系統匣。見 ARCHITECTURE.md §12。

跟 gateway 的連線走 WebSocket（`SubtitleClient`，背景執行緒 + 重連），
不是直接接 ZMQ——UI 是「無狀態消費者」，該接的是 gateway 那一層
（見 ARCHITECTURE.md §3 的三層職責界線），不是繞過 gateway 直接碰
inference-service 的內部匯流排。
"""

from __future__ import annotations

import logging
import sys
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from contracts.messages import Transcript
from contracts.topics import gateway_ws_url
from ui.hotkeys import MOD_ALT, MOD_CONTROL, HotkeyManager
from ui.overlay.layout import ScreenTracker, compute_geometry, font_point_size_for_screen
from ui.overlay.renderer import RenderState, SubtitleRenderer
from ui.overlay.window import OverlayWindow
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_S = 1.0
_RECV_TIMEOUT_S = 1.0


class SubtitleClient(QThread):
    """背景執行緒：連 gateway 的 WebSocket，收到訊息就透過 Qt signal 轉發
    給主執行緒。斷線會自動重連（gateway 可能比 UI 晚啟動，或中途重啟）。
    """

    transcript_received = Signal(object)  # Transcript
    connection_state_changed = Signal(bool)  # True=已連線

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self._stop_requested = False

    def run(self) -> None:
        while not self._stop_requested:
            try:
                with ws_connect(self._url, open_timeout=5) as ws:
                    logger.info("已連上 gateway: %s", self._url)
                    self.connection_state_changed.emit(True)
                    while not self._stop_requested:
                        try:
                            msg = ws.recv(timeout=_RECV_TIMEOUT_S)
                        except TimeoutError:
                            continue
                        transcript = Transcript.decode(msg.encode("utf-8"))
                        self.transcript_received.emit(transcript)
            except (WebSocketException, OSError) as e:
                if self._stop_requested:
                    break
                logger.info("gateway 連線中斷/失敗 (%s)，%.1fs 後重試", e, _RECONNECT_DELAY_S)
                self.connection_state_changed.emit(False)
                time.sleep(_RECONNECT_DELAY_S)

    def stop(self) -> None:
        self._stop_requested = True
        self.wait(3000)


def _make_tray_icon() -> QIcon:
    """程式化畫一個簡單的圖示，不需要另外準備圖檔資源。"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(255, 255, 255))
    painter.setPen(QColor(0, 0, 0))
    painter.drawRoundedRect(2, 8, 28, 16, 4, 4)
    painter.end()
    return QIcon(pixmap)


def main() -> int:
    setup_logging("ui")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 關掉浮層（隱藏）不代表整個程式要結束

    renderer = SubtitleRenderer()
    window = OverlayWindow(renderer)
    screen_tracker = ScreenTracker(window)

    def apply_screen_geometry(screen) -> None:
        window.setGeometry(compute_geometry(screen))
        renderer.set_font_point_size(font_point_size_for_screen(screen))

    screen_tracker.screen_changed.connect(apply_screen_geometry)
    window.show()
    screen_tracker.start()

    hotkeys = HotkeyManager()
    hotkeys.install(app)

    def toggle_visibility() -> None:
        window.setVisible(not window.isVisible())

    def toggle_edit_mode() -> None:
        window.set_edit_mode(not window.edit_mode)

    # Ctrl+Alt+S 顯示/隱藏、Ctrl+Alt+E 編輯模式（見 ARCHITECTURE.md §12）
    hotkeys.register(MOD_CONTROL | MOD_ALT, ord("S"), toggle_visibility)
    hotkeys.register(MOD_CONTROL | MOD_ALT, ord("E"), toggle_edit_mode)

    def on_transcript(transcript: Transcript) -> None:
        renderer.set_state(RenderState(text=transcript.text, state=transcript.state))

    client = SubtitleClient(gateway_ws_url())
    client.transcript_received.connect(on_transcript)
    client.start()

    tray = QSystemTrayIcon(_make_tray_icon())
    tray.setToolTip("System Audio Subtitle")
    menu = QMenu()

    toggle_action = QAction("顯示/隱藏字幕")
    toggle_action.triggered.connect(toggle_visibility)
    menu.addAction(toggle_action)

    edit_action = QAction("編輯模式（可拖曳）")
    edit_action.setCheckable(True)
    edit_action.toggled.connect(window.set_edit_mode)
    menu.addAction(edit_action)

    menu.addSeparator()
    quit_action = QAction("結束")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()

    exit_code = app.exec()

    hotkeys.unregister_all()
    client.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
