"""gateway/session.py 的單元測試。純邏輯，不需要 GUI 或網路。"""

from __future__ import annotations

from contracts.enums import EngineKind, SubtitleState
from contracts.messages import AudioSpan, Subtitle
from gateway.session import Session


def make_subtitle(
    utt_id: str, revision: int, text: str, *, state: SubtitleState = SubtitleState.DRAFT
) -> Subtitle:
    return Subtitle(
        utt_id=utt_id,
        revision=revision,
        state=state,
        span=AudioSpan(0, 16000),
        src_lang="en",
        tgt_lang="zh-Hant",
        pack_id="en-zhHant",
        source_text=text,
        target_text=text,
        engine=EngineKind.TRANSLATE_CT2_NLLB,
    )


def test_empty_session_has_no_current_display() -> None:
    session = Session()
    assert session.current_display() is None
    assert session.history() == []


def test_apply_new_utterance_becomes_current_display() -> None:
    session = Session()
    t = make_subtitle("u1", 0, "hello")
    changed = session.apply(t)

    assert changed is True
    assert session.current_display() == t


def test_higher_revision_replaces_lower_for_same_utterance() -> None:
    session = Session()
    session.apply(make_subtitle("u1", 0, "hel"))
    t2 = make_subtitle("u1", 1, "hello")
    changed = session.apply(t2)

    assert changed is True
    assert session.current_display() == t2
    assert session.latest("u1") == t2


def test_stale_revision_is_ignored() -> None:
    """見 ARCHITECTURE.md §5：亂序到達時，舊 revision 要被丟棄，不能覆蓋新的。"""
    session = Session()
    session.apply(make_subtitle("u1", 2, "hello world"))
    changed = session.apply(make_subtitle("u1", 1, "hello"))  # 過期的舊訊息晚到

    assert changed is False
    assert session.latest("u1").target_text == "hello world"  # 沒有被舊訊息覆蓋


def test_new_utterance_becomes_display_focus_even_if_previous_not_final() -> None:
    """新句子一開始，顯示焦點就切過去——即使前一句還沒收到 FINAL。"""
    session = Session()
    session.apply(make_subtitle("u1", 0, "first"))
    session.apply(make_subtitle("u2", 0, "second"))

    assert session.current_display().utt_id == "u2"
    assert session.latest("u1").target_text == "first"  # u1 的狀態還在，只是不是顯示焦點


def test_final_utterance_stays_displayed_until_next_utterance_starts() -> None:
    session = Session()
    session.apply(make_subtitle("u1", 0, "draft text", state=SubtitleState.DRAFT))
    final = make_subtitle("u1", 1, "final text", state=SubtitleState.FINAL)
    session.apply(final)

    # 還沒有新句子開始，定稿要留在畫面上
    assert session.current_display() == final


def test_history_preserves_first_seen_order_with_latest_state() -> None:
    session = Session()
    session.apply(make_subtitle("u1", 0, "a"))
    session.apply(make_subtitle("u2", 0, "b"))
    session.apply(make_subtitle("u1", 1, "a-updated"))  # u1 更新，但順序不變

    history = session.history()
    assert [t.utt_id for t in history] == ["u1", "u2"]
    assert history[0].target_text == "a-updated"


def test_clear_resets_everything() -> None:
    session = Session()
    session.apply(make_subtitle("u1", 0, "hello"))
    session.clear()

    assert session.current_display() is None
    assert session.history() == []
    assert session.latest("u1") is None
