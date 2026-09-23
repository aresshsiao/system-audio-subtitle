"""contracts/langpack_schema.py 的單元測試。

兩件事要測：
  1. 四個內建語言包（config/langpacks/*）真的能被驗證通過 —— 這是
     ARCHITECTURE.md §13 機制成立的前提，schema 跟實際資料檔不同步
     是最容易發生也最晚被發現的錯誤。
  2. 驗證邏輯真的會擋下壞資料：未知 engine、schema_version 過高、
     路徑逃逸到 pack 目錄之外。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from contracts.langpack_schema import (
    CURRENT_SCHEMA_VERSION,
    LangPackValidationError,
    LanguagePack,
    load_and_validate,
    validate_raw,
)

LANGPACKS_DIR = Path(__file__).resolve().parent.parent / "config" / "langpacks"


def minimal_pack_dict(**overrides: object) -> dict:
    base = {
        "schema_version": 1,
        "id": "xx-yy",
        "display_name": "測試包",
        "version": "1.0.0",
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
    base.update(overrides)
    return base


# --- 內建語言包：全部必須驗證通過 ---


@pytest.mark.parametrize(
    "pack_id", ["en-zhHant", "ja-zhHant", "th-zhHant", "zhHant-en"]
)
def test_builtin_langpack_validates(pack_id: str) -> None:
    pack_dir = LANGPACKS_DIR / pack_id
    pack = load_and_validate(pack_dir)

    assert isinstance(pack, LanguagePack)
    assert pack.id == pack_id
    assert pack.schema_version == CURRENT_SCHEMA_VERSION


def test_builtin_langpacks_directory_matches_pack_id() -> None:
    """資料夾名稱要跟 pack.yaml 裡的 id 一致，避免 registry 查表對不上。"""
    for pack_dir in LANGPACKS_DIR.iterdir():
        if not pack_dir.is_dir():
            continue
        pack = load_and_validate(pack_dir)
        assert pack.id == pack_dir.name, f"{pack_dir} 的 id 與資料夾名稱不一致"


def test_ja_zhHant_disables_draft_translate() -> None:
    """§8 的關鍵設計決策：日文 SOV 語序，暫定稿不能先翻。"""
    pack = load_and_validate(LANGPACKS_DIR / "ja-zhHant")
    assert pack.policy.draft_translate is False
    assert pack.stabilizer_granularity == "char"


def test_ja_zhHant_hallucination_blacklist_resolves_and_exists() -> None:
    pack = load_and_validate(LANGPACKS_DIR / "ja-zhHant")
    assert pack.asr.hallucination_blacklist_path is not None
    assert Path(pack.asr.hallucination_blacklist_path).is_file()


def test_thai_and_japanese_use_char_granularity() -> None:
    """§8 泰文特例：無空格分詞，比對粒度必須是字元。"""
    for pack_id in ("ja-zhHant", "th-zhHant"):
        pack = load_and_validate(LANGPACKS_DIR / pack_id)
        assert pack.stabilizer_granularity == "char"


def test_reverse_pack_zhHant_en_direction() -> None:
    pack = load_and_validate(LANGPACKS_DIR / "zhHant-en")
    assert pack.src_lang == "zh"
    assert pack.tgt_lang == "en"
    assert pack.translate.src_code == "zho_Hant"
    assert pack.translate.tgt_code == "eng_Latn"


# --- 驗證邏輯本身：擋壞資料 ---


def test_missing_required_field_rejected() -> None:
    raw = minimal_pack_dict()
    del raw["src_lang"]
    with pytest.raises(LangPackValidationError, match="src_lang"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_schema_version_too_new_rejected() -> None:
    raw = minimal_pack_dict(schema_version=CURRENT_SCHEMA_VERSION + 1)
    with pytest.raises(LangPackValidationError, match="schema_version"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_unknown_asr_engine_rejected() -> None:
    """engine 白名單是安全邊界：不能讓語言包指向任意模組路徑（見 §16）。"""
    raw = minimal_pack_dict(asr={"engine": "os.system", "model": "large-v3"})
    with pytest.raises(LangPackValidationError, match="asr.engine"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_unknown_translate_engine_rejected() -> None:
    raw = minimal_pack_dict(
        translate={
            "local_engine": "some-random-engine",
            "src_code": "xxx_Yyyy",
            "tgt_code": "yyy_Zzzz",
        }
    )
    with pytest.raises(LangPackValidationError, match="translate.local_engine"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_unknown_stabilizer_granularity_rejected() -> None:
    raw = minimal_pack_dict(stabilizer={"granularity": "syllable"})
    with pytest.raises(LangPackValidationError, match="stabilizer.granularity"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_path_traversal_in_glossary_rejected() -> None:
    """匯入語言包時，glossary/blacklist 路徑不能逃逸到 pack 目錄之外。"""
    raw = minimal_pack_dict(
        translate={
            "local_engine": "ct2-nllb",
            "src_code": "xxx_Yyyy",
            "tgt_code": "yyy_Zzzz",
            "glossary": "../../../../windows/system32/drivers/etc/hosts",
        }
    )
    with pytest.raises(LangPackValidationError, match="glossary"):
        validate_raw(raw, pack_dir=LANGPACKS_DIR / "en-zhHant")


def test_missing_pack_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(LangPackValidationError, match="pack.yaml"):
        load_and_validate(tmp_path)


def test_malformed_yaml_rejected(tmp_path: Path) -> None:
    (tmp_path / "pack.yaml").write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(LangPackValidationError):
        load_and_validate(tmp_path)


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    (tmp_path / "pack.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(LangPackValidationError, match="mapping"):
        load_and_validate(tmp_path)


def test_new_language_pair_requires_only_a_yaml_file(tmp_path: Path) -> None:
    """§13.3 的核心承諾：新增語言配對不需要改任何程式碼，只要一份 pack.yaml。

    這裡用韓文→繁中模擬「使用者匯入一個全新配對」，驗證 schema 本身
    對任意 BCP-47 語言代碼都成立，沒有 en/ja/th 三個語言被寫死在驗證邏輯裡。
    """
    pack_dir = tmp_path / "ko-zhHant"
    pack_dir.mkdir()
    raw = minimal_pack_dict(
        id="ko-zhHant",
        display_name="韓文 → 繁體中文",
        src_lang="ko",
        tgt_lang="zh-Hant",
        translate={
            "local_engine": "ct2-nllb",
            "src_code": "kor_Hang",
            "tgt_code": "zho_Hant",
        },
    )
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    pack = load_and_validate(pack_dir)
    assert pack.id == "ko-zhHant"
    assert pack.src_lang == "ko"
