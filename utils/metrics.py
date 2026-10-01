"""輕量可觀測性：延遲直方圖（p50/p95/p99）、計數器、量表。見
ARCHITECTURE.md §15。

刻意不用 prometheus_client 之類的套件：這是單機自用工具，需要的只是「最近
一段時間的分位數」，不是長期時序資料庫。直方圖只保留最近 N 筆樣本（滑動
視窗），分位數反映的是「現在的狀況」——使用者抱怨字幕現在很慢時，要看的是
最近幾十秒，不是開機以來的平均（平均值會把偶發的長尾整個抹平，見 §15
「直方圖，不是平均值」）。

執行緒安全：雲端精修在背景執行緒記錄成功/延遲，主迴圈也在寫，所以全部加鎖。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class HistogramSummary:
    count: int  # 視窗內的樣本數
    p50: float
    p95: float
    p99: float
    max: float

    def as_dict(self) -> dict[str, float]:
        return {
            "count": float(self.count),
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "max": self.max,
        }


def _percentile(sorted_values: list[float], q: float) -> float:
    """最近秩法（nearest-rank）：不做插值，回傳的一定是真的出現過的樣本值。"""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(len(sorted_values) * q))
    return sorted_values[min(rank, len(sorted_values)) - 1]


class Histogram:
    """同時受「筆數」與「時間」兩個上限約束：最多 `window` 筆、且只保留最近
    `max_age_s` 秒。只用筆數的話，低頻事件（例如每句定稿一筆、約 5~10 秒一筆）
    的 512 筆窗口要一個多小時才會換完——M5 實機負載測試時，壓力早就撤掉了，
    面板上的「定稿落後 p95」還停在壓力期間的 7 秒。"""

    def __init__(
        self,
        window: int = 512,
        max_age_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=window)
        self._max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._samples.append((self._clock(), value))

    def summary(self) -> HistogramSummary:
        cutoff = self._clock() - self._max_age_s
        with self._lock:
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            values = sorted(v for _t, v in self._samples)
        if not values:
            return HistogramSummary(0, 0.0, 0.0, 0.0, 0.0)
        return HistogramSummary(
            count=len(values),
            p50=_percentile(values, 0.50),
            p95=_percentile(values, 0.95),
            p99=_percentile(values, 0.99),
            max=values[-1],
        )


class Metrics:
    """一個進程一份。名稱慣例：`<階段>_ms`（直方圖）、`<事件>_total`（計數器）、
    其餘是量表（目前值）。"""

    def __init__(self) -> None:
        self._histograms: dict[str, Histogram] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            hist = self._histograms.setdefault(name, Histogram())
        hist.observe(value)

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def timer(self, name: str) -> "_Timer":
        """`with metrics.timer("asr_final_ms"):` 量測區塊耗時（毫秒）。"""
        return _Timer(self, name)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            hist_items = list(self._histograms.items())
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        return {
            "histograms": {name: h.summary().as_dict() for name, h in hist_items},
            "counters": counters,
            "gauges": gauges,
        }


class _Timer:
    def __init__(self, metrics: Metrics, name: str) -> None:
        self._metrics = metrics
        self._name = name
        self.elapsed_ms = 0.0

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._metrics.observe(self._name, self.elapsed_ms)
