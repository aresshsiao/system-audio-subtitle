"""Translator 的 NLLB-200 (CTranslate2) 實作。見 ARCHITECTURE.md §9。

**這支模組需要 `transformers`（連帶需要 `torch` 能被 import，見下方說明），
是整個專案除了 `scripts/setup_models.py` 之外唯一的例外**：`transformers`
這個版本的 `__init__` 內部無條件 `import torch`（不是延遲載入），沒辦法
只用 tokenizer 功能又完全避開 torch。這跟 audio-service 刻意避開 torch
是兩回事——audio-service 是延遲敏感的純 CPU 進程（見 utils/gpu.py 附近
的說明），這裡是 inference-service，本來就要載入 GPU 上的 ASR 模型、
啟動要花好幾秒，多一個 torch import 的一次性開銷不影響翻譯本身的延遲
（tokenize/translate/detokenize 這條熱路徑完全不會用到 torch 的運算，
torch 只是 transformers 套件結構上甩不掉的 import-time 形式要求）。
"""

from __future__ import annotations

from pathlib import Path

from utils.gpu import ensure_cuda_dll_path

ensure_cuda_dll_path()

import ctranslate2
from transformers import AutoTokenizer

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "nllb-200-distilled-600M-ct2"
NLLB_HF_TOKENIZER_NAME = "facebook/nllb-200-distilled-600M"


class Ct2NllbTranslator:
    """一個實例對應一個載入好的 NLLB CTranslate2 模型 + tokenizer，語言
    代碼在每次呼叫 `translate()` 時指定（同一個模型支援 NLLB 的全部
    200 種語言，不需要每個語言方向各自載入一份）。
    """

    def __init__(
        self,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        *,
        device: str = "cuda",
        compute_type: str = "int8_float16",
    ) -> None:
        model_dir = Path(model_dir)
        if not (model_dir / "model.bin").is_file():
            raise FileNotFoundError(
                f"找不到轉換好的 NLLB 模型: {model_dir}\n"
                f"先跑 `.venv/Scripts/python.exe scripts/setup_models.py` 轉換"
            )
        self._translator = ctranslate2.Translator(
            str(model_dir), device=device, compute_type=compute_type
        )
        # tokenizer 的 src_lang 參數只影響「編碼時要不要在開頭加語言代碼」，
        # 這裡每次呼叫都自己組 token 序列、自己指定 src_lang token，不依賴
        # tokenizer 物件記住的 src_lang，所以只需要載入一次、不用因為語言
        # 方向不同重新建立 tokenizer。
        self._tokenizer = AutoTokenizer.from_pretrained(NLLB_HF_TOKENIZER_NAME)

    def translate(self, text: str, *, src_code: str, tgt_code: str) -> str:
        if not text.strip():
            return ""

        source_tokens = [src_code, *self._tokenizer.tokenize(text), "</s>"]
        results = self._translator.translate_batch(
            [source_tokens],
            target_prefix=[[tgt_code]],
        )
        # 輸出的第一個 token 是 target_prefix 指定的目標語言代碼本身，
        # 要去掉才是真正的譯文 token 序列。
        output_tokens = results[0].hypotheses[0][1:]
        return self._tokenizer.convert_tokens_to_string(output_tokens).strip()
