"""音訊時鐘：樣本數 ↔ 時間的換算，以及延遲量測用的參考點。

ARCHITECTURE.md §5 的規則是「時間的唯一真相是樣本數，不是牆上時鐘」，
但量測端到端延遲（scripts/bench_latency.py）、寫 log 時間戳記，
還是需要牆上時鐘 —— 這支檔案就是兩者之間唯一的橋接點，其他地方
不應該自己做 sample <-> wall time 的換算。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioClock:
    """描述一條音訊串流「第 0 個樣本」對應的牆上時間。

    audio-service 在擷取開始的瞬間建立一個 AudioClock，之後所有 AudioSpan
    都用這個 clock 換算回牆上時間 —— 僅供 metrics/log/UI 顯示使用，
    絕不能拿牆上時間反過來推算樣本數（順序不可逆，見 §5）。
    """

    sample_rate: int
    stream_start_monotonic: float  # time.monotonic() 在第 0 個樣本擷取當下的值

    @classmethod
    def start_now(cls, sample_rate: int) -> "AudioClock":
        return cls(sample_rate=sample_rate, stream_start_monotonic=time.monotonic())

    def sample_to_elapsed_seconds(self, sample_index: int) -> float:
        """第 sample_index 個樣本，距離串流開始經過了幾秒。"""
        return sample_index / self.sample_rate

    def sample_to_monotonic(self, sample_index: int) -> float:
        """第 sample_index 個樣本對應的 time.monotonic() 值。"""
        return self.stream_start_monotonic + self.sample_to_elapsed_seconds(sample_index)

    def latency_since_sample(self, sample_index: int) -> float:
        """現在距離「這個樣本被擷取的時間點」過了幾秒 —— 端到端延遲量測的核心算式。

        用法範例：暫定稿在 utt.span.end_sample 產生時呼叫這個函式，
        就是 ARCHITECTURE.md §10 延遲預算表要量的「暫定稿合計」數字。
        """
        return time.monotonic() - self.sample_to_monotonic(sample_index)
