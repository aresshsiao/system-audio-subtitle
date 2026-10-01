"""套用 Profile：把 `runtime/profiles.py` 的宣告接到實際的 UI/控制通道上。
純邏輯（不建立任何 Qt 元件），接線用的函式由呼叫端傳入，方便測試。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from contracts.messages import SetActiveLangPacks, SetActiveLangPacksAck
from runtime.profiles import Profile, resolve_langpack_ids
from utils.paths import user_data_dir

logger = logging.getLogger(__name__)


@dataclass
class ApplyReport:
    messages: list[str] = field(default_factory=list)
    ok: bool = True

    def add(self, text: str, *, ok: bool = True) -> None:
        self.messages.append(text)
        self.ok = self.ok and ok


def apply_profile(
    profile: Profile,
    *,
    known_langpack_ids: set[str],
    request_fn: Callable[[SetActiveLangPacks], SetActiveLangPacksAck | None],
    set_font_scale: Callable[[float], None],
) -> ApplyReport:
    report = ApplyReport()
    set_font_scale(profile.font_scale)

    usable, missing = resolve_langpack_ids(profile, known_langpack_ids)
    if missing:
        report.add(f"這個情境要用的語言包沒有安裝：{', '.join(missing)}（請先匯入）", ok=False)
    if usable:
        try:
            ack = request_fn(SetActiveLangPacks(pack_ids=usable, reload=True))
        except Exception as e:  # noqa: BLE001
            ack = None
            logger.warning("套用 Profile 語言包失敗: %s", e)
        if ack is not None and ack.success:
            report.add(f"語言包：{', '.join(ack.active_pack_ids)}")
        else:
            report.add("語言包沒有套用成功（inference-service 沒有回應或拒絕）", ok=False)
    else:
        report.add("語言包維持目前設定（這個情境依素材手動選語言）")

    if profile.cloud_polish_hint:
        report.add("這個情境建議開啟「雲端精修」（會把字幕送往第三方，需要你自己到選單開啟）")
    return report


def autosave_transcript(text: str, *, now: datetime | None = None) -> Path | None:
    """結束程式時存逐字稿到使用者資料目錄。沒有內容就不建立空檔案。"""
    if not text.strip():
        return None
    now = now or datetime.now()
    out_dir = user_data_dir() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"transcript-{now:%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return path
