"""Tier 3：Virtual Cable（後備）。見 ARCHITECTURE.md §6。

技術上不是獨立的 `CaptureBackend` 實作——VB-CABLE 這類虛擬音效裝置
在 Windows 眼中就是一個普通的 WASAPI 輸入裝置，`WasapiLoopbackCapture`
（Tier 1）已經能直接擷取它，不需要另外寫一套擷取邏輯。這支檔案只負責
「有沒有裝、裝置 id 是哪個」的偵測，真正擷取還是交給 Tier 1。

使用情境：Tier 2 不可用的機器（見 ARCHITECTURE.md §16 風險表），引導
使用者安裝 VB-CABLE、把想聽的應用程式輸出裝置手動切到 CABLE Input，
UI 再選擇這裡偵測到的裝置 id 當成一般的 WASAPI 端點（Tier 1）擷取。
"""

from __future__ import annotations

from dataclasses import dataclass

import pyaudiowpatch as pyaudio

# VB-CABLE 安裝後，裝置名稱固定包含這個字樣（不分裝置語系，這是
# VB-CABLE 本身寫死的英文裝置名稱，見官方文件）。
_VB_CABLE_NAME_MARKER = "CABLE Input"

_GUIDANCE_TEXT = (
    "偵測不到虛擬音效裝置。若要在 Tier 2（行程級擷取）不可用的機器上使用，"
    "請先安裝 VB-CABLE（https://vb-audio.com/Cable/），"
    "然後把想擷取的應用程式播放裝置手動切到「CABLE Input」，"
    "再回來這裡選擇偵測到的裝置。"
)


@dataclass(frozen=True)
class VirtualCableDevice:
    device_id: str  # 對應 CaptureTarget(kind=ENDPOINT, device_id=...) 的值
    name: str


def find_virtual_cable_device() -> VirtualCableDevice | None:
    """找系統上安裝的 VB-CABLE loopback 裝置。找不到回傳 None——呼叫端
    （UI）該顯示 `guidance_text()` 引導使用者安裝，不是當成錯誤中止。
    """
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if not info.get("isLoopbackDevice"):
                continue
            name = str(info.get("name", ""))
            if _VB_CABLE_NAME_MARKER in name:
                return VirtualCableDevice(device_id=str(info["index"]), name=name)
        return None
    finally:
        pa.terminate()


def guidance_text() -> str:
    return _GUIDANCE_TEXT
