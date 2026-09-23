"""CaptureBackend 抽象介面。見 ARCHITECTURE.md §6。

三層 Backend（WASAPI 端點 / Process Loopback / Virtual Cable）都實作同一
個介面，上層（`audio/service.py`）完全不知道底下是哪一層——這是 Tier 2/3
可以分期實作、Tier 1 先跑的前提：只要遵守這個介面，換掉底層實作不需要動
`audio/service.py` 一行程式碼。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from contracts.messages import CaptureTarget


@dataclass(frozen=True)
class AudioFormat:
    """擷取端實際吐出來的原生格式（尚未重採樣）。"""

    sample_rate: int
    channels: int


class CaptureBackend(Protocol):
    """所有擷取層共用的介面。`read()` 是阻塞式短暫等待，不是非阻塞 poll——
    音訊擷取本來就該用阻塞等待資料到來的方式驅動主迴圈節奏，真正的
    「不阻塞」鐵則是在 ring buffer 的寫入端（見 audio/ringbuffer.py），
    不是在這一層。
    """

    def open(self, target: CaptureTarget) -> None:
        """開啟指定的擷取目標。呼叫前 `format` 尚未確定，呼叫後才能讀。"""
        ...

    def read(self) -> np.ndarray | None:
        """讀一個緩衝週期的樣本，原生取樣率、float32、shape=(frames, channels)
        或 mono 時 shape=(frames,)。裝置暫時沒有新資料時回傳 None
        （呼叫端應該視為「這次沒有」，不是錯誤）。
        """
        ...

    @property
    def format(self) -> AudioFormat:
        """`open()` 之後才有效；`open()` 之前呼叫是呼叫端的錯誤。"""
        ...

    def close(self) -> None:
        """釋放底層裝置資源。之後這個實例不應該再被呼叫 `read()`。"""
        ...
