"""Spike A（ROADMAP.md M0）+ 正式版 scripts/bench_latency.py。

目標：驗證 ARCHITECTURE.md §10 延遲預算表的假設是否成立，量到「每一格」
而不是只有端到端數字。第一版先量 ASR 解碼本身（暫定稿 beam=1、定稿 beam=5），
之後 M3 接上翻譯層後補上 MT 那幾格。

跑法：.venv/Scripts/python.exe scripts/bench_latency.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 一般使用者帳號（非系統管理員 / 未開發者模式）在 Windows 上沒有建立符號連結的
# 權限，huggingface_hub 預設用 symlink 佈置快取會直接炸掉。改用複製檔案，
# 犧牲一點硬碟空間換取「不需要使用者去開發者模式」。
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from utils.gpu import ensure_cuda_dll_path

ensure_cuda_dll_path()

from faster_whisper import WhisperModel  # noqa: E402  (要在 ensure_cuda_dll_path 之後 import)

FIXTURES = ROOT / "tests" / "fixtures"
N_RUNS = 30
WARMUP_RUNS = 3


def percentile(values: list[float], p: float) -> float:
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * p
    f, c = int(k), min(int(k) + 1, len(values_sorted) - 1)
    if f == c:
        return values_sorted[f]
    return values_sorted[f] + (values_sorted[c] - values_sorted[f]) * (k - f)


def bench_decode(
    model: WhisperModel, wav_path: Path, *, beam_size: int, label: str, n_runs: int = N_RUNS
) -> list[float]:
    durations_ms: list[float] = []

    for i in range(WARMUP_RUNS + n_runs):
        t0 = time.perf_counter()
        segments, _info = model.transcribe(
            str(wav_path),
            beam_size=beam_size,
            language="en",
            vad_filter=False,  # Spike A 只量解碼本身，VAD 開銷另外量
            condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments)  # noqa: F841  (強制消費 generator)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if i >= WARMUP_RUNS:
            durations_ms.append(elapsed_ms)

    p50 = percentile(durations_ms, 0.50)
    p95 = percentile(durations_ms, 0.95)
    p99 = percentile(durations_ms, 0.99)
    mean = statistics.mean(durations_ms)
    print(
        f"[{label}] n={n_runs}  mean={mean:6.1f}ms  p50={p50:6.1f}ms  "
        f"p95={p95:6.1f}ms  p99={p99:6.1f}ms  min={min(durations_ms):6.1f}ms  max={max(durations_ms):6.1f}ms"
    )
    return durations_ms


def wav_duration_seconds(wav_path: Path) -> float:
    import wave

    with wave.open(str(wav_path), "rb") as f:
        return f.getnframes() / f.getframerate()


def main() -> int:
    short_wav = FIXTURES / "spike_a_short.wav"  # ~1.4s，模擬暫定稿滑動視窗
    typical_wav = FIXTURES / "spike_a_typical.wav"  # ~5.1s，典型一句話的長度
    long_wav = FIXTURES / "spike_a_sample.wav"  # ~8.6s，接近 12s 強制斷句上限
    for p in (short_wav, typical_wav, long_wav):
        if not p.is_file():
            print(f"!! 找不到測試音檔: {p}")
            return 1

    print("載入 faster-whisper large-v3 (int8_float16, cuda)...")
    model = WhisperModel("large-v3", device="cuda", compute_type="int8_float16")
    print("模型載入完成，開始量測\n")

    print("=== 暫定稿場景：beam_size=1，短句 (~1.4s) ===")
    draft_durations = bench_decode(model, short_wav, beam_size=1, label="draft beam=1")

    print("\n=== 定稿場景：beam_size=5，典型長度 (~5.1s) ===")
    final_typical_durations = bench_decode(
        model, typical_wav, beam_size=5, label="final beam=5 typical"
    )

    print("\n=== 定稿場景（worst case）：beam_size=5，接近 12s 強制斷句上限 (~8.6s) ===")
    final_worst_durations = bench_decode(
        model, long_wav, beam_size=5, label="final beam=5 worst"
    )

    print("\n=== 對照組：worst case 但 beam_size=1（量 beam search 本身的開銷）===")
    beam1_long = bench_decode(model, long_wav, beam_size=1, label="long  beam=1")

    # RTF：解碼耗時 / 音訊長度。< 1.0 代表比即時快，數字越小代表 GPU 還有餘裕。
    long_duration_s = wav_duration_seconds(long_wav)
    rtf_beam1 = statistics.median(beam1_long) / 1000 / long_duration_s
    rtf_beam5 = statistics.median(final_worst_durations) / 1000 / long_duration_s
    print(f"\nRTF (beam=1, {long_duration_s:.1f}s 音訊): {rtf_beam1:.3f}")
    print(f"RTF (beam=5, {long_duration_s:.1f}s 音訊): {rtf_beam5:.3f}")
    print(
        "解讀：RTF 遠小於 1 代表解碼耗時大部分是固定開銷（特徵擷取/encoder），"
        "不是隨音訊長度線性增加 —— 這代表典型長度的句子比 worst-case 快得不成比例，"
        "不能只用 worst-case 數字去否定整個延遲預算。"
    )

    print("\n--- ARCHITECTURE.md §10 對照 ---")
    draft_p95 = percentile(draft_durations, 0.95)
    final_typical_p95 = percentile(final_typical_durations, 0.95)
    final_worst_p95 = percentile(final_worst_durations, 0.95)
    print(f"暫定稿 ASR p95:            {draft_p95:.1f}ms  (預算表估計: 120-250ms)")
    print(f"定稿 ASR p95 (典型長度):   {final_typical_p95:.1f}ms  (預算表估計: 250-400ms)")
    print(f"定稿 ASR p95 (worst case): {final_worst_p95:.1f}ms  (無明確預算，僅供參考)")

    budget_draft_ok = draft_p95 <= 250
    budget_final_ok = final_typical_p95 <= 400
    print(f"暫定稿 ASR 是否達標: {'OK' if budget_draft_ok else 'FAIL -- 需要下修預算或改用 distil 模型'}")
    print(f"定稿   ASR 是否達標: {'OK' if budget_final_ok else 'FAIL -- 需要下修預算或改用 distil 模型'}")
    if final_worst_p95 > 400:
        print(
            f"注意: worst-case (~12s) 定稿 p95={final_worst_p95:.1f}ms 超出預算表 400ms 上限，"
            "長句仍建議在 degrade.py L1（beam 5→1）介入，見 ARCHITECTURE.md §11"
        )

    return 0 if (budget_draft_ok and budget_final_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
