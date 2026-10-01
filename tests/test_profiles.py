"""runtime/profiles.py"""

from __future__ import annotations

from pathlib import Path

import pytest

from inference.langpack import LangPackRegistry
from runtime.profiles import (
    BUILTIN_PROFILES_DIR,
    ProfileValidationError,
    load_profile,
    load_profiles,
    resolve_langpack_ids,
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_four_builtin_profiles_load_and_reference_only_existing_langpacks() -> None:
    profiles = {p.id: p for p in load_profiles([BUILTIN_PROFILES_DIR])}
    assert set(profiles) == {"anime", "meeting", "lecture", "streaming-out"}

    registry = LangPackRegistry()
    registry.reload()
    known = {p.id for p in registry.all_packs()}
    for profile in profiles.values():
        _usable, missing = resolve_langpack_ids(profile, known)
        assert missing == [], f"{profile.id} 引用了不存在的語言包 {missing}"

    assert profiles["anime"].langpack_ids == ("ja-zhHant",)
    assert profiles["streaming-out"].langpack_ids == ("zhHant-en",)
    assert profiles["lecture"].langpack_ids == ()  # 依素材手動選
    assert profiles["meeting"].autosave_transcript and not profiles["anime"].autosave_transcript


def test_no_profile_can_silently_enable_cloud_polish() -> None:
    """雲端精修會把內容送往第三方：Profile 只有『提示』欄位，沒有『啟用』欄位。"""
    from dataclasses import fields

    from runtime.profiles import Profile

    names = {f.name for f in fields(Profile)}
    assert "cloud_polish_hint" in names and "cloud_polish" not in names and "enable_cloud_polish" not in names


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("display_name: x\n", "id"),
        ("id: x\n", "display_name"),
        ("id: x\ndisplay_name: y\nlangpack_ids: ja\n", "langpack_ids"),
        ("id: x\ndisplay_name: y\nfont_scale: big\n", "font_scale"),
        ("id: x\ndisplay_name: y\nfont_scale: 99\n", "font_scale"),
        ("id: x\ndisplay_name: y\nautosave_transcript: maybe\n", "autosave_transcript"),
        ("- just\n- a list\n", "對照表"),
        ("id: [unclosed", "YAML"),
    ],
)
def test_invalid_profiles_give_readable_errors(tmp_path, text, fragment) -> None:
    with pytest.raises(ProfileValidationError) as exc:
        load_profile(write(tmp_path, "bad.yaml", text))
    assert fragment in str(exc.value)


def test_bad_profile_is_skipped_not_fatal(tmp_path) -> None:
    write(tmp_path, "bad.yaml", "id: [oops")
    write(tmp_path, "good.yaml", "id: good\ndisplay_name: G\n")
    assert [p.id for p in load_profiles([tmp_path])] == ["good"]


def test_user_profile_dir_is_loaded_and_builtin_wins_on_id_clash(tmp_path, monkeypatch) -> None:
    user = tmp_path / "u"
    user.mkdir()
    write(user, "mine.yaml", "id: mine\ndisplay_name: 我的\nlangpack_ids: [en-zhHant]\n")
    write(user, "anime.yaml", "id: anime\ndisplay_name: 蓋掉內建？\n")
    loaded = {p.id: p for p in load_profiles([BUILTIN_PROFILES_DIR, user])}
    assert "mine" in loaded
    assert loaded["anime"].display_name != "蓋掉內建？"


def test_resolve_reports_missing_and_keeps_usable() -> None:
    from runtime.profiles import Profile

    p = Profile("x", "X", ("en-zhHant", "ko-zhHant"))
    assert resolve_langpack_ids(p, {"en-zhHant"}) == (["en-zhHant"], ["ko-zhHant"])


# --- ui/profile_apply.py ---


def _ack(ids):
    from contracts.messages import SetActiveLangPacksAck

    return SetActiveLangPacksAck(success=True, active_pack_ids=list(ids))


def test_apply_profile_sends_langpacks_sets_font_and_reports() -> None:
    from runtime.profiles import Profile
    from ui.profile_apply import apply_profile

    sent, scales = [], []
    profile = Profile("anime", "動畫", ("ja-zhHant",), font_scale=1.15, cloud_polish_hint=True)
    report = apply_profile(
        profile,
        known_langpack_ids={"ja-zhHant", "en-zhHant"},
        request_fn=lambda m: sent.append(m) or _ack(m.pack_ids),
        set_font_scale=scales.append,
    )
    assert sent[0].pack_ids == ["ja-zhHant"] and sent[0].reload is True
    assert scales == [1.15] and report.ok
    assert any("語言包：ja-zhHant" in m for m in report.messages)
    assert any("雲端精修" in m and "自己" in m for m in report.messages)  # 只提示，不代開


def test_apply_profile_with_empty_langpacks_leaves_current_setting() -> None:
    from runtime.profiles import Profile
    from ui.profile_apply import apply_profile

    sent = []
    report = apply_profile(
        Profile("lecture", "課程", ()),
        known_langpack_ids={"en-zhHant"},
        request_fn=lambda m: sent.append(m),
        set_font_scale=lambda s: None,
    )
    assert sent == [] and report.ok


def test_apply_profile_reports_missing_pack_and_dead_service() -> None:
    from runtime.profiles import Profile
    from ui.profile_apply import apply_profile

    def down(_m):
        raise OSError("down")

    report = apply_profile(
        Profile("x", "X", ("ko-zhHant", "en-zhHant")),
        known_langpack_ids={"en-zhHant"},
        request_fn=down,
        set_font_scale=lambda s: None,
    )
    assert not report.ok
    assert any("ko-zhHant" in m and "沒有安裝" in m for m in report.messages)
    assert any("沒有套用成功" in m for m in report.messages)


def test_autosave_transcript_writes_only_when_there_is_content(tmp_path) -> None:
    from datetime import datetime

    from ui.profile_apply import autosave_transcript

    assert autosave_transcript("   \n") is None
    path = autosave_transcript("[00:00:01] 你好\n", now=datetime(2026, 9, 26, 12, 30, 5))
    assert path.name == "transcript-20260926-123005.txt"
    assert path.read_text(encoding="utf-8") == "[00:00:01] 你好\n"
