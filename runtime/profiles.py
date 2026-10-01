"""使用情境 Profile。見 ARCHITECTURE.md §14。

**Profile 與語言包是兩個正交的軸**：語言包回答「這個語言配對怎麼處理」，Profile
回答「這個使用情境要怎麼呈現」。Profile 只**引用**語言包 id，不重複定義任何語言
相關欄位。

目前 Profile 能控制的（都是真的有接線的）：

  - `langpack_ids`：套用時送 `SetActiveLangPacks`；空清單 = 不改動目前設定
    （例如 lecture：語言依素材而定，由使用者手動選）
  - `font_scale`：字幕字體倍率
  - `autosave_transcript`：結束程式時把逐字稿存到使用者資料目錄
  - `cloud_polish_hint`：**只是提示**。雲端精修會把內容送往第三方，任何 Profile
    都不會替使用者開啟它，只會提醒「這個情境建議開啟」

**還沒有**：ARCHITECTURE.md 原本提到的「更新頻率拉高」「暫定稿優先」這類
延遲/品質取捨旋鈕——目前延遲行為由降級階梯（inference/degrade.py）自動決定，
還沒有開放成可由 Profile 調整的參數。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from utils.paths import user_data_dir

logger = logging.getLogger(__name__)

BUILTIN_PROFILES_DIR = Path(__file__).resolve().parent.parent / "config" / "profiles"
_FONT_SCALE_RANGE = (0.5, 3.0)


class ProfileValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str
    langpack_ids: tuple[str, ...]
    font_scale: float = 1.0
    cloud_polish_hint: bool = False
    autosave_transcript: bool = False


def _require(raw: dict, key: str, path: Path):
    if key not in raw:
        raise ProfileValidationError(f"{path.name}: 缺少必要欄位 '{key}'")
    return raw[key]


def load_profile(path: Path) -> Profile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileValidationError(f"{path.name}: 不是合法的 YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ProfileValidationError(f"{path.name}: 內容必須是 key: value 的對照表")

    ids = raw.get("langpack_ids", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ProfileValidationError(f"{path.name}: langpack_ids 必須是字串清單")
    scale = raw.get("font_scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        raise ProfileValidationError(f"{path.name}: font_scale 必須是數字")
    if not _FONT_SCALE_RANGE[0] <= scale <= _FONT_SCALE_RANGE[1]:
        raise ProfileValidationError(f"{path.name}: font_scale 必須介於 {_FONT_SCALE_RANGE}，收到 {scale}")
    for flag in ("cloud_polish_hint", "autosave_transcript"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise ProfileValidationError(f"{path.name}: {flag} 必須是 true/false")

    return Profile(
        id=str(_require(raw, "id", path)),
        display_name=str(_require(raw, "display_name", path)),
        langpack_ids=tuple(ids),
        font_scale=float(scale),
        cloud_polish_hint=bool(raw.get("cloud_polish_hint", False)),
        autosave_transcript=bool(raw.get("autosave_transcript", False)),
    )


def load_profiles(dirs: list[Path] | None = None) -> list[Profile]:
    """內建 + 使用者目錄（`<user_data>/profiles`）。壞掉的檔案記警告並跳過，不影響其他。"""
    dirs = dirs if dirs is not None else [BUILTIN_PROFILES_DIR, user_data_dir() / "profiles"]
    profiles: dict[str, Profile] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.yaml")):
            try:
                profile = load_profile(path)
            except ProfileValidationError as e:
                logger.warning("Profile 無效，跳過: %s", e)
                continue
            profiles.setdefault(profile.id, profile)
    return list(profiles.values())


def resolve_langpack_ids(profile: Profile, known_ids: set[str]) -> tuple[list[str], list[str]]:
    """回傳 (可套用的 id, 找不到的 id)。Profile 引用了沒安裝的語言包時，套用其餘的
    並讓呼叫端提醒使用者，而不是整個失敗。"""
    usable = [i for i in profile.langpack_ids if i in known_ids]
    missing = [i for i in profile.langpack_ids if i not in known_ids]
    return usable, missing
