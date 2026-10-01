"""使用者資料目錄。所有「使用者自己的東西」（匯入的語言包、設定、匯出檔的預設
位置、首次啟動標記）都放在這裡，不放在程式目錄——程式目錄可能唯讀（安裝在
Program Files）、也會隨升級被整個換掉。`SAS_USER_DATA_DIR` 可覆寫（測試用）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def user_data_dir() -> Path:
    override = os.environ.get("SAS_USER_DATA_DIR")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "SystemAudioSubtitle"


def user_langpacks_dir() -> Path:
    return user_data_dir() / "langpacks"


def models_dir() -> Path:
    """模型資料夾（VAD onnx、NLLB）。開發時是專案根目錄的 `models/`；PyInstaller
    打包後放在 **exe 旁邊**（不在包內：模型好幾 GB、升級也不該重新打包）。
    `SAS_MODELS_DIR` 可覆寫。"""
    override = os.environ.get("SAS_MODELS_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    return Path(__file__).resolve().parent.parent / "models"
