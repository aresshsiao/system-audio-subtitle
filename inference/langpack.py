"""語言包 registry。見 ARCHITECTURE.md §13.3：inference-service 啟動與熱
重載時建立 `{pack_id: LanguagePack}`，供 pipeline.py 依偵測到的來源語言
查表決定要用哪個語言包（ASR 選型、stabilizer 粒度、要不要翻暫定稿、
翻譯引擎與語言代碼、術語表）。

**新增一個語言配對不需要改這支檔案**——這支檔案只認 `contracts/
langpack_schema.py` 定義的介面，不認識任何特定語言，這是 §13 整個機制
唯一的存在理由（見 ROADMAP.md M6 的零程式碼驗收標準）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from contracts.langpack_schema import LangPackValidationError, LanguagePack, load_and_validate

logger = logging.getLogger(__name__)

DEFAULT_LANGPACKS_DIR = Path(__file__).resolve().parent.parent / "config" / "langpacks"


class LangPackRegistry:
    """掃描一或多個目錄，載入、驗證所有語言包，提供查表與熱重載。"""

    def __init__(self, search_dirs: list[Path] | None = None) -> None:
        self._search_dirs = search_dirs or [DEFAULT_LANGPACKS_DIR]
        self._packs: dict[str, LanguagePack] = {}
        self._enabled_ids: set[str] = set()

    def reload(self) -> None:
        """重新掃描所有目錄，驗證失敗的包會被記錄下來並跳過（不影響其他
        已經驗證通過的包）——一個壞掉的語言包不該讓整個系統開不起來。
        """
        packs: dict[str, LanguagePack] = {}
        for base_dir in self._search_dirs:
            if not base_dir.is_dir():
                continue
            for pack_dir in sorted(base_dir.iterdir()):
                if not pack_dir.is_dir():
                    continue
                try:
                    pack = load_and_validate(pack_dir)
                except LangPackValidationError as e:
                    logger.warning("語言包 %s 驗證失敗，跳過: %s", pack_dir, e)
                    continue
                if pack.id in packs:
                    logger.warning(
                        "語言包 id 重複: %s（%s 與之前載入的衝突），保留先載入的那個",
                        pack.id,
                        pack_dir,
                    )
                    continue
                packs[pack.id] = pack

        self._packs = packs
        # 熱重載後，先前啟用的 id 如果消失了（例如包被刪除），跟著移除。
        self._enabled_ids &= set(packs.keys())
        logger.info("語言包 registry 重新載入完成，共 %d 個: %s", len(packs), sorted(packs))

    def all_packs(self) -> list[LanguagePack]:
        return list(self._packs.values())

    def get(self, pack_id: str) -> LanguagePack | None:
        return self._packs.get(pack_id)

    def enable(self, pack_id: str) -> None:
        if pack_id not in self._packs:
            raise KeyError(f"未知的語言包 id: {pack_id}")
        self._enabled_ids.add(pack_id)

    def disable(self, pack_id: str) -> None:
        self._enabled_ids.discard(pack_id)

    def set_enabled(self, pack_ids: list[str]) -> None:
        """一次設定啟用集合（取代目前的，不是疊加）。UI 的下拉選單/複選
        走這個方法，語意是「使用者現在選的就是這些」。
        """
        unknown = [pid for pid in pack_ids if pid not in self._packs]
        if unknown:
            raise KeyError(f"未知的語言包 id: {unknown}")
        self._enabled_ids = set(pack_ids)

    @property
    def enabled_packs(self) -> list[LanguagePack]:
        return [self._packs[pid] for pid in self._enabled_ids if pid in self._packs]

    def find_by_src_lang(self, src_lang: str) -> LanguagePack | None:
        """在目前啟用的語言包裡，找第一個來源語言相符的。

        找不到時回傳 None——呼叫端（pipeline.py）該顯示原文並標記「未翻譯」，
        不是靜默丟棄或硬套一個不對的包（見 §13.3 的運作方式說明）。
        """
        for pack in self.enabled_packs:
            if pack.src_lang == src_lang:
                return pack
        return None
