"""gateway/export.py 的單元測試（純邏輯）。"""

from __future__ import annotations

import re

from contracts.enums import EngineKind, SubtitleState
from contracts.messages import AudioSpan, Subtitle
from gateway.export import ExportOptions, build_cues, export_subtitles, format_timestamp, wrap_text


def sub(
    utt: str,
    start_s: float,
    end_s: float,
    target: str,
    source: str = "src",
    state: SubtitleState = SubtitleState.FINAL,
) -> Subtitle:
    return Subtitle(
        utt_id=utt,
        revision=1,
        state=state,
        span=AudioSpan(round(start_s * 16000), round(end_s * 16000)),
        src_lang="ja",
        tgt_lang="zh-Hant",
        pack_id="ja-zhHant",
        source_text=source,
        target_text=target,
        engine=EngineKind.TRANSLATE_CT2_NLLB,
    )


def test_timestamp_formats_and_rounding() -> None:
    assert format_timestamp(0, fmt="srt") == "00:00:00,000"
    assert format_timestamp(3661.5, fmt="srt") == "01:01:01,500"
    assert format_timestamp(3661.5, fmt="vtt") == "01:01:01.500"
    # 浮點進位不能產生 1000 毫秒
    assert format_timestamp(1.9996, fmt="srt") == "00:00:02,000"
    assert format_timestamp(-5, fmt="srt") == "00:00:00,000"


def test_srt_structure_is_valid() -> None:
    out = export_subtitles([sub("a", 1.0, 2.5, "你好"), sub("b", 3.0, 4.0, "再見")])
    assert out == (
        "1\n00:00:01,000 --> 00:00:02,500\n你好\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n再見\n"
    )
    # 每個 cue：序號、時間碼、文字，以空行分隔
    assert re.fullmatch(r"(\d+\n[\d:,]+ --> [\d:,]+\n.+\n\n?)+", out)


def test_vtt_has_header_and_dot_separator_and_no_index() -> None:
    out = export_subtitles([sub("a", 1.0, 2.5, "你好")], ExportOptions(fmt="vtt"))
    assert out.startswith("WEBVTT\n\n00:00:01.000 --> 00:00:02.500\n你好")
    assert "\n1\n" not in out


def test_txt_format() -> None:
    out = export_subtitles([sub("a", 65.0, 66.0, "你好")], ExportOptions(fmt="txt"))
    assert out == "[00:01:05] 你好\n"


def test_only_final_and_polished_by_default_and_latest_state_wins() -> None:
    history = [
        sub("a", 0, 1, "定稿", state=SubtitleState.FINAL),
        sub("b", 2, 3, "暫定稿殘留（定稿被幻覺過濾丟掉了）", state=SubtitleState.DRAFT),
        sub("c", 4, 5, "精修後", state=SubtitleState.POLISHED),
        sub("d", 6, 7, "   ", state=SubtitleState.FINAL),  # 空白
    ]
    out = export_subtitles(history)
    assert "定稿" in out and "精修後" in out
    assert "暫定稿殘留" not in out
    assert out.count(" --> ") == 2
    assert "暫定稿殘留" in export_subtitles(history, ExportOptions(include_drafts=True))


def test_offset_shifts_and_clamps_at_zero() -> None:
    history = [sub("a", 1.0, 2.0, "x"), sub("b", 5.0, 6.0, "y")]
    cues = build_cues(history, ExportOptions(offset_ms=-1500))
    assert (cues[0].start_s, cues[0].end_s) == (0.0, 0.5)  # 1.0-1.5 → 夾到 0，且不為零時長
    assert cues[1].start_s == 3.5
    cues = build_cues(history, ExportOptions(offset_ms=+250))
    assert cues[0].start_s == 1.25


def test_rebase_starts_first_cue_at_zero_then_offset_applies() -> None:
    history = [sub("a", 100.0, 101.0, "x"), sub("b", 104.0, 105.0, "y")]
    cues = build_cues(history, ExportOptions(rebase=True))
    assert (cues[0].start_s, cues[1].start_s) == (0.0, 4.0)
    cues = build_cues(history, ExportOptions(rebase=True, offset_ms=500))
    assert (cues[0].start_s, cues[1].start_s) == (0.5, 4.5)


def test_cues_sorted_by_time_and_never_overlap() -> None:
    history = [sub("b", 1.1, 3.0, "later"), sub("a", 1.0, 1.1, "short")]  # 亂序；a 太短會被撐到 0.5s
    cues = build_cues(history, ExportOptions())
    assert [c.lines[0] for c in cues] == ["short", "later"]
    assert cues[0].end_s <= cues[1].start_s


def test_bilingual_adds_source_line_only_when_different() -> None:
    same = sub("a", 0, 1, "Hello", source="Hello")
    diff = sub("b", 2, 3, "你好", source="こんにちは")
    out = export_subtitles([same, diff], ExportOptions(bilingual=True))
    assert "你好\nこんにちは" in out
    assert out.count("Hello") == 1


def test_wrapping_cjk_prefers_punctuation_and_latin_wraps_on_words() -> None:
    lines = wrap_text("我認為我們應該，在決定明年預算之前先考慮季度結果。", 12)
    assert all(len(line) <= 12 for line in lines) and "".join(lines).startswith("我認為")
    assert lines[0].endswith("，")
    latin = wrap_text("I think we should consider the quarterly results first", 20)
    assert all(len(line) <= 20 for line in latin) and len(latin) >= 3
    assert wrap_text("短句", 12) == ["短句"]
    assert wrap_text("   ", 12) == []
    assert wrap_text("x" * 60, None) == ["x" * 60]


def test_empty_history_exports_empty() -> None:
    assert export_subtitles([]) == ""
    assert export_subtitles([], ExportOptions(fmt="vtt")) == "WEBVTT\n\n"
