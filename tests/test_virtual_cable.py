"""audio/capture/virtual_cable.py 的測試。這台開發機沒有安裝 VB-CABLE，
所以「真的找到裝置」這條路徑沒辦法在這裡驗證，只測「找不到時的行為」
跟名稱比對邏輯本身（用假的裝置清單，不需要真的裝 VB-CABLE）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from audio.capture.virtual_cable import (
    VirtualCableDevice,
    find_virtual_cable_device,
    guidance_text,
)

pytestmark = __import__("pytest").mark.slow  # 會真的列舉系統音效裝置


def test_find_returns_none_when_not_installed() -> None:
    """這台開發機沒裝 VB-CABLE，驗證真實環境下的行為：不丟例外、回傳 None。"""
    result = find_virtual_cable_device()
    assert result is None


def test_guidance_text_mentions_install_and_cable_input() -> None:
    text = guidance_text()
    assert "VB-CABLE" in text
    assert "CABLE Input" in text


def test_find_virtual_cable_device_matches_by_name_marker(monkeypatch) -> None:
    """用假的裝置清單驗證比對邏輯本身：找 loopback 裝置裡名稱包含
    "CABLE Input" 的那個，不是隨便一個 loopback 裝置都算。
    """
    fake_devices = [
        {"index": 0, "isLoopbackDevice": False, "name": "麥克風"},
        {"index": 1, "isLoopbackDevice": True, "name": "喇叭 (Realtek) [Loopback]"},
        {"index": 2, "isLoopbackDevice": True, "name": "CABLE Input (VB-Audio Virtual Cable) [Loopback]"},
    ]

    fake_pa = MagicMock()
    fake_pa.get_device_count.return_value = len(fake_devices)
    fake_pa.get_device_info_by_index.side_effect = lambda i: fake_devices[i]

    monkeypatch.setattr(
        "audio.capture.virtual_cable.pyaudio.PyAudio", lambda: fake_pa
    )

    result = find_virtual_cable_device()
    assert result == VirtualCableDevice(
        device_id="2", name="CABLE Input (VB-Audio Virtual Cable) [Loopback]"
    )
    fake_pa.terminate.assert_called_once()


def test_find_virtual_cable_device_none_when_no_match(monkeypatch) -> None:
    fake_devices = [
        {"index": 0, "isLoopbackDevice": True, "name": "喇叭 (Realtek) [Loopback]"},
    ]
    fake_pa = MagicMock()
    fake_pa.get_device_count.return_value = len(fake_devices)
    fake_pa.get_device_info_by_index.side_effect = lambda i: fake_devices[i]
    monkeypatch.setattr("audio.capture.virtual_cable.pyaudio.PyAudio", lambda: fake_pa)

    assert find_virtual_cable_device() is None
