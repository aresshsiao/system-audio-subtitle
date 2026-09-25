"""inference/translate/glossary.py 的測試。"""

from __future__ import annotations

from pathlib import Path

from inference.translate.glossary import Glossary, load_glossary


def test_load_glossary_missing_path_returns_empty() -> None:
    g = load_glossary(None)
    assert g.terms == {}


def test_load_glossary_parses_tsv(tmp_path: Path) -> None:
    p = tmp_path / "glossary.tsv"
    p.write_text("# 註解行\nAlice\t愛麗絲\nBob\t鮑伯\n", encoding="utf-8")

    g = load_glossary(p)
    assert g.terms == {"Alice": "愛麗絲", "Bob": "鮑伯"}


def test_load_glossary_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "glossary.tsv"
    p.write_text("Alice\t愛麗絲\nmalformed_line_no_tab\nBob\t鮑伯\n", encoding="utf-8")

    g = load_glossary(p)
    assert g.terms == {"Alice": "愛麗絲", "Bob": "鮑伯"}


def test_apply_and_restore_placeholders_roundtrip() -> None:
    g = Glossary(terms={"Alice": "愛麗絲"})
    replaced, mapping = g.apply_placeholders("Alice went to the store.")

    assert "Alice" not in replaced
    assert len(mapping) == 1

    # 模擬翻譯引擎原樣保留了佔位符（理想情況）
    fake_translation_output = replaced.replace("went to the store.", "去了商店。")
    restored = Glossary.restore_placeholders(fake_translation_output, mapping)
    assert "愛麗絲" in restored


def test_longer_terms_matched_before_shorter_substrings() -> None:
    """"Alice" 跟 "Alice Wang" 同時存在時，要先處理長的，不能被短的吃掉一部分。"""
    g = Glossary(terms={"Alice": "愛麗絲", "Alice Wang": "王愛麗絲"})
    replaced, mapping = g.apply_placeholders("Alice Wang said hello to Alice.")

    # "Alice Wang" 應該整體被換成它自己的佔位符，不會被 "Alice" 的替換切開
    assert "Alice Wang" not in replaced
    assert "Alice" not in replaced
    assert len(mapping) == 2


def test_apply_placeholders_noop_when_term_not_present() -> None:
    g = Glossary(terms={"Alice": "愛麗絲"})
    replaced, mapping = g.apply_placeholders("Nothing relevant here.")
    assert replaced == "Nothing relevant here."
    assert mapping == {}


def test_restore_placeholders_leaves_unmatched_text_untouched() -> None:
    result = Glossary.restore_placeholders("no placeholders here", {})
    assert result == "no placeholders here"
