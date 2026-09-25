"""inference/translate/context.py 的測試。"""

from __future__ import annotations

from inference.translate.context import TranslationContext


def test_empty_context_returns_text_unchanged() -> None:
    ctx = TranslationContext(max_sentences=5)
    assert ctx.build_input("hello world") == "hello world"


def test_build_input_prepends_prior_sources() -> None:
    ctx = TranslationContext(max_sentences=5)
    ctx.push("First sentence.", "第一句。")
    ctx.push("Second sentence.", "第二句。")

    result = ctx.build_input("Third sentence.")
    assert result == "First sentence. Second sentence. Third sentence."


def test_rolling_window_drops_oldest_beyond_max() -> None:
    ctx = TranslationContext(max_sentences=2)
    ctx.push("A", "甲")
    ctx.push("B", "乙")
    ctx.push("C", "丙")  # 超過 max_sentences=2，A 該被擠掉

    assert len(ctx) == 2
    result = ctx.build_input("D")
    assert "A" not in result
    assert result == "B C D"


def test_clear_empties_history() -> None:
    ctx = TranslationContext(max_sentences=5)
    ctx.push("A", "甲")
    ctx.clear()
    assert len(ctx) == 0
    assert ctx.build_input("B") == "B"


def test_extract_current_translation_no_context() -> None:
    ctx = TranslationContext()
    out = ctx.extract_current_translation("這是唯一一句。", num_context_sentences=0)
    assert out == "這是唯一一句。"


def test_extract_current_translation_splits_by_sentence_end() -> None:
    ctx = TranslationContext()
    full_output = "第一句。第二句。第三句。"
    out = ctx.extract_current_translation(full_output, num_context_sentences=2)
    assert out == "第三句。"


def test_extract_current_translation_handles_english_punctuation() -> None:
    ctx = TranslationContext()
    full_output = "First. Second. Third sentence here."
    out = ctx.extract_current_translation(full_output, num_context_sentences=2)
    assert out == "Third sentence here."


def test_extract_current_translation_no_sentence_boundary_returns_whole() -> None:
    """模型輸出完全沒有句尾標點時（極端情況），退回整段輸出，不是空字串。"""
    ctx = TranslationContext()
    out = ctx.extract_current_translation("沒有標點的一整段文字", num_context_sentences=1)
    assert out == "沒有標點的一整段文字"
