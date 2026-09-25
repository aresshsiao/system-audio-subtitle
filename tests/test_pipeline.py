"""inference/pipeline.py 的測試。用假的 ASR/翻譯引擎測路由決策——語言包
選對了沒、日文暫定稿真的不翻、找不到語言包時顯示原文、術語表有套用、
上下文有正確累積。不需要真的 GPU/模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from inference.asr.base import ASRResult
from inference.langpack import LangPackRegistry
from inference.pipeline import Pipeline, TranslatorRegistry
from inference.stabilizer import Stabilizer
from inference.translate.context import TranslationContext


class FakeASREngine:
    """依序吐出預先設定好的結果，不管傳進來的 audio 是什麼。"""

    def __init__(self, results: list[ASRResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def transcribe(self, audio, *, language=None, beam_size=1, initial_prompt=None) -> ASRResult:
        self.calls.append(
            {"language": language, "beam_size": beam_size, "initial_prompt": initial_prompt}
        )
        return self._results.pop(0)


class FakeTranslator:
    """把輸入文字包一個標記回傳，方便斷言「真的呼叫了翻譯、代碼有傳對」。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def translate(self, text: str, *, src_code: str, tgt_code: str) -> str:
        self.calls.append({"text": text, "src_code": src_code, "tgt_code": tgt_code})
        return f"[{src_code}->{tgt_code}] {text}"


def make_registry(tmp_path: Path, packs: list[dict]) -> LangPackRegistry:
    for raw in packs:
        d = tmp_path / raw["id"]
        d.mkdir()
        (d / "pack.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    reg = LangPackRegistry([tmp_path])
    reg.reload()
    reg.set_enabled([p["id"] for p in packs])
    return reg


def en_pack(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "id": "en-zhHant",
        "display_name": "英文 → 繁體中文",
        "version": "1.0.0",
        "src_lang": "en",
        "tgt_lang": "zh-Hant",
        "asr": {"engine": "faster-whisper", "model": "large-v3"},
        "stabilizer": {"granularity": "word"},
        "policy": {"draft_translate": True, "line_break": "space"},
        "translate": {"local_engine": "ct2-nllb", "src_code": "eng_Latn", "tgt_code": "zho_Hant"},
    }
    base.update(overrides)
    return base


def ja_pack(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "id": "ja-zhHant",
        "display_name": "日文 → 繁體中文",
        "version": "1.0.0",
        "src_lang": "ja",
        "tgt_lang": "zh-Hant",
        "asr": {"engine": "faster-whisper", "model": "large-v3"},
        "stabilizer": {"granularity": "char"},
        "policy": {"draft_translate": False, "line_break": "phrase"},
        "translate": {"local_engine": "ct2-nllb", "src_code": "jpn_Jpan", "tgt_code": "zho_Hant"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def translator() -> FakeTranslator:
    return FakeTranslator()


def make_pipeline(tmp_path, packs, asr_results, translator) -> tuple[Pipeline, FakeASREngine]:
    registry = make_registry(tmp_path, packs)
    asr = FakeASREngine(asr_results)
    translators = TranslatorRegistry({"ct2-nllb": translator})
    return Pipeline(asr, registry, translators), asr


# --- 暫定稿 ---


def test_draft_translates_when_policy_allows(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="hello world", language="en", no_speech_prob=0.01)],
        translator,
    )
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("word"))

    assert result is not None
    assert result.source_text == "hello world"
    assert result.target_text == "[eng_Latn->zho_Hant] hello world"
    assert result.pack_id == "en-zhHant"
    assert len(translator.calls) == 1


def test_draft_skips_translation_when_policy_forbids(
    tmp_path: Path, translator: FakeTranslator
) -> None:
    """見 §8：日文 SOV 語序，暫定稿只顯示原文。"""
    pipeline, _ = make_pipeline(
        tmp_path,
        [ja_pack()],
        [ASRResult(text="行きます", language="ja", no_speech_prob=0.01)],
        translator,
    )
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("char"))

    assert result is not None
    assert result.source_text == "行きます"
    assert result.target_text is None
    assert result.pack_id == "ja-zhHant"
    assert len(translator.calls) == 0  # 根本不該呼叫翻譯引擎


def test_draft_returns_none_for_hallucination(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="Thank you.", language="en", no_speech_prob=0.9)],
        translator,
    )
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("word"))
    assert result is None


