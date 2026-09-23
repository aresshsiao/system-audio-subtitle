"""語言包（Language Pack）schema 定義與驗證。

見 ARCHITECTURE.md §13。這是使用者匯入外部檔案的唯一入口，所以驗證要嚴格：
語言包是**純資料**，不執行任何包內程式碼；`engine` 一類欄位一律對白名單檢查，
不接受任意字串當成模組路徑（那會變成任意程式碼載入的後門）。

用法：

    from contracts.langpack_schema import load_and_validate
    pack = load_and_validate(Path("config/langpacks/ja-zhHant"))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CURRENT_SCHEMA_VERSION = 1

# 白名單：pack.yaml 只能引用這裡列出的實作，不接受任意字串。
# 新增一種 ASR/翻譯引擎實作時，先在這裡登記，再讓語言包引用它 ——
# 這一步仍然是「登記一個能力」，不是「登記一個語言」，兩者不衝突：
# 加一個新語言配對（複用既有引擎）永遠不需要碰這個檔案。
KNOWN_ASR_ENGINES = frozenset({"faster-whisper"})
KNOWN_TRANSLATE_ENGINES = frozenset({"ct2-nllb", "llm-api", "none"})
KNOWN_STABILIZER_GRANULARITY = frozenset({"word", "char"})
KNOWN_LINE_BREAK = frozenset({"space", "phrase", "width"})


class LangPackValidationError(ValueError):
    """語言包格式不合法。訊息必須讓使用者看得懂哪裡錯，不是 KeyError 堆疊。"""


@dataclass(frozen=True)
class AsrConfig:
    engine: str
    model: str
    hallucination_blacklist_path: str | None = None


@dataclass(frozen=True)
class TranslateConfig:
    local_engine: str
    src_code: str  # 引擎專屬語言代碼，例如 NLLB 的 "jpn_Jpan"
    tgt_code: str
    cloud_engine: str | None = None
    glossary_path: str | None = None


@dataclass(frozen=True)
class PolicyConfig:
    draft_translate: bool
    line_break: str


@dataclass(frozen=True)
class LanguagePack:
    """驗證通過後的語言包，供 inference/langpack.py 的 registry 使用。"""

    pack_dir: Path
    schema_version: int
    id: str
    display_name: str
    version: str
    src_lang: str  # BCP-47
    tgt_lang: str  # BCP-47
    asr: AsrConfig
    stabilizer_granularity: str
    policy: PolicyConfig
    translate: TranslateConfig
    font_hint: str | None = None


def _require(d: dict[str, Any], key: str, context: str) -> Any:
    if key not in d:
        raise LangPackValidationError(f"{context}: 缺少必要欄位 '{key}'")
    return d[key]


def _require_choice(value: str, choices: frozenset[str], field_name: str) -> str:
    if value not in choices:
        raise LangPackValidationError(
            f"欄位 '{field_name}' 的值 '{value}' 不在允許清單內: {sorted(choices)}"
        )
    return value


def validate_raw(raw: dict[str, Any], *, pack_dir: Path) -> LanguagePack:
    """驗證一份已解析的 pack.yaml dict，回傳型別化的 LanguagePack。

    刻意不接受檔案路徑以外的任何動態行為：不 eval、不 import 使用者字串、
    不跟隨任意檔案路徑（glossary/blacklist 路徑一律相對於 pack_dir 解析，
    且必須落在 pack_dir 之內，避免 `../../` 逃逸到檔案系統其他地方）。
    """

    schema_version = _require(raw, "schema_version", "pack.yaml")
    if not isinstance(schema_version, int):
        raise LangPackValidationError("schema_version 必須是整數")
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise LangPackValidationError(
            f"這個語言包需要 schema_version {schema_version}，"
            f"但目前程式只支援到 {CURRENT_SCHEMA_VERSION}。請更新程式版本。"
        )

    pack_id = _require(raw, "id", "pack.yaml")
    display_name = _require(raw, "display_name", "pack.yaml")
    version = _require(raw, "version", "pack.yaml")
    src_lang = _require(raw, "src_lang", "pack.yaml")
    tgt_lang = _require(raw, "tgt_lang", "pack.yaml")

    asr_raw = _require(raw, "asr", "pack.yaml")
    engine = _require_choice(_require(asr_raw, "engine", "asr"), KNOWN_ASR_ENGINES, "asr.engine")
    asr = AsrConfig(
        engine=engine,
        model=_require(asr_raw, "model", "asr"),
        hallucination_blacklist_path=_resolve_optional_path(
            asr_raw.get("hallucination_blacklist"), pack_dir, "asr.hallucination_blacklist"
        ),
    )

    stabilizer_raw = _require(raw, "stabilizer", "pack.yaml")
    granularity = _require_choice(
        _require(stabilizer_raw, "granularity", "stabilizer"),
        KNOWN_STABILIZER_GRANULARITY,
        "stabilizer.granularity",
    )

    policy_raw = _require(raw, "policy", "pack.yaml")
    policy = PolicyConfig(
        draft_translate=bool(_require(policy_raw, "draft_translate", "policy")),
        line_break=_require_choice(
            _require(policy_raw, "line_break", "policy"), KNOWN_LINE_BREAK, "policy.line_break"
        ),
    )

    translate_raw = _require(raw, "translate", "pack.yaml")
    local_engine = _require_choice(
        _require(translate_raw, "local_engine", "translate"),
        KNOWN_TRANSLATE_ENGINES,
        "translate.local_engine",
    )
    cloud_engine = translate_raw.get("cloud_engine")
    if cloud_engine is not None:
        _require_choice(cloud_engine, KNOWN_TRANSLATE_ENGINES, "translate.cloud_engine")
    translate = TranslateConfig(
        local_engine=local_engine,
        src_code=_require(translate_raw, "src_code", "translate"),
        tgt_code=_require(translate_raw, "tgt_code", "translate"),
        cloud_engine=cloud_engine,
        glossary_path=_resolve_optional_path(
            translate_raw.get("glossary"), pack_dir, "translate.glossary"
        ),
    )

    typography_raw = raw.get("typography", {})
    font_hint = typography_raw.get("font_hint")

    return LanguagePack(
        pack_dir=pack_dir,
        schema_version=schema_version,
        id=pack_id,
        display_name=display_name,
        version=version,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        asr=asr,
        stabilizer_granularity=granularity,
        policy=policy,
        translate=translate,
        font_hint=font_hint,
    )


def _resolve_optional_path(rel_path: str | None, pack_dir: Path, field_name: str) -> str | None:
    if rel_path is None:
        return None
    resolved = (pack_dir / rel_path).resolve()
    pack_dir_resolved = pack_dir.resolve()
    if pack_dir_resolved not in resolved.parents and resolved != pack_dir_resolved:
        raise LangPackValidationError(
            f"欄位 '{field_name}' 指向語言包目錄之外的路徑，拒絕載入: {rel_path}"
        )
    return str(resolved)


def load_and_validate(pack_dir: Path) -> LanguagePack:
    """從資料夾載入 pack.yaml 並驗證。是匯入流程（§13.4）的唯一入口。"""

    pack_yaml = pack_dir / "pack.yaml"
    if not pack_yaml.is_file():
        raise LangPackValidationError(f"{pack_dir} 底下找不到 pack.yaml")

    try:
        raw = yaml.safe_load(pack_yaml.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise LangPackValidationError(f"pack.yaml 不是合法的 YAML: {e}") from e

    if not isinstance(raw, dict):
        raise LangPackValidationError("pack.yaml 的頂層必須是一個對照表 (mapping)")

    return validate_raw(raw, pack_dir=pack_dir)
