"""launcher.py 與 supervisor 的啟動指令。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from runtime import supervisor

ROOT = Path(__file__).resolve().parent.parent


def test_command_for_module_normal_and_frozen(monkeypatch) -> None:
    assert supervisor.command_for_module("audio.service") == [sys.executable, "-m", "audio.service"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert supervisor.command_for_module("audio.service") == [sys.executable, "--module", "audio.service"]


def test_launcher_module_mode_runs_target_like_python_dash_m() -> None:
    """`--module` 是打包後 Supervisor 啟動各服務的方式，要跟 `python -m` 行為一致
    （`__name__ == "__main__"`、能收到參數）。"""
    code = (
        "import sys\n"
        "from launcher import main\n"
        "raise SystemExit(main(['--module', 'tests._launcher_probe', 'x']))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()  # 跟 `python -m` 一樣，argv[0] 會是模組檔案路徑
    assert out.startswith("main:") and out.endswith("_launcher_probe.py', 'x']")
