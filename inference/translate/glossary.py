"""術語表載入與套用。見 ARCHITECTURE.md §9：動畫人名、公司名、技術術語
用強制替換，這是「專屬工具」相對通用服務最大的優勢之一。

**做法是佔位符替換，不是約束解碼**：翻譯前把原文裡出現的術語換成一個
NLLB 不太可能翻譯/改動的佔位符（`§0§`、`§1§`...），送進去翻譯，翻完
再把佔位符換回指定的目標語言譯法。這不是 100% 保證——如果模型還是動了
佔位符本身，替換就會失敗，但這是不用碰模型內部解碼邏輯就能做術語約束
的做法，複雜度跟 M3 的範圍相稱。ROADMAP.md M3 的驗收標準（人名整段
影片譯法一致）要用來檢驗這個做法夠不夠用。

檔案格式：TSV（tab 分隔），每行 `原文詞\t目標語言譯法`，`#` 開頭是註解。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PLACEHOLDER_TEMPLATE = "§{index}§"


@dataclass(frozen=True)
class Glossary:
    """`term -> target_text` 對照表。詞的比對順序按**長度由長到短**，
    避免短詞先比對到把長詞的一部分吃掉（例如同時有 "Alice" 跟
    "Alice Wang" 兩個詞時，要先處理 "Alice Wang"）。
    """

    terms: dict[str, str]

    def apply_placeholders(self, text: str) -> tuple[str, dict[str, str]]:
        """把 text 裡出現的術語換成佔位符，回傳 (替換後的文字, 佔位符→目標譯法對照表)。"""
        mapping: dict[str, str] = {}
        result = text
        for i, term in enumerate(sorted(self.terms, key=len, reverse=True)):
            if term not in result:
                continue
            placeholder = _PLACEHOLDER_TEMPLATE.format(index=i)
            result = result.replace(term, placeholder)
            mapping[placeholder] = self.terms[term]
        return result, mapping

    @staticmethod
    def restore_placeholders(text: str, mapping: dict[str, str]) -> str:
        """把翻譯輸出裡的佔位符換回指定譯法。呼叫端要檢查佔位符是否都還
        在（`all(ph in text for ph in mapping)`）來判斷替換有沒有成功，
        這支方法本身不做這個檢查——沒找到的佔位符就是原樣留著不動。
        """
        result = text
        for placeholder, target_text in mapping.items():
            result = result.replace(placeholder, target_text)
        return result


def load_glossary(path: Path | str | None) -> Glossary:
    if path is None:
        return Glossary(terms={})
    path = Path(path)
    if not path.is_file():
        return Glossary(terms={})

    terms: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        source_term, target_text = parts
        terms[source_term] = target_text
    return Glossary(terms=terms)
