"""LocalAgreement-2 假說穩定化。見 ARCHITECTURE.md §8。

暫定稿是「同一段還在成長的音訊，每隔一段時間整段重新解碼一次」，每次
重新解碼都可能跟上一次不完全一樣（尤其是接近音訊尾端、還沒說完的部分）。
LocalAgreement-2 的做法：只有連續兩次解碼都一致的前綴才視為「穩定」，
UI 只把穩定前綴以外的部分標記成「還會變」的樣式（見 §8 的例子）。

比對粒度是可調的（word 或 char）——日文/泰文沒有空格分詞，必須用字元層級
比對，見 §8「泰文特例」與語言包的 `stabilizer.granularity` 欄位（§13.2）。
"""

from __future__ import annotations

from typing import Literal

Granularity = Literal["word", "char"]


class Stabilizer:
    """一個 Stabilizer 實例對應一個 utterance；utterance 收句後要建立新的
    實例（或呼叫 `reset()`），不能跨 utterance 沿用狀態——不同句子的文字
    没有「共同前綴」的意義。
    """

    def __init__(self, granularity: Granularity = "word") -> None:
        self.granularity = granularity
        self._prev_tokens: list[str] | None = None

    def reset(self) -> None:
        self._prev_tokens = None

    def update(self, text: str) -> int:
        """餵入這一輪重新解碼出的完整文字，回傳目前的穩定前綴長度（字元數）。

        第一次呼叫（該 utterance的第一次解碼）永遠回傳 0——LocalAgreement-2
        需要「連續兩次」才能比較，只有一次解碼時沒有東西可以比對，全部視為
        還不穩定。
        """
        tokens = self._tokenize(text)

        if self._prev_tokens is None:
            self._prev_tokens = tokens
            return 0

        common_len = self._common_prefix_len(self._prev_tokens, tokens)
        self._prev_tokens = tokens

        stable_text = self._join(tokens[:common_len])
        return len(stable_text)

    def _tokenize(self, text: str) -> list[str]:
        if self.granularity == "char":
            return list(text)
        # 簡化的詞切分：按空格分。真正語言學意義上的詞邊界（例如英文的
        # 標點附著）留給之後有實際問題再處理，這裡先求「詞層級比對前綴」
        # 這個機制本身正確。
        return text.split(" ")

    def _join(self, tokens: list[str]) -> str:
        return "".join(tokens) if self.granularity == "char" else " ".join(tokens)

    @staticmethod
    def _common_prefix_len(a: list[str], b: list[str]) -> int:
        n = min(len(a), len(b))
        i = 0
        while i < n and a[i] == b[i]:
            i += 1
        return i
