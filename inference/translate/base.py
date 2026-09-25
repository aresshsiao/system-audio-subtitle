"""Translator 抽象。見 ARCHITECTURE.md §9：本地/雲端引擎替換要透過這個
介面，`pipeline.py` 不該知道底下是 NLLB 還是 LLM API。
"""

from __future__ import annotations

from typing import Protocol


class Translator(Protocol):
    def translate(self, text: str, *, src_code: str, tgt_code: str) -> str:
        """`src_code`/`tgt_code` 是該引擎自己慣用的語言代碼（NLLB 用
        FLORES-200 代碼如 `jpn_Jpan`），由語言包的
        `translate.src_code`/`tgt_code` 提供，這支介面不解讀代碼格式。
        """
        ...
