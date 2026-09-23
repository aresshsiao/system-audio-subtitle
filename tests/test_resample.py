"""audio/resample.py 的測試。

重點不是「有沒有跑」，是「取樣率轉換後頻率內容有沒有跑掉」跟「分批餵
跟一次餵有沒有明顯落差」——後者才是 StreamResampler 存在的理由，見模組
docstring 的爆音說明。
"""

from __future__ import annotations

import numpy as np
import pytest

from audio.resample import StreamResampler, to_mono


def sine(freq: float, duration_s: float, sample_rate: int) -> np.ndarray:
    t = np.arange(int(duration_s * sample_rate)) / sample_rate
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def dominant_frequency(signal: np.ndarray, sample_rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(signal))
    freqs = np.fft.rfftfreq(len(signal), d=1 / sample_rate)
    return float(freqs[np.argmax(spectrum)])


def test_to_mono_passes_through_1d() -> None:
    x = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    np.testing.assert_array_equal(to_mono(x), x)


def test_to_mono_averages_stereo() -> None:
    stereo = np.array([[1.0, 3.0], [2.0, 4.0]], dtype=np.float32)
    out = to_mono(stereo)
    np.testing.assert_allclose(out, [2.0, 3.0])


def test_to_mono_rejects_bad_ndim() -> None:
    with pytest.raises(ValueError):
        to_mono(np.zeros((2, 2, 2), dtype=np.float32))


@pytest.mark.parametrize("src_rate", [48000, 44100])
def test_output_rate_matches_target_length(src_rate: int) -> None:
    """1 秒的輸入，轉出來的長度應該接近 1 秒的 16kHz（允許濾波器邊界誤差）。"""
    resampler = StreamResampler(src_rate=src_rate, dst_rate=16000)
    audio = sine(440, 1.0, src_rate)
    out = resampler.push(audio)

    expected_len = 16000
    assert abs(len(out) - expected_len) < 200, f"長度偏差過大: {len(out)} vs {expected_len}"


@pytest.mark.parametrize("src_rate", [48000, 44100])
def test_frequency_preserved_after_resample(src_rate: int) -> None:
    """440Hz 純音經過重取樣，主頻率應該還是接近 440Hz，不是跑到別的頻率。"""
    resampler = StreamResampler(src_rate=src_rate, dst_rate=16000)
    audio = sine(440, 2.0, src_rate)
    out = resampler.push(audio)

    freq = dominant_frequency(out, 16000)
    assert abs(freq - 440) < 5, f"重取樣後主頻率偏移過多: {freq}Hz"


def test_chunked_streaming_matches_oneshot_closely() -> None:
    """分成一堆小批餵，跟一次整段餵，結果應該高度一致——這是 StreamResampler
    存在的核心理由：分批呼叫不該在接縫處產生明顯偏差。"""
    src_rate = 48000
    audio = sine(300, 1.0, src_rate) * 0.5 + sine(1000, 1.0, src_rate) * 0.3

    oneshot = StreamResampler(src_rate).push(audio)

    chunked_resampler = StreamResampler(src_rate)
    chunk_size = 480  # 10ms @ 48kHz，模擬真實 WASAPI callback 的批次大小
    chunks_out = []
    for i in range(0, len(audio), chunk_size):
        chunks_out.append(chunked_resampler.push(audio[i : i + chunk_size]))
    chunked = np.concatenate(chunks_out)

    n = min(len(oneshot), len(chunked))
    # 掐頭去尾各 50 個樣本再比較：極頭極尾一小段濾波器邊界效應的殘留差異
    # 不是我們要驗證的重點，中段的連續性才是。
    margin = 50
    rmse = np.sqrt(np.mean((oneshot[margin : n - margin] - chunked[margin : n - margin]) ** 2))
    assert rmse < 0.02, f"分批餵跟一次餵的差異過大 (RMSE={rmse:.4f})，可能有邊界爆音"


def test_reset_clears_history_state() -> None:
    src_rate = 48000
    resampler = StreamResampler(src_rate)
    resampler.push(sine(1000, 0.5, src_rate))  # 累積一些歷史脈絡
    resampler.reset()

    fresh = StreamResampler(src_rate)
    audio = sine(440, 0.3, src_rate)
    out_after_reset = resampler.push(audio)
    out_fresh = fresh.push(audio)

    np.testing.assert_allclose(out_after_reset, out_fresh, atol=1e-6)


def test_rejects_nonpositive_sample_rate() -> None:
    with pytest.raises(ValueError):
        StreamResampler(src_rate=0)
    with pytest.raises(ValueError):
        StreamResampler(src_rate=48000, dst_rate=-16000)


def test_empty_input_returns_empty_output() -> None:
    resampler = StreamResampler(48000)
    out = resampler.push(np.empty(0, dtype=np.float32))
    assert len(out) == 0
