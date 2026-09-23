"""ASREngine 抽象。見 ARCHITECTURE.md §9 風險表：ASR 引擎的替換（
faster-whisper → 其他）只要遵守這個介面，`inference/pipeline.py`（M3）
與 `stabilizer.py` 都不需要跟著改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: str  # BCP-47（或至少是 ASR 引擎慣用的語言代碼），偵測或指定的來源語言
    no_speech_prob: float  # 見 inference/asr/hallucination.py


class ASREngine(Protocol):
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        beam_size: int = 1,
        initial_prompt: str | None = None,
    ) -> ASRResult:
        """`audio`：float32、16kHz、mono、shape=(N,)。

        `language=None` 交給引擎自動偵測；`initial_prompt` 用於定稿階段帶
        前文語境（見 ARCHITECTURE.md §8 兩段式字幕）。
        """
        ...
