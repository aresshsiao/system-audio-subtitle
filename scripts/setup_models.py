"""下載並準備模型權重到 `models/`（`.gitignore` 排除，每台機器要跑一次）。

ASR（faster-whisper）不需要這支腳本處理——它自己會在 inference-service
第一次使用時透過 huggingface_hub 快取。這裡處理的是：
  - VAD 的 onnx 模型（M1）
  - NLLB-200 翻譯模型轉成 CTranslate2 int8（M3，見 §9）

跑法：.venv/Scripts/python.exe scripts/setup_models.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

SILERO_VAD_VERSION = "6.2.2"  # 要跟 requirements.txt 的 pip 版本標記一致（供人對照，非硬依賴）
SILERO_VAD_WHEEL_MEMBER = "silero_vad/data/silero_vad.onnx"  # opset 16，8k+16k 合併模型


def fetch_silero_vad_onnx() -> Path:
    """只取出 silero-vad wheel 裡的 .onnx 模型檔，不安裝整個套件。

    刻意不把 `silero-vad` 加進 requirements.txt 當執行期依賴——它的 pip
    metadata 強制要求 `torch<2.10`，會把我們釘死的 torch==2.14.0 降版，
    且 audio-service 這種要求低延遲、高優先權的純 CPU 進程不該背 torch
    的 import 開銷。VAD 推論實際上只需要 onnxruntime + numpy（見
    audio/vad.py），這裡用 `pip download --no-deps` 只把 wheel 抓下來，
    從裡面挖出 .onnx 檔案，wheel 本身丟掉。
    """
    dest = MODELS_DIR / "silero_vad.onnx"
    if dest.is_file():
        print(f"已存在，略過: {dest}")
        return dest

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        print(f"下載 silero-vad=={SILERO_VAD_VERSION} wheel（僅取用 .onnx，不裝 torch 依賴）...")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                f"silero-vad=={SILERO_VAD_VERSION}",
                "--no-deps",
                "-d",
                str(tmp_path),
            ],
            check=True,
        )

        wheels = list(tmp_path.glob("silero_vad-*.whl"))
        if not wheels:
            raise FileNotFoundError("pip download 沒有產出預期的 silero_vad wheel 檔")

        with zipfile.ZipFile(wheels[0]) as z:
            with z.open(SILERO_VAD_WHEEL_MEMBER) as src, open(dest, "wb") as out:
                out.write(src.read())

    print(f"完成: {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    return dest


NLLB_HF_MODEL = "facebook/nllb-200-distilled-600M"
NLLB_QUANTIZATION = "int8"  # 見 ARCHITECTURE.md §9：本地即時翻譯，短句 < 100ms


def convert_nllb_to_ctranslate2() -> Path:
    """把 NLLB-200-distilled-600M 從 HuggingFace 轉成 CTranslate2 int8。

    這是一次性、離線的轉換步驟（跟 ASR 模型不一樣，faster-whisper 直接
    吃官方已經轉好的 CTranslate2 模型，但 NLLB 沒有官方預轉版本，要自己
    用 `ct2-transformers-converter` 轉）。轉換過程需要 `transformers` +
    `torch`（CPU 版即可，見 requirements.txt 的說明——這是唯一會用到
    torch 的地方，執行期的 inference-service 完全不需要 torch）。
    """
    dest = MODELS_DIR / "nllb-200-distilled-600M-ct2"
    if (dest / "model.bin").is_file():
        print(f"已存在，略過: {dest}")
        return dest

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"轉換 {NLLB_HF_MODEL} → CTranslate2 {NLLB_QUANTIZATION}（第一次跑會從 HF 下載原始權重，較久）...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ctranslate2.converters.transformers",
            "--model",
            NLLB_HF_MODEL,
            "--output_dir",
            str(dest),
            "--quantization",
            NLLB_QUANTIZATION,
        ],
        check=True,
        env={**__import__("os").environ, "HF_HUB_DISABLE_SYMLINKS": "1"},
    )
    print(f"完成: {dest}")
    return dest


def main() -> int:
    fetch_silero_vad_onnx()
    convert_nllb_to_ctranslate2()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
