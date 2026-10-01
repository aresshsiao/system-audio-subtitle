"""runtime/preflight.py：判斷邏輯（外部依賴全部注入）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.preflight import (
    Status,
    check_audio,
    check_gpu,
    check_langpacks,
    check_models,
    check_ports,
    check_python,
    run_preflight,
    summarize,
)


def by_name(results, name):
    return next(r for r in results if r.name == name)


def test_python_and_os() -> None:
    assert check_python((3, 13), "win32").status == Status.OK
    assert check_python((3, 9), "win32").status == Status.FAIL
    r = check_python((3, 13), "linux")
    assert r.status == Status.FAIL and "Windows" in r.fix


def test_gpu_ok_low_total_low_free_and_missing() -> None:
    ok = check_gpu(lambda: "NVIDIA GeForce RTX 4070, 12282, 9000, 555.85\n")
    assert [r.status for r in ok] == [Status.OK, Status.OK]

    small = check_gpu(lambda: "GTX 1650, 4096, 3500, 555.85")
    assert small[1].status == Status.WARN and "4096" in small[1].detail

    busy = check_gpu(lambda: "RTX 4070, 12282, 1200, 555.85")
    assert busy[1].status == Status.WARN and "遊戲" in busy[1].fix  # 給出可行動的建議

    def no_smi():
        raise FileNotFoundError("nvidia-smi")

    missing = check_gpu(no_smi)
    assert missing[0].status == Status.FAIL and "驅動" in missing[0].fix

    def timeout():
        raise subprocess.TimeoutExpired("nvidia-smi", 10)

    assert check_gpu(timeout)[0].status == Status.FAIL
    assert check_gpu(lambda: "garbage")[0].status == Status.FAIL


def test_models_distinguish_fail_from_first_run_warn(tmp_path: Path) -> None:
    models, hf = tmp_path / "models", tmp_path / "hf"
    models.mkdir()
    empty = check_models(models, hf)
    assert by_name(empty, "VAD 模型").status == Status.FAIL
    assert by_name(empty, "翻譯模型 (NLLB)").status == Status.FAIL and "setup_models" in by_name(empty, "VAD 模型").fix
    # Whisper 沒快取只是「第一次啟動會下載」，不是壞掉
    assert by_name(empty, "語音辨識模型 (Whisper large-v3)").status == Status.WARN
    assert by_name(empty, "降級備援模型 (L4)").status == Status.WARN

    (models / "silero_vad.onnx").write_bytes(b"x")
    (models / "nllb-200-distilled-600M-ct2").mkdir()
    (models / "nllb-200-distilled-600M-ct2" / "model.bin").write_bytes(b"x")
    snap = hf / "models--Systran--faster-whisper-large-v3" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    full = check_models(models, hf)
    assert by_name(full, "VAD 模型").status == Status.OK
    assert by_name(full, "翻譯模型 (NLLB)").status == Status.OK
    assert by_name(full, "語音辨識模型 (Whisper large-v3)").status == Status.OK


def test_audio_devices() -> None:
    assert check_audio(lambda: 3).status == Status.OK
    assert check_audio(lambda: 0).status == Status.FAIL

    def boom():
        raise OSError("no audio")

    assert check_audio(boom).status == Status.FAIL


def test_ports() -> None:
    assert check_ports(lambda p: True, [1, 2]).status == Status.OK
    r = check_ports(lambda p: p != 2, [1, 2])
    assert r.status == Status.WARN and "[2]" in r.detail


def test_langpacks_builtin_available() -> None:
    assert check_langpacks().status == Status.OK


def test_summary_takes_worst_status() -> None:
    from runtime.preflight import CheckResult

    ok, warn, fail = (CheckResult("a", s, "") for s in (Status.OK, Status.WARN, Status.FAIL))
    assert summarize([ok]) == Status.OK
    assert summarize([ok, warn]) == Status.WARN
    assert summarize([ok, warn, fail]) == Status.FAIL


def test_run_preflight_on_this_machine_returns_named_results_without_crashing() -> None:
    results = run_preflight()
    assert len(results) >= 8 and all(r.name and r.detail for r in results)
    assert all(r.fix for r in results if r.status != Status.OK)  # 有問題的每一項都要說怎麼修
