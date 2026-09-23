"""ASREngine 的 faster-whisper 實作。見 ARCHITECTURE.md §10 Spike A 實測數字。

**必須先 import `utils.gpu` 並呼叫 `ensure_cuda_dll_path()`，再 import
`faster_whisper`/`ctranslate2`**——Windows 上單靠 PATH 環境變數不夠，
需要 `os.add_dll_directory()` 才能正確載入 cuBLAS/cuDNN，見 utils/gpu.py
的說明與 M0 Spike A 實際踩到的坑。這裡在模組頂層就做，確保這支模組
不管被誰 import，順序都一定正確。
"""

from __future__ import annotations

from utils.gpu import ensure_cuda_dll_path

ensure_cuda_dll_path()

import numpy as np
from faster_whisper import WhisperModel

from inference.asr.base import ASRResult


class FasterWhisperEngine:
    """見 ARCHITECTURE.md §8：暫定稿用 `beam_size=1`，定稿用 `beam_size=5` +
    `initial_prompt` 帶前文語境。這兩種呼叫方式都透過同一個 `transcribe()`，
    差別只在呼叫端傳的參數。
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        *,
        device: str = "cuda",
        compute_type: str = "int8_float16",
    ) -> None:
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        initial_prompt: str | None = None,
    ) -> ASRResult:
        segments_iter, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            initial_prompt=initial_prompt,
            # 我們自己在 audio-service 用 Silero VAD 做過段落切分了
            # （見 audio/segmenter.py），這裡不重複做一次 VAD 濾波。
            vad_filter=False,
            # 暫定稿/定稿都是各自獨立重新解碼同一段累積音訊（見 §8 的
            # 滑動視窗設計），不該讓 Whisper 自己去接續「上一次呼叫」的
            # 文字語境——那是不同 utterance 之間的概念，會混淆。
            condition_on_previous_text=False,
        )
        segments = list(segments_iter)

        text = "".join(seg.text for seg in segments).strip()
        # 沒有任何 segment 時，通常代表整段音訊被判定為靜音/雜訊——
        # 用 1.0（幾乎確定是靜音）當保守預設，讓 hallucination.py 能濾掉。
        no_speech_prob = max((seg.no_speech_prob for seg in segments), default=1.0)
        detected_language = language if language is not None else info.language

        return ASRResult(text=text, language=detected_language, no_speech_prob=no_speech_prob)