def test_draft_shows_original_text_when_no_pack_matches(
    tmp_path: Path, translator: FakeTranslator
) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],  # 只啟用英文包
        [ASRResult(text="こんにちは", language="ja", no_speech_prob=0.01)],  # 但音訊是日文
        translator,
    )
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("word"))

    assert result is not None
    assert result.source_text == "こんにちは"
    assert result.target_text is None
    assert result.pack_id is None


def test_draft_stabilizer_receives_text_and_tracks_state(
    tmp_path: Path, translator: FakeTranslator
) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="I think", language="en", no_speech_prob=0.01)],
        translator,
    )
    stabilizer = Stabilizer("word")
    result = pipeline.process_draft(None, forced_language=None, stabilizer=stabilizer)
    assert result.stable_chars == 0  # 第一次解碼，還沒有東西可比對


# --- 定稿 ---


def test_final_uses_beam5_and_pushes_context(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, asr = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="Hello there.", language="en", no_speech_prob=0.01)],
        translator,
    )
    context = TranslationContext(max_sentences=5)
    result = pipeline.process_final(None, forced_language=None, context=context)

    assert asr.calls[0]["beam_size"] == 5
    assert result.target_text is not None
    assert len(context) == 1  # 翻完自動 push 進 context


def test_final_passes_prior_context_to_asr_prompt(
    tmp_path: Path, translator: FakeTranslator
) -> None:
    pipeline, asr = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="Second sentence.", language="en", no_speech_prob=0.01)],
        translator,
    )
    context = TranslationContext(max_sentences=5)
    context.push("First sentence.", "第一句。")

    pipeline.process_final(None, forced_language=None, context=context)

    assert asr.calls[0]["initial_prompt"] == "First sentence."


def test_final_with_no_context_has_no_prompt(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, asr = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="hello", language="en", no_speech_prob=0.01)],
        translator,
    )
    pipeline.process_final(None, forced_language=None, context=TranslationContext())
    assert asr.calls[0]["initial_prompt"] is None


def test_final_returns_none_for_hallucination(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="Thank you.", language="en", no_speech_prob=0.95)],
        translator,
    )
    result = pipeline.process_final(None, forced_language=None, context=TranslationContext())
    assert result is None


def test_final_no_pack_shows_original_no_context_push(
    tmp_path: Path, translator: FakeTranslator
) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack()],
        [ASRResult(text="こんにちは", language="ja", no_speech_prob=0.01)],
        translator,
    )
    context = TranslationContext()
    result = pipeline.process_final(None, forced_language=None, context=context)

    assert result.target_text is None
    assert len(context) == 0  # 沒翻成功，不該汙染上下文


# --- 術語表 ---


def test_glossary_applied_during_translation(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack(translate={
            "local_engine": "ct2-nllb",
            "src_code": "eng_Latn",
            "tgt_code": "zho_Hant",
            "glossary": "glossary.tsv",
        })],
        [ASRResult(text="Alice said hi.", language="en", no_speech_prob=0.01)],
        translator,
    )
    # 術語表路徑是相對於「語言包自己的資料夾」解析的（見
    # contracts/langpack_schema.py 的 _resolve_optional_path），
    # 不是相對於 tmp_path 根目錄——make_registry() 已經把 pack.yaml
    # 建在 tmp_path/en-zhHant/ 底下，glossary.tsv 要放在同一個資料夾。
    (tmp_path / "en-zhHant" / "glossary.tsv").write_text("Alice\t愛麗絲\n", encoding="utf-8")
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("word"))

    # FakeTranslator 原樣把送進去的文字包回來，所以可以直接檢查佔位符有沒有
    # 被送進去（代表術語替換發生了），以及最終輸出有沒有換回目標譯法。
    assert "Alice" not in translator.calls[0]["text"]  # 送進翻譯引擎前已經替換成佔位符
    assert "愛麗絲" in result.target_text  # 佔位符換回來了


# --- local_engine="none" ---


def test_local_engine_none_returns_original_text(tmp_path: Path, translator: FakeTranslator) -> None:
    pipeline, _ = make_pipeline(
        tmp_path,
        [en_pack(translate={"local_engine": "none", "src_code": "eng_Latn", "tgt_code": "zho_Hant"})],
        [ASRResult(text="hello", language="en", no_speech_prob=0.01)],
        translator,
    )
    result = pipeline.process_draft(None, forced_language=None, stabilizer=Stabilizer("word"))

    assert result.target_text == "hello"  # 沒有可用引擎，原樣返回
    assert len(translator.calls) == 0
