"""兩段式編排 + 語言包路由。見 ARCHITECTURE.md §8、§9、§13.3。

這支檔案只管「決策」——用哪個語言包、要不要翻暫定稿、翻譯要不要帶上下文、
術語表怎麼套用——不管「怎麼跟 ring buffer/ZMQ 打交道」（那是
`inference/service.py` 的事）。ASR 引擎、翻譯引擎都用依賴注入傳進來，
所以這支檔案的邏輯可以完全不碰真正的 GPU/模型就測（見
tests/test_pipeline.py 用假引擎測路由決策）。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.langpack_schema import LanguagePack
from inference.asr.base import ASREngine
from inference.asr.hallucination import is_hallucination, load_blacklist
from inference.langpack import LangPackRegistry
from inference.stabilizer import Stabilizer
from inference.translate.base import Translator
from inference.translate.context import TranslationContext
from inference.translate.glossary import Glossary, load_glossary

DRAFT_BEAM_SIZE = 1
FINAL_BEAM_SIZE = 5


@dataclass(frozen=True)
class DecodeResult:
    """一次解碼 + （可能的）翻譯的完整結果。`target_text is None` 有兩種
    情況：語言包政策決定這個階段不翻（例如日文暫定稿，見 §8），或是根本
    找不到對應的語言包——呼叫端（inference/service.py）都一樣顯示原文，
    不需要區分這兩種情況。
    """

    source_text: str
    target_text: str | None
    src_lang: str
    tgt_lang: str | None
    pack_id: str | None
    engine_name: str | None
    stable_chars: int  # 只在暫定稿階段有意義，定稿永遠等於 len(source_text)


class TranslatorRegistry:
    """`local_engine` 名稱 → Translator 實例的對照表。名稱來自語言包的
    `translate.local_engine`（已經在 contracts/langpack_schema.py 驗證過
    是白名單內的值），這裡不做額外檢查。
    """

    def __init__(self, translators: dict[str, Translator]) -> None:
        self._translators = translators

    def get(self, name: str) -> Translator | None:
        return self._translators.get(name)


class Pipeline:
    def __init__(
        self,
        asr_engine: ASREngine,
        langpack_registry: LangPackRegistry,
        translator_registry: TranslatorRegistry,
    ) -> None:
        self._asr = asr_engine
        self._registry = langpack_registry
        self._translators = translator_registry
        self._glossary_cache: dict[str, Glossary] = {}
        self._blacklist_cache: dict[str, frozenset[str]] = {}

    def granularity_for(self, pack_id: str | None) -> str:
        """給呼叫端（inference/service.py）決定 `Stabilizer.granularity` 用。

        存在的理由：`Stabilizer` 要在收到第一句 `Utterance` OPEN 事件時就
        建立（那時候還沒解碼過，不知道是哪個語言），但正確的比對粒度
        要等第一次 ASR 解碼、知道語言包之後才查得到。呼叫端的作法是：
        每次解碼完，用這個方法查出粒度、更新 `stabilizer.granularity`，
        下一次呼叫才會用上——這代表第一次到第二次解碼之間，粒度可能還
        是預設的 "word"，是已知、可接受的一次性誤差（見
        inference/service.py 的說明），不是這支方法該解決的事。
        """
        if pack_id:
            pack = self._registry.get(pack_id)
            if pack is not None:
                return pack.stabilizer_granularity
        return "word"

    def _glossary_for(self, pack: LanguagePack) -> Glossary:
        if pack.id not in self._glossary_cache:
            self._glossary_cache[pack.id] = load_glossary(pack.translate.glossary_path)
        return self._glossary_cache[pack.id]

    def _blacklist_for(self, pack: LanguagePack) -> frozenset[str]:
        if pack.id not in self._blacklist_cache:
            self._blacklist_cache[pack.id] = load_blacklist(pack.asr.hallucination_blacklist_path)
        return self._blacklist_cache[pack.id]

    def _translate_with_glossary(self, pack: LanguagePack, text: str) -> str:
        translator = self._translators.get(pack.translate.local_engine)
        if translator is None:
            return text  # local_engine="none" 或引擎沒設定好：原樣返回，不假裝翻了

        glossary = self._glossary_for(pack)
        placeholder_text, mapping = glossary.apply_placeholders(text)
        translated = translator.translate(
            placeholder_text, src_code=pack.translate.src_code, tgt_code=pack.translate.tgt_code
        )
        return Glossary.restore_placeholders(translated, mapping)

    def process_draft(
        self, audio, *, forced_language: str | None, stabilizer: Stabilizer
    ) -> DecodeResult | None:
        """暫定稿：beam=1、不帶上下文。回傳 None 代表這段音訊被判定為幻覺
        或空白，呼叫端不該發布任何東西。
        """
        result = self._asr.transcribe(audio, language=forced_language, beam_size=DRAFT_BEAM_SIZE)

        pack = self._registry.find_by_src_lang(result.language)
        blacklist = self._blacklist_for(pack) if pack is not None else frozenset()
        if is_hallucination(result.text, result.no_speech_prob, blacklist=blacklist):
            return None

        stable_chars = stabilizer.update(result.text)

        if pack is None or not pack.policy.draft_translate:
            # 見 §8「日文的語序問題」：SOV 語序的語言，暫定稿只顯示原文。
            # 語言包本身找不到時也是同樣的處理——不假裝翻了。
            return DecodeResult(
                source_text=result.text,
                target_text=None,
                src_lang=result.language,
                tgt_lang=pack.tgt_lang if pack else None,
                pack_id=pack.id if pack else None,
                engine_name=None,
                stable_chars=stable_chars,
            )

        target_text = self._translate_with_glossary(pack, result.text)
        return DecodeResult(
            source_text=result.text,
            target_text=target_text,
            src_lang=result.language,
            tgt_lang=pack.tgt_lang,
            pack_id=pack.id,
            engine_name=pack.translate.local_engine,
            stable_chars=stable_chars,
        )

    def process_final(
        self, audio, *, forced_language: str | None, context: TranslationContext
    ) -> DecodeResult | None:
        """定稿：beam=5 + 前文 prompt，翻譯一律帶上下文（見 §8、§9）。
        成功翻譯後會把這句加進 `context`，呼叫端不需要自己記得 push。
        """
        initial_prompt = self._build_asr_prompt(context)
        result = self._asr.transcribe(
            audio,
            language=forced_language,
            beam_size=FINAL_BEAM_SIZE,
            initial_prompt=initial_prompt,
        )

        pack = self._registry.find_by_src_lang(result.language)
        blacklist = self._blacklist_for(pack) if pack is not None else frozenset()
        if is_hallucination(result.text, result.no_speech_prob, blacklist=blacklist):
            return None

        if pack is None:
            return DecodeResult(
                source_text=result.text,
                target_text=None,
                src_lang=result.language,
                tgt_lang=None,
                pack_id=None,
                engine_name=None,
                stable_chars=len(result.text),
            )

        num_context = len(context)
        translate_input = context.build_input(result.text)
        raw_output = self._translate_with_glossary(pack, translate_input)
        target_text = context.extract_current_translation(
            raw_output, num_context_sentences=num_context
        )
        context.push(result.text, target_text)

        return DecodeResult(
            source_text=result.text,
            target_text=target_text,
            src_lang=result.language,
            tgt_lang=pack.tgt_lang,
            pack_id=pack.id,
            engine_name=pack.translate.local_engine,
            stable_chars=len(result.text),
        )

    @staticmethod
    def _build_asr_prompt(context: TranslationContext) -> str | None:
        """定稿解碼時，把前文的「原文」當 Whisper 的 initial_prompt——這跟
        翻譯用的上下文是分開的兩件事：ASR 的 prompt 只是給模型語境線索，
        不影響輸出格式；`TranslationContext.build_input()` 才是真正組出
        要送進翻譯引擎的完整輸入。
        """
        if len(context) == 0:
            return None
        return " ".join(context.recent_sources(2))
