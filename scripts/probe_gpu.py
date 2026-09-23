"""M0 環境驗證：確認 ctranslate2 能看到 GPU 與需要的 compute type。

跑法：.venv/Scripts/python.exe scripts/probe_gpu.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 主控台預設非 UTF-8，避免中文亂碼

from utils.gpu import ensure_cuda_dll_path


def main() -> int:
    found_dlls = ensure_cuda_dll_path()
    print(f"nvidia-cublas / nvidia-cudnn DLL 目錄找到: {found_dlls}")

    import ctranslate2

    print(f"ctranslate2 version: {ctranslate2.__version__}")
    device_count = ctranslate2.get_cuda_device_count()
    print(f"CUDA device count: {device_count}")

    if device_count == 0:
        print("!! 沒有偵測到 CUDA 裝置 —— 會退回 CPU，延遲預算(ARCHITECTURE.md §10)不成立")
        return 1

    compute_types = ctranslate2.get_supported_compute_types("cuda")
    print(f"支援的 compute types: {sorted(compute_types)}")

    required = {"int8_float16"}
    missing = required - compute_types
    if missing:
        print(f"!! 缺少必要 compute type: {missing}")
        return 1

    print("OK — GPU 環境符合 ARCHITECTURE.md 假設 (int8_float16 可用)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
