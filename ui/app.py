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

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from contracts.messages import Subtitle
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

    subtitle_received = Signal(object)  # Subtitle
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
                        self.subtitle_received.emit(Subtitle.decode(msg.encode("utf-8")))
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

    font_scale_holder = [1.0]  # Profile 的字體倍率，見 choose_profile

    def apply_screen_geometry(screen) -> None:
        window.setGeometry(compute_geometry(screen))
        renderer.set_font_point_size(round(font_point_size_for_screen(screen) * font_scale_holder[0]))

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

    # Ctrl+Alt+, / Ctrl+Alt+. 微調匯出字幕的整體偏移（±100ms）。時間軸原點是「開始
    # 擷取」不是影片開頭，播放晚於擷取啟動時用這個對齊（見 gateway/export.py）。
    from ui.panel.history import OFFSET_STEP_MS, ExportSettings, format_offset

    export_settings = ExportSettings()
    VK_OEM_COMMA, VK_OEM_PERIOD = 0xBC, 0xBE

    def nudge_offset(delta_ms: int) -> None:
        offset = export_settings.nudge(delta_ms)
        tray.setToolTip(f"System Audio Subtitle（字幕偏移 {format_offset(offset)}）")
        tray.showMessage("字幕偏移", f"匯出字幕偏移：{format_offset(offset)}", QSystemTrayIcon.MessageIcon.NoIcon, 1500)

    hotkeys.register(MOD_CONTROL | MOD_ALT, VK_OEM_COMMA, lambda: nudge_offset(-OFFSET_STEP_MS))
    hotkeys.register(MOD_CONTROL | MOD_ALT, VK_OEM_PERIOD, lambda: nudge_offset(+OFFSET_STEP_MS))

    def on_subtitle(subtitle: Subtitle) -> None:
        renderer.set_state(RenderState(text=subtitle.target_text, state=subtitle.state))

    client = SubtitleClient(gateway_ws_url())
    client.subtitle_received.connect(on_subtitle)
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

    def open_langpack_manager() -> None:
        from ui.panel.langpack_manager import LangPackManagerDialog

        dialog = LangPackManagerDialog()
        dialog.exec()

    def open_audio_source() -> None:
        from ui.panel.audio_source import AudioSourceDialog

        dialog = AudioSourceDialog()
        dialog.exec()

    audio_source_action = QAction("音源選擇...")
    audio_source_action.triggered.connect(open_audio_source)
    menu.addAction(audio_source_action)

    history_dialog: list = []

    def open_history() -> None:
        from ui.panel.history import HistoryDialog

        if history_dialog and history_dialog[0].isVisible():
            history_dialog[0].refresh()
            history_dialog[0].raise_()
            return
        history_dialog[:] = [HistoryDialog(export_settings)]
        history_dialog[0].show()

    history_action = QAction("字幕歷史與匯出...")
    history_action.triggered.connect(open_history)
    menu.addAction(history_action)

    # --- 情境 Profile ---
    from inference.langpack import LangPackRegistry
    from runtime.profiles import load_profiles
    from ui.panel.langpack_manager import _send_request as send_langpack_request
    from ui.profile_apply import apply_profile

    current_profile: list = []

    def set_font_scale(scale: float) -> None:
        font_scale_holder[0] = scale
        screen = window.screen()
        renderer.set_font_point_size(round(font_point_size_for_screen(screen) * scale))

    def choose_profile(profile) -> None:
        registry = LangPackRegistry()
        registry.reload()
        report = apply_profile(
            profile,
            known_langpack_ids={p.id for p in registry.all_packs()},
            request_fn=send_langpack_request,
            set_font_scale=set_font_scale,
        )
        current_profile[:] = [profile]
        tray.showMessage(
            f"情境：{profile.display_name}", chr(10).join(report.messages), QSystemTrayIcon.MessageIcon.Information, 6000
        )

    profile_menu = menu.addMenu("情境 Profile")
    for profile in load_profiles():
        action = QAction(profile.display_name, profile_menu)
        action.triggered.connect(lambda _=False, p=profile: choose_profile(p))
        profile_menu.addAction(action)

    def open_first_run() -> None:
        from ui.panel.first_run import FirstRunDialog

        FirstRunDialog(profiles=load_profiles(), on_profile=choose_profile).exec()

    env_action = QAction("環境檢查...")
    env_action.triggered.connect(open_first_run)
    menu.addAction(env_action)

    def open_cloud_polish() -> None:
        from ui.panel.cloud_polish import CloudPolishDialog

        dialog = CloudPolishDialog()
        dialog.exec()

    cloud_action = QAction("雲端精修...")
    cloud_action.triggered.connect(open_cloud_polish)
    menu.addAction(cloud_action)

    metrics_panel: list = []  # 非模態，要留著參照不然會被回收

    def open_metrics() -> None:
        from ui.panel.metrics_panel import MetricsPanel

        if metrics_panel and metrics_panel[0].isVisible():
            metrics_panel[0].raise_()
            return
        metrics_panel[:] = [MetricsPanel()]
        metrics_panel[0].show()

    metrics_action = QAction("效能監控...")
    metrics_action.triggered.connect(open_metrics)
    menu.addAction(metrics_action)

    langpack_action = QAction("語言包設定...")
    langpack_action.triggered.connect(open_langpack_manager)
    menu.addAction(langpack_action)

    menu.addSeparator()
    quit_action = QAction("結束")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()

    # 主控視窗：關掉它就結束整個服務（app.quit → launcher 收掉所有子進程）
    from ui.panel.control_window import ControlWindow

    control = ControlWindow(
        [
            ("顯示/隱藏字幕", toggle_visibility),
            ("語言包設定...", open_langpack_manager),
            ("音源選擇...", open_audio_source),
            ("字幕歷史與匯出...", open_history),
            ("效能監控...", open_metrics),
            ("環境檢查...", open_first_run),
        ],
        on_close=app.quit,
        icon=_make_tray_icon(),
    )
    control.show()

    from ui.panel.first_run import should_show_first_run

    if should_show_first_run():
        QTimer.singleShot(800, open_first_run)  # 等浮層與系統匣都就緒再跳出

    exit_code = app.exec()

    # Profile 要求結束時存逐字稿（例如 meeting）
    if current_profile and current_profile[0].autosave_transcript:
        from ui.panel.history import GatewayClient
        from ui.profile_apply import autosave_transcript

        try:
            saved = autosave_transcript(GatewayClient().export("txt"))
            if saved:
                logger.info("逐字稿已存到 %s", saved)
        except Exception as e:  # noqa: BLE001 — gateway 已經關掉時不能讓結束流程炸掉
            logger.warning("自動存逐字稿失敗: %s", e)

    hotkeys.unregister_all()
    client.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
