"""audio/vad.py 的測試，用真實語音檔而不是合成訊號。

用 Spike A 留下的 TTS 語音檔（tests/fixtures/spike_a_sample.wav）—— 真人
語音的頻譜特徵跟白噪音/純音完全不同，Silero VAD 是在真實語音上訓練的，
用假訊號測「有沒有正確分辨語音」沒有意義，容易測出偽陽性的安心感。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audio.vad import FRAME_SAMPLES, SAMPLE_RATE, SileroVAD, VadStream

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEECH_WAV = FIXTURES / "spike_a_sample.wav"

pytestmark = pytest.mark.skipif(
    not SPEECH_WAV.is_file(), reason="需要 tests/fixtures/spike_a_sample.wav（M0 Spike A 產物）"
)


def load_speech() -> np.ndarray:
    data, sr = sf.read(SPEECH_WAV, dtype="float32")
    assert sr == SAMPLE_RATE, f"測試音檔取樣率應該是 {SAMPLE_RATE}，實際是 {sr}"
    return data


def test_speech_gets_high_probability() -> None:
    vad = VadStream()
    probs = vad.push(load_speech())

    assert len(probs) > 0
    mean_prob = sum(probs) / len(probs)
    assert mean_prob > 0.7, f"真人語音的平均機率應該明顯偏高，實際 {mean_prob:.3f}"


def test_silence_gets_low_probability() -> None:
    silence = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
    vad = VadStream()
    probs = vad.push(silence)

    assert len(probs) > 0
    assert max(probs) < 0.1, f"純靜音不該被判定為語音，實際 max={max(probs):.3f}"


def test_speech_clearly_separable_from_silence() -> None:
    """這是 ARCHITECTURE.md §7 幻覺過濾的前提：語音跟靜音的機率差距要夠大，
    才能用一個門檻值可靠地分開兩者。"""
    speech_probs = VadStream().push(load_speech())
    silence_probs = VadStream().push(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))

    speech_mean = sum(speech_probs) / len(speech_probs)
    silence_mean = sum(silence_probs) / len(silence_probs)
    assert speech_mean - silence_mean > 0.5


def test_push_handles_arbitrary_chunk_sizes_not_just_frame_multiples() -> None:
    """呼叫端（segmenter）不該被迫知道 512 這個數字，任意長度都要能餵。"""
    speech = load_speech()
    vad = VadStream()

    all_probs: list[float] = []
    # 故意用不是 FRAME_SAMPLES 倍數的怪異切法餵進去
    chunk_size = 333
    for i in range(0, len(speech), chunk_size):
        all_probs.extend(vad.push(speech[i : i + chunk_size]))

    # 跟一次全部餵進去的結果比對數量：理論上框數應該一致（allow ±1 因為
    # 尾端不足一框的樣本處理方式可能有一框的差異）
    one_shot_probs = VadStream().push(speech)
    assert abs(len(all_probs) - len(one_shot_probs)) <= 1


def test_reset_clears_recurrent_state() -> None:
    """reset() 後的行為應該跟全新物件一致，不該殘留前一段音訊的遞迴狀態。"""
    speech = load_speech()

    vad = VadStream()
    vad.push(speech)  # 跑過一段，累積狀態
    vad.reset()
    probs_after_reset = vad.push(speech)

    fresh_probs = VadStream().push(speech)

    np.testing.assert_allclose(probs_after_reset, fresh_probs, atol=1e-5)


def test_process_frame_rejects_wrong_size() -> None:
    vad = SileroVAD()
    with pytest.raises(ValueError):
        vad.process_frame(np.zeros(100, dtype=np.float32))


def test_process_frame_accepts_exact_frame_size() -> None:
    vad = SileroVAD()
    prob = vad.process_frame(np.zeros(FRAME_SAMPLES, dtype=np.float32))
    assert 0.0 <= prob <= 1.0
