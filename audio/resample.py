"""任意取樣率 → 16k mono float32，支援串流（分批呼叫）不產生邊界爆音。

見 ARCHITECTURE.md §10：這一段的延遲預算是 < 3ms，所以不能對每一小批
（WASAPI callback 通常 10ms 一批）獨立呼叫 `scipy.signal.resample_poly`——
多相濾波器（FIR）在每次獨立呼叫的頭尾都有邊界失真，連續呼叫會在每個
batch 的接縫處產生可聽見的爆音，且各自獨立呼叫也拿不到「這批訊號其實是
上一批的延續」這個資訊。

作法：`StreamResampler` 保留前一批「輸入端」的尾巴幾個樣本當作歷史脈絡，
跟這一批新樣本一起餵給 `resample_poly`，再把對應「歷史脈絡」那段的輸出
丟掉，只留下真正屬於這一批新資料的輸出——這是分批做 FIR 重取樣的標準
技巧（overlap 只在輸入端，不需要在輸出端做重疊相加）。
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

TARGET_SAMPLE_RATE = 16000

# 歷史脈絡要留多少個「輸入端」樣本。這個數字是經驗值：太小邊界效應蓋不掉，
# 太大則每批呼叫的計算量都白白多花在重算舊資料上。32 個輸入樣本在常見的
# 44.1k/48k → 16k 轉換比例下，換算成濾波器脈衝響應的量級綽綽有餘。
_HISTORY_INPUT_SAMPLES = 32


def _reduced_up_down(src_rate: int, dst_rate: int) -> tuple[int, int]:
    frac = Fraction(dst_rate, src_rate).limit_denominator(1000)
    return frac.numerator, frac.denominator


def to_mono(samples: np.ndarray) -> np.ndarray:
    """(N,) 維持原樣；(N, C) 多聲道取平均混成單聲道。"""
    if samples.ndim == 1:
        return samples
    if samples.ndim == 2:
        return samples.mean(axis=1)
    raise ValueError(f"不支援的音訊陣列維度: {samples.ndim}")


class StreamResampler:
    """分批餵任意取樣率的音訊，回傳連續、無邊界爆音的 16kHz mono float32。

    同一個實例的呼叫之間有狀態（保留輸入端歷史），**不可以**跨不同音源
    共用同一個實例——換音源（例如切換擷取目標）要建立新的 StreamResampler，
    或呼叫 `reset()`。
    """

    def __init__(self, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError(f"取樣率必須是正數: src={src_rate}, dst={dst_rate}")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._up, self._down = _reduced_up_down(src_rate, dst_rate)
        self.reset()

    def reset(self) -> None:
        """換音源、或偵測到不連續（例如裝置重新開啟）時呼叫，清掉歷史脈絡。"""
        self._history = np.zeros(_HISTORY_INPUT_SAMPLES, dtype=np.float32)

    def push(self, samples: np.ndarray) -> np.ndarray:
        """餵一批任意取樣率的音訊（可為 (N,) 或 (N, C)），回傳 16kHz mono float32。

        回傳長度並非精確等於「理論上這批樣本轉換後該有幾個」——多相濾波器
        的取捨（`limit_denominator`、四捨五入）會讓長度有 ±1、±2 個樣本的
        誤差，呼叫端（segmenter/ringbuffer）不應該假設嚴格對齊，只在乎
        音訊內容連續、取樣率正確。
        """
        mono = to_mono(np.asarray(samples, dtype=np.float32))
        if len(mono) == 0:
            return np.empty(0, dtype=np.float32)

        combined = np.concatenate([self._history, mono])
        resampled = resample_poly(combined, self._up, self._down).astype(np.float32)

        # 歷史脈絡那段輸入，對應輸出端大約這麼多個樣本，丟掉不要。第一次呼叫
        # 時歷史脈絡是補的 0 而非真正的前情，但一樣要丟——那段本來就是濾波器
        # 的啟動失真區，丟掉才能讓輸出從乾淨的狀態開始，不需要為此特判。
        history_out_samples = round(_HISTORY_INPUT_SAMPLES * self._up / self._down)
        out = resampled[history_out_samples:]

        # 更新歷史脈絡為這一批「輸入端」的尾巴。
        if len(mono) >= _HISTORY_INPUT_SAMPLES:
            self._history = mono[-_HISTORY_INPUT_SAMPLES:].copy()
        else:
            self._history = np.concatenate([self._history, mono])[-_HISTORY_INPUT_SAMPLES:]

        return out
