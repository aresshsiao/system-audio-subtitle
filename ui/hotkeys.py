"""全域熱鍵。見 ARCHITECTURE.md §12：顯示/隱藏、編輯模式、暫停。

Qt 的 `QShortcut` 只在視窗有焦點時才會觸發，但 Overlay 刻意設計成
`WindowDoesNotAcceptFocus`（不搶焦點，見 window.py），代表 `QShortcut`
在這裡完全用不上。全域熱鍵在 Windows 上要靠 Win32 的
`RegisterHotKey` / `WM_HOTKEY`，用 ctypes 直接呼叫，不另外引入套件
（`requirements.txt` 沒有 `keyboard`/`global-hotkeys` 這類依賴）。

`RegisterHotKey` 註冊的熱鍵訊息會送進呼叫執行緒自己的訊息佇列，Qt
應用程式的主執行緒本來就在跑 Win32 訊息迴圈（`QApplication.exec()`
底層就是），所以只要掛一個 `QAbstractNativeEventFilter` 攔截
`WM_HOTKEY` 訊息即可，不需要另開執行緒。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import itertools
import logging
from collections.abc import Callable

from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000  # 按住不放不要重複觸發

_user32 = ctypes.windll.user32
_id_counter = itertools.count(1)


class HotkeyManager(QAbstractNativeEventFilter):
    """管理這個行程註冊的所有全域熱鍵。一個行程只需要一個實例。"""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._registered_ids: list[int] = []

    def register(self, modifiers: int, vk: int, callback: Callable[[], None]) -> None:
        """註冊一個全域熱鍵。`vk` 是虛擬鍵碼（例如 `ord('S')`）。

        重複註冊同一組按鍵組合會失敗（`RegisterHotKey` 回傳 0），通常代表
        another app 已經佔用了這個組合——記 log 但不中止，讓其餘熱鍵仍然
        可以正常運作。
        """
        hotkey_id = next(_id_counter)
        ok = _user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk)
        if not ok:
            logger.warning(
                "全域熱鍵註冊失敗（可能被其他程式佔用）: modifiers=0x%X vk=0x%X",
                modifiers,
                vk,
            )
            return
        self._callbacks[hotkey_id] = callback
        self._registered_ids.append(hotkey_id)

    def unregister_all(self) -> None:
        for hotkey_id in self._registered_ids:
            _user32.UnregisterHotKey(None, hotkey_id)
        self._registered_ids.clear()
        self._callbacks.clear()

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt 命名慣例)
        if event_type != b"windows_generic_MSG":
            return False, 0

        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            callback = self._callbacks.get(msg.wParam)
            if callback is not None:
                callback()
        return False, 0

    def install(self, app: QApplication) -> None:
        app.installNativeEventFilter(self)
