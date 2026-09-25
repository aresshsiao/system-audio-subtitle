"""裝置/行程列舉，供 UI 選擇音源，也供 `process_loopback.py` 的自動重建
邏輯查詢行程存活狀態與名稱。見 ARCHITECTURE.md §6。

只用標準、有完整文件的 Win32 API（`OpenProcess`/`QueryFullProcessImageNameW`/
`EnumProcesses`），跟 `_win32_process_loopback_com.py` 那種沒有文件、
純靠實測拼出來的 COM 呼叫鏈完全不同等級的風險。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


@dataclass(frozen=True)
class AudioProcessInfo:
    """給 UI 顯示用的一筆「可擷取的行程」。"""

    pid: int
    name: str  # 例如 "chrome.exe"，不含路徑
    window_title: str = ""  # 該行程最上層可見視窗的標題，讓使用者分辨是哪一個


def _open_process_query_handle(pid: int) -> wintypes.HANDLE | None:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle if handle else None


def is_process_alive(pid: int) -> bool:
    """PID 存在但已經是殭屍/結束中也算「不算活著」——用
    `GetExitCodeProcess` 確認真的還在跑，不是只看 handle 開不開得起來
    （剛結束的行程 PID 短時間內可能還開得了 handle）。
    """
    handle = _open_process_query_handle(pid)
    if handle is None:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def get_process_name(pid: int) -> str | None:
    """回傳行程的執行檔名稱（例如 `chrome.exe`），拿不到就回傳 None——
    行程可能已經結束，或呼叫端沒有足夠權限查詢（例如系統保護行程）。
    """
    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        full_path = buf.value
        return full_path.rsplit("\\", 1)[-1] if full_path else None
    finally:
        _kernel32.CloseHandle(handle)


def list_running_pids() -> list[int]:
    """列出目前系統上所有行程的 PID（`EnumProcesses`）。"""
    array_size = 1024
    while True:
        pids = (wintypes.DWORD * array_size)()
        bytes_returned = wintypes.DWORD()
        ok = _psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(bytes_returned))
        if not ok:
            return []
        count = bytes_returned.value // ctypes.sizeof(wintypes.DWORD)
        if count < array_size:
            return [pid for pid in pids[:count] if pid != 0]
        array_size *= 2  # 陣列不夠大，行程數比預期多，放大重試


def find_pid_by_process_name(process_name: str) -> int | None:
    """依執行檔名稱（例如 `chrome.exe`）找目前正在跑、PID 最小的那個——
    瀏覽器等應用程式重開後通常會有一個新的主行程，這裡不保證找到「同一個
    視窗」，只保證找到「同名的某個行程」，供 `process_loopback.py` 的
    自動重建邏輯使用（見 ARCHITECTURE.md §6 陷阱 4）。

    找不到回傳 None——呼叫端該把這種情況當成「目標還沒重新開啟」，
    不是錯誤。
    """
    matches = [pid for pid in list_running_pids() if get_process_name(pid) == process_name]
    return min(matches) if matches else None


def list_candidate_audio_processes() -> list[AudioProcessInfo]:
    """列出可能在播放音訊、適合給使用者選的行程清單。

    這是簡化版：列出所有查得到名稱的行程，不特別過濾「目前正在出聲」的
    （要準確知道這件事需要 IAudioSessionManager2/IAudioSessionEnumerator
    這組另外的 COM 介面，判斷「有沒有音訊 session」比「查得到行程名稱」
    複雜得多）。UI 顯示時可以自己依常見瀏覽器/播放器名稱排序或篩選，
    這支函式只負責提供原始清單。
    """
    results = []
    for pid in list_running_pids():
        name = get_process_name(pid)
        if name:
            results.append(AudioProcessInfo(pid=pid, name=name))
    return results


# ---------------------------------------------------------------------------
# UI 音源選擇器用的列舉（M4）
# ---------------------------------------------------------------------------

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_CLOAKED = 14

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]


def _is_cloaked(hwnd: int) -> bool:
    """UWP 應用在背景暫停時視窗仍然 IsWindowVisible，但被 DWM 標成 cloaked
    （使用者實際看不到）——不濾掉的話清單會塞滿看不見的「設定」之類。"""
    cloaked = wintypes.DWORD(0)
    hr = _dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def list_windowed_processes() -> list[AudioProcessInfo]:
    """列出「有可見、有標題的頂層視窗」的行程——給 UI 音源選擇器用。

    條件選「有視窗」而不是「正在出聲」：準確知道誰在出聲要走
    IAudioSessionManager2 那組另外的 COM 介面，而且會漏掉「暫停中但下一秒
    就會出聲」的播放器。有視窗這個條件便宜、穩定，覆蓋播放器/瀏覽器/直播
    軟體等使用者會主動去選的對象；瀏覽器實際出聲的是音訊子行程，
    `include_process_tree=True` 會涵蓋，所以列主視窗所在的行程就對。

    沿 Z-order（最上層優先）掃，同一行程只留第一個視窗的標題。排除自己這個
    進程的視窗。
    """
    own_pid = os.getpid()
    first_title_by_pid: dict[int, str] = {}

    @_EnumWindowsProc
    def _visit(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0 or _is_cloaked(hwnd):
            return True

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid or pid.value in first_title_by_pid:
            return True

        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        first_title_by_pid[pid.value] = buf.value
        return True

    _user32.EnumWindows(_visit, 0)

    results = []
    for pid, title in first_title_by_pid.items():
        name = get_process_name(pid)
        if name:
            results.append(AudioProcessInfo(pid=pid, name=name, window_title=title))
    results.sort(key=lambda info: (info.name.lower(), info.window_title.lower()))
    return results


@dataclass(frozen=True)
class LoopbackDeviceInfo:
    device_id: str  # 對應 CaptureTarget(kind=ENDPOINT, device_id=...)，是 PyAudio 裝置索引
    name: str
    is_default: bool


def list_loopback_devices() -> list[LoopbackDeviceInfo]:
    """列出所有可擷取的 WASAPI loopback 端點（Tier 1 的目標）。"""
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        try:
            default_index = pa.get_default_wasapi_loopback()["index"]
        except OSError:
            default_index = None
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("isLoopbackDevice"):
                devices.append(
                    LoopbackDeviceInfo(
                        device_id=str(info["index"]),
                        name=str(info["name"]),
                        is_default=(info["index"] == default_index),
                    )
                )
        return devices
    finally:
        pa.terminate()
