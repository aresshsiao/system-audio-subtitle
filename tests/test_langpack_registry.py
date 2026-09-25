"""inference/langpack.py 的測試。用 config/langpacks/ 的四個真實內建包，
再用 tmp_path 模擬「使用者匯入了新語言包 / 語言包壞掉」等情境。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from inference.langpack import DEFAULT_LANGPACKS_DIR, LangPackRegistry


def test_reload_finds_all_builtin_packs() -> None:
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()

    ids = {p.id for p in reg.all_packs()}
    assert ids == {"en-zhHant", "ja-zhHant", "th-zhHant", "zhHant-en"}


def test_get_returns_none_for_unknown_id() -> None:
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()
    assert reg.get("does-not-exist") is None


def test_enable_and_find_by_src_lang() -> None:
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()

    assert reg.find_by_src_lang("en") is None  # 還沒啟用任何包

    reg.enable("en-zhHant")
    found = reg.find_by_src_lang("en")
    assert found is not None
    assert found.id == "en-zhHant"


def test_set_enabled_replaces_not_accumulates() -> None:
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()

    reg.set_enabled(["en-zhHant", "ja-zhHant"])
    assert {p.id for p in reg.enabled_packs} == {"en-zhHant", "ja-zhHant"}

    reg.set_enabled(["th-zhHant"])  # 取代，不是疊加
    assert {p.id for p in reg.enabled_packs} == {"th-zhHant"}


def test_set_enabled_rejects_unknown_id() -> None:
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()
    with pytest.raises(KeyError):
        reg.set_enabled(["not-a-real-pack"])


def test_multiple_enabled_packs_route_by_src_lang() -> None:
    """模擬混合語言來源（例如同時開日文/英文）自動路由到對的包。"""
    reg = LangPackRegistry([DEFAULT_LANGPACKS_DIR])
    reg.reload()
    reg.set_enabled(["en-zhHant", "ja-zhHant", "th-zhHant"])

    assert reg.find_by_src_lang("en").id == "en-zhHant"
    assert reg.find_by_src_lang("ja").id == "ja-zhHant"
    assert reg.find_by_src_lang("th").id == "th-zhHant"
    assert reg.find_by_src_lang("ko") is None  # 沒有對應的包


def test_invalid_pack_is_skipped_not_fatal(tmp_path: Path) -> None:
    """一個壞掉的語言包不該讓整個 registry 掛掉，其他包要正常載入。"""
    good_dir = tmp_path / "good-pack"
    good_dir.mkdir()
    good_yaml = {
        "schema_version": 1,
        "id": "good-pack",
        "display_name": "測試包",
        "version": "1.0.0",
        "src_lang": "xx",
        "tgt_lang": "yy",
        "asr": {"engine": "faster-whisper", "model": "large-v3"},
        "stabilizer": {"granularity": "word"},
        "policy": {"draft_translate": True, "line_break": "space"},
        "translate": {"local_engine": "ct2-nllb", "src_code": "xxx_Yyyy", "tgt_code": "yyy_Zzzz"},
    }
    (good_dir / "pack.yaml").write_text(yaml.safe_dump(good_yaml), encoding="utf-8")

    bad_dir = tmp_path / "bad-pack"
    bad_dir.mkdir()
    (bad_dir / "pack.yaml").write_text("not: valid: yaml: at: all: [", encoding="utf-8")

    reg = LangPackRegistry([tmp_path])
    reg.reload()

    ids = {p.id for p in reg.all_packs()}
    assert ids == {"good-pack"}


def test_duplicate_pack_id_keeps_first_loaded(tmp_path: Path) -> None:
    def make_pack(dirname: str, version: str) -> None:
        d = tmp_path / dirname
        d.mkdir()
        raw = {
            "schema_version": 1,
            "id": "dup-id",
            "display_name": "重複測試",
            "version": version,
            "src_lang": "xx",
            "tgt_lang": "yy",
            "asr": {"engine": "faster-whisper", "model": "large-v3"},
            "stabilizer": {"granularity": "word"},
            "policy": {"draft_translate": True, "line_break": "space"},
            "translate": {
                "local_engine": "ct2-nllb",
                "src_code": "xxx_Yyyy",
                "tgt_code": "yyy_Zzzz",
            },
        }
        (d / "pack.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    make_pack("a-first", "1.0.0")
    make_pack("z-second", "2.0.0")  # 字母序在後，後載入

    reg = LangPackRegistry([tmp_path])
    reg.reload()

    packs = [p for p in reg.all_packs() if p.id == "dup-id"]
    assert len(packs) == 1
    assert packs[0].version == "1.0.0"  # 保留先載入（字母序較前）的那個


def test_reload_removes_deleted_packs_from_enabled(tmp_path: Path) -> None:
    pack_dir = tmp_path / "temp-pack"
    pack_dir.mkdir()
    raw = {
        "schema_version": 1,
        "id": "temp-pack",
        "display_name": "暫時的",
        "version": "1.0.0",
        "src_lang": "xx",
        "tgt_lang": "yy",
        "asr": {"engine": "faster-whisper", "model": "large-v3"},
        "stabilizer": {"granularity": "word"},
        "policy": {"draft_translate": True, "line_break": "space"},
        "translate": {"local_engine": "ct2-nllb", "src_code": "xxx_Yyyy", "tgt_code": "yyy_Zzzz"},
    }
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    reg = LangPackRegistry([tmp_path])
    reg.reload()
    reg.enable("temp-pack")
    assert "temp-pack" in {p.id for p in reg.enabled_packs}

    import shutil

    shutil.rmtree(pack_dir)
    reg.reload()

    assert reg.get("temp-pack") is None
    assert "temp-pack" not in {p.id for p in reg.enabled_packs}
