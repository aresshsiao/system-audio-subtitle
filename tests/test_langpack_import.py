"""inference/langpack_import.py：匯入流程與安全防線。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from inference.asr.base import ASRResult
from inference.langpack import LangPackRegistry
from inference.langpack_import import (
    LangPackImportError,
    import_langpack,
    remove_user_langpack,
)
from inference.pipeline import Pipeline, TranslatorRegistry
from inference.stabilizer import Stabilizer
from utils.paths import user_langpacks_dir

ROOT = Path(__file__).resolve().parent.parent
KO_EXAMPLE = ROOT / "docs" / "examples" / "langpacks" / "ko-zhHant"

VALID_YAML = """\
schema_version: 1
id: {id}
display_name: "測試包"
version: "1.0.0"
src_lang: fr
tgt_lang: zh-Hant
asr: {{engine: faster-whisper, model: large-v3}}
stabilizer: {{granularity: word}}
policy: {{draft_translate: true, line_break: space}}
translate: {{local_engine: ct2-nllb, src_code: fra_Latn, tgt_code: zho_Hant}}
"""


def make_pack(root: Path, pack_id: str = "fr-zhHant", yaml_text: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(yaml_text or VALID_YAML.format(id=pack_id), encoding="utf-8")
    return root


def make_zip(zip_path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return zip_path


# --- 成功路徑 ---


def test_import_folder_installs_into_user_dir(tmp_path) -> None:
    src = make_pack(tmp_path / "src")
    result = import_langpack(src)

    assert result.pack.id == "fr-zhHant" and not result.replaced
    assert result.dest == user_langpacks_dir() / "fr-zhHant"
    assert (result.dest / "pack.yaml").is_file()


def test_import_zip_root_and_nested_folder_layouts(tmp_path) -> None:
    yaml = VALID_YAML.format(id="fr-a")
    r1 = import_langpack(make_zip(tmp_path / "a.langpack.zip", {"pack.yaml": yaml}))
    assert r1.pack.id == "fr-a"

    yaml = VALID_YAML.format(id="fr-b")
    r2 = import_langpack(make_zip(tmp_path / "b.zip", {"fr-b/pack.yaml": yaml, "fr-b/glossary.tsv": "a\tb\n"}))
    assert r2.pack.id == "fr-b" and (r2.dest / "glossary.tsv").is_file()


def test_reimport_replaces_and_reports_it(tmp_path) -> None:
    import_langpack(make_pack(tmp_path / "v1"))
    second = make_pack(tmp_path / "v2", yaml_text=VALID_YAML.format(id="fr-zhHant").replace('"1.0.0"', '"2.0.0"'))
    result = import_langpack(second)
    assert result.replaced and result.pack.version == "2.0.0"


def test_registry_sees_imported_pack_after_reload_and_builtins_stay(tmp_path) -> None:
    registry = LangPackRegistry()
    registry.reload()
    before = {p.id for p in registry.all_packs()}
    assert len(before) == 4

    import_langpack(make_pack(tmp_path / "src"))
    registry.reload()
    after = {p.id for p in registry.all_packs()}
    assert after == before | {"fr-zhHant"}


def test_remove_user_langpack(tmp_path) -> None:
    import_langpack(make_pack(tmp_path / "src"))
    assert remove_user_langpack("fr-zhHant") is True
    assert remove_user_langpack("fr-zhHant") is False
    assert remove_user_langpack("../../etc") is False  # 不接受路徑


# --- 零程式碼新增語言（ROADMAP M6 驗收）---


def test_zero_code_korean_pack_imports_and_routes_through_unchanged_pipeline(tmp_path) -> None:
    """全新語言只靠一個資料夾：匯入 → registry → 既有 Pipeline 依包的宣告路由與翻譯，
    沒有動 audio/、inference/、ui/ 任何一行。"""
    result = import_langpack(KO_EXAMPLE)
    assert result.pack.src_lang == "ko" and result.pack.translate.src_code == "kor_Hang"

    registry = LangPackRegistry()
    registry.reload()
    registry.set_enabled(["ko-zhHant"])

    calls = []

    class FakeTranslator:
        def translate(self, text, *, src_code, tgt_code):
            calls.append((text, src_code, tgt_code))
            return "譯文"

    class FakeASR:
        def transcribe(self, audio, *, language=None, beam_size=1, initial_prompt=None):
            return ASRResult(text="방탄소년단 안녕하세요", language="ko", no_speech_prob=0.0)

    pipeline = Pipeline(FakeASR(), registry, TranslatorRegistry({"ct2-nllb": FakeTranslator()}))
    out = pipeline.process_draft(None, forced_language="ko", stabilizer=Stabilizer())

    assert out.pack_id == "ko-zhHant"
    assert out.target_text is None  # 包宣告 draft_translate: false → 暫定稿不翻
    assert calls == []

    from inference.translate.context import TranslationContext

    final = pipeline.process_final(None, forced_language="ko", context=TranslationContext())
    assert final.tgt_lang == "zh-Hant" and final.target_text == "譯文"
    sent_text, src_code, tgt_code = calls[0]
    assert (src_code, tgt_code) == ("kor_Hang", "zho_Hant")
    assert "防彈少年團" not in sent_text and "§0§" in sent_text  # 包裡的術語表生效（先換成佔位符）


# --- 失敗路徑：明確錯誤、不留殘渣 ---


def fails_with(source: Path, fragment: str) -> None:
    with pytest.raises(LangPackImportError) as exc:
        import_langpack(source)
    assert fragment in str(exc.value), str(exc.value)
    assert not user_langpacks_dir().exists() or list(user_langpacks_dir().iterdir()) == []


def test_missing_required_field_gives_readable_error(tmp_path) -> None:
    broken = VALID_YAML.format(id="x1").replace("src_lang: fr\n", "")
    fails_with(make_pack(tmp_path / "s", yaml_text=broken), "src_lang")


def test_unknown_engine_is_rejected(tmp_path) -> None:
    broken = VALID_YAML.format(id="x2").replace("ct2-nllb", "os.system")
    fails_with(make_pack(tmp_path / "s", yaml_text=broken), "不在允許清單")


def test_newer_schema_version_is_rejected(tmp_path) -> None:
    broken = VALID_YAML.format(id="x3").replace("schema_version: 1", "schema_version: 99")
    fails_with(make_pack(tmp_path / "s", yaml_text=broken), "schema_version")


def test_invalid_yaml_is_rejected(tmp_path) -> None:
    fails_with(make_pack(tmp_path / "s", yaml_text="id: [unclosed"), "YAML")


def test_missing_pack_yaml_is_rejected(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    fails_with(empty, "pack.yaml")


def test_glossary_path_escape_is_rejected(tmp_path) -> None:
    broken = VALID_YAML.format(id="x4").replace(
        "tgt_code: zho_Hant}", "tgt_code: zho_Hant, glossary: ../../secret.tsv}"
    )
    fails_with(make_pack(tmp_path / "s", yaml_text=broken), "目錄之外")


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", ".hidden", "x" * 80, "with space"])
def test_unsafe_pack_id_is_rejected(tmp_path, bad_id) -> None:
    fails_with(make_pack(tmp_path / "s", yaml_text=VALID_YAML.format(id=f'"{bad_id}"')), "不合法")


def test_builtin_id_cannot_be_overridden(tmp_path) -> None:
    fails_with(make_pack(tmp_path / "s", pack_id="ja-zhHant"), "內建")


def test_executable_or_unknown_file_types_are_rejected(tmp_path) -> None:
    src = make_pack(tmp_path / "s")
    (src / "payload.py").write_text("print('hi')")
    fails_with(src, "不能含可執行內容")
    fails_with(
        make_zip(tmp_path / "z.zip", {"pack.yaml": VALID_YAML.format(id="x5"), "run.exe": b"MZ"}),
        "run.exe",
    )


def test_zip_slip_is_rejected(tmp_path) -> None:
    z = make_zip(tmp_path / "slip.zip", {"pack.yaml": VALID_YAML.format(id="x6"), "../evil.txt": "x"})
    fails_with(z, "不安全的路徑")
    assert not (tmp_path / "evil.txt").exists()
    fails_with(make_zip(tmp_path / "abs.zip", {"/abs.txt": "x", "pack.yaml": "x"}), "不安全的路徑")


def test_zip_bomb_limits(tmp_path) -> None:
    fails_with(make_zip(tmp_path / "big.zip", {"pack.yaml": VALID_YAML.format(id="x7"), "g.tsv": "a" * (3 * 1024 * 1024)}), "過大")
    many = {f"f{i}.txt": "x" for i in range(60)}
    many["pack.yaml"] = VALID_YAML.format(id="x8")
    fails_with(make_zip(tmp_path / "many.zip", many), "太多")


def test_not_a_zip_and_wrong_extension_and_missing_path(tmp_path) -> None:
    bad = tmp_path / "fake.zip"
    bad.write_bytes(b"not a zip")
    fails_with(bad, "zip")
    other = tmp_path / "pack.rar"
    other.write_bytes(b"x")
    fails_with(other, "資料夾")
    fails_with(tmp_path / "nope", "找不到")


def test_failed_import_keeps_existing_installed_version(tmp_path) -> None:
    import_langpack(make_pack(tmp_path / "ok"))
    broken = make_pack(tmp_path / "bad", yaml_text=VALID_YAML.format(id="fr-zhHant").replace("ct2-nllb", "bogus"))
    with pytest.raises(LangPackImportError):
        import_langpack(broken)
    assert (user_langpacks_dir() / "fr-zhHant" / "pack.yaml").is_file()  # 舊版還在


def test_example_folder_is_data_only() -> None:
    """範例包本身要遵守自己的規則：只有資料檔。"""
    assert {p.suffix for p in KO_EXAMPLE.iterdir()} <= {".yaml", ".tsv", ".txt"}
