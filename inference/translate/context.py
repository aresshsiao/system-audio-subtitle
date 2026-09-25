"""前 N 句上下文視窗。見 ARCHITECTURE.md §9：逐句翻譯會丟失代名詞/省略
主詞這類跨句資訊，滾動保留最近幾組已定稿的 (原文, 譯文) 用來給翻譯引擎
更多語境。

**NLLB 是 encoder-decoder 的句子級翻譯模型，不是能吃任意長度 prompt 的
LLM**——沒辦法像雲端 LLM 那樣直接把「前文 + 這句話」丟進去講「請參考
上下文翻譯這句」。這裡用的是輕量的 heuristic：把前幾句的原文接在這句
原文前面一起送進 NLLB 翻譯，輸出出來的文字**用句尾標點切開**，只取最後
一段當作「這句話」的翻譯結果——前面幾句的譯文只是拿來給模型「看」，
換取它對代名詞/指涉的判斷更準，不是真的要顯示出來。

這是近似解法，不是精確解法：NLLB 沒有專門訓練成「輸入幾句、輸出對應
幾句」，句數不一定完全對得上，尤其是原文本身沒有清楚句尾標點時。
ROADMAP.md M3 的驗收標準要求「有上下文 vs 無上下文人工比對」，這支
模組的價值要用那組結果來檢驗，不是假設它一定有效。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

_SENTENCE_END_RE = re.compile(r"(?<=[。．.!?！？])\s*")


@dataclass
class TranslationContext:
    """滾動保留最近 `max_sentences` 組已定稿的 (原文, 譯文)。"""

    max_sentences: int = 5
    _history: deque[tuple[str, str]] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self._history = deque(maxlen=self.max_sentences)

    def push(self, source_text: str, target_text: str) -> None:
        """一句話定稿翻譯完成後呼叫，把它加進滾動視窗。"""
        self._history.append((source_text, target_text))

    def clear(self) -> None:
        self._history.clear()

    def build_input(self, current_source_text: str) -> str:
        """組出「前文 + 這句話」要送進翻譯引擎的完整輸入文字。

        沒有歷史時就是原句本身，呼叫端不需要特判「有沒有上下文」。
        """
        if not self._history:
            return current_source_text
        prior_sources = [src for src, _tgt in self._history]
        return " ".join([*prior_sources, current_source_text])

    def extract_current_translation(self, full_output: str, *, num_context_sentences: int) -> str:
        """從翻譯引擎對「前文+這句話」的完整輸出裡，切出「這句話」對應的
        部分——按句尾標點切開，取最後 `num_context_sentences + 1` 段裡的
        最後一段。

        `num_context_sentences` 通常就是呼叫 `build_input()` 當下
        `len(self._history)`——呼叫端要自己傳，因為這個方法本身是無狀態
        的純函式（方便單獨測試，不用真的先 push 歷史）。
        """
        if num_context_sentences == 0:
            return full_output.strip()

        segments = [s for s in _SENTENCE_END_RE.split(full_output.strip()) if s]
        if not segments:
            return full_output.strip()
        return segments[-1].strip()

    def recent_sources(self, n: int) -> list[str]:
        """最近 n 句的原文（不含譯文），供 ASR 的 initial_prompt 這類
        只需要原文語境、不需要完整 (原文,譯文) 配對的用途。
        """
        return [src for src, _tgt in list(self._history)[-n:]]

    def __len__(self) -> int:
        return len(self._history)
