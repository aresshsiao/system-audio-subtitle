"""降級階梯控制器。見 ARCHITECTURE.md §11。

**鐵則：音訊擷取永不阻塞**——跟不上時不是排隊等（那會讓延遲單調遞增），而是
逐級犧牲品質：

    L0 正常
    L1 定稿 beam 5 → 1
    L2 暫定稿更新間隔 250ms → 500ms
    L3 關閉雲端精修
    L4 換成較小的多語模型（見下）
    L5 丟棄過舊的未處理段落（字幕缺漏，最後手段）

這支檔案只有純決策邏輯（輸入觀測值、輸出目前級別與各級別對應的參數），不碰
GPU/模型，所以能用假的時鐘與觀測值把升降級與遲滯完整測過。真正套用參數
（換 beam、換模型）是 inference/service.py 的事。

**判定訊號**：暫定稿「實際間隔」（連續兩次暫定稿解碼的起點間距）> 目前級別
目標間隔 × 1.5，連續 N 次才升一級（單次超標可能只是一次 GC 或長句，不代表
持續過載）。**降級的恢復**不能看間隔本身（間隔的下限就是目前目標間隔，
負載回落了也看不出來），所以看「解碼耗時」相對於**低一級**目標間隔的餘裕。

**遲滯**：每次級別變動後有最短停留時間（升級短、恢復長），且恢復門檻
（解碼耗時 < 低一級升級門檻 × 0.7）比升級門檻嚴，避免在邊界反覆震盪。

**L4 與 ARCHITECTURE.md 原設計的差異**：原文寫 large-v3 → distil-large-v3，
但 distil-large-v3 是**純英文**模型，換上去日文/泰文會直接壞掉。這裡換成
同樣多語的較小模型（預設 `large-v3-turbo`，可用 `SAS_DEGRADE_MODEL` 改）。
而且新模型要事先在本機快取好才會啟用 L4（`l4_available`）——不能在過載的
當下才去下載幾百 MB 的模型，那只會讓情況更糟。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from contracts.enums import DegradeLevel

logger = logging.getLogger(__name__)

BASE_DRAFT_INTERVAL_S = 0.25
SLOW_DRAFT_INTERVAL_S = 0.5


@dataclass(frozen=True)
class DegradeParams:
    """某個級別下，各處該用的參數。級別越高，累積前面所有級別的降級。"""

    final_beam_size: int
    draft_interval_s: float
    cloud_polish_allowed: bool
    use_fallback_model: bool
    drop_stale: bool


def params_for(level: DegradeLevel, *, l4_available: bool = True) -> DegradeParams:
    return DegradeParams(
        final_beam_size=1 if level >= DegradeLevel.L1_FINAL_BEAM_DOWN else 5,
        draft_interval_s=(
            SLOW_DRAFT_INTERVAL_S
            if level >= DegradeLevel.L2_DRAFT_INTERVAL_UP
            else BASE_DRAFT_INTERVAL_S
        ),
        cloud_polish_allowed=level < DegradeLevel.L3_CLOUD_POLISH_OFF,
        use_fallback_model=level >= DegradeLevel.L4_MODEL_DOWNGRADE and l4_available,
        drop_stale=level >= DegradeLevel.L5_DROP_OLDEST,
    )


class DegradeController:
    def __init__(
        self,
        *,
        l4_available: bool = False,
        up_ratio: float = 1.5,
        down_margin: float = 0.7,
        up_count: int = 3,
        down_count: int = 20,
        min_dwell_up_s: float = 3.0,
        min_dwell_down_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.l4_available = l4_available
        self._up_ratio = up_ratio
        self._down_margin = down_margin
        self._up_count = up_count
        self._down_count = down_count
        self._min_dwell_up_s = min_dwell_up_s
        self._min_dwell_down_s = min_dwell_down_s
        self._clock = clock

        self.level = DegradeLevel.L0_NORMAL
        self.transitions_up = 0
        self.transitions_down = 0
        self._changed_at = clock()
        self._over_streak = 0
        self._under_streak = 0

    @property
    def params(self) -> DegradeParams:
        return params_for(self.level, l4_available=self.l4_available)

    def _next_up(self) -> DegradeLevel | None:
        if self.level >= DegradeLevel.L5_DROP_OLDEST:
            return None
        nxt = DegradeLevel(self.level + 1)
        if nxt == DegradeLevel.L4_MODEL_DOWNGRADE and not self.l4_available:
            # 沒有預先快取的小模型：L4 跳過，直接到 L5。
            nxt = DegradeLevel.L5_DROP_OLDEST
        return nxt

    def _next_down(self) -> DegradeLevel | None:
        if self.level == DegradeLevel.L0_NORMAL:
            return None
        nxt = DegradeLevel(self.level - 1)
        if nxt == DegradeLevel.L4_MODEL_DOWNGRADE and not self.l4_available:
            nxt = DegradeLevel.L3_CLOUD_POLISH_OFF
        return nxt

    def observe_draft(self, *, interval_s: float, decode_s: float) -> DegradeLevel | None:
        """每完成一次暫定稿解碼呼叫一次。回傳新級別（有變動時），沒變回傳 None。

        `interval_s`：這次解碼起點與上一次解碼起點的間距（同一句話內）。
        `decode_s`：這次解碼本身耗時。
        """
        target = self.params.draft_interval_s
        dwell = self._clock() - self._changed_at

        if interval_s > target * self._up_ratio:
            self._over_streak += 1
            self._under_streak = 0
        else:
            self._over_streak = 0
            # 恢復訊號：假設回到低一級，還會不會再度觸發升級？低一級的升級門檻
            # 是「間隔 > 目標 × up_ratio」，解碼耗時要明顯低於這條線
            # （× down_margin）才算有餘裕——比升級門檻嚴，這就是遲滯。
            lower = self._next_down()
            if lower is not None:
                lower_target = params_for(lower, l4_available=self.l4_available).draft_interval_s
                if decode_s < lower_target * self._up_ratio * self._down_margin:
                    self._under_streak += 1
                else:
                    self._under_streak = 0
            else:
                self._under_streak = 0

        if self._over_streak >= self._up_count and dwell >= self._min_dwell_up_s:
            nxt = self._next_up()
            if nxt is not None:
                return self._move(
                    nxt, reason=f"間隔 {interval_s:.2f}s > 目標 {target:.2f}s×{self._up_ratio}"
                )
        if self._under_streak >= self._down_count and dwell >= self._min_dwell_down_s:
            nxt = self._next_down()
            if nxt is not None:
                return self._move(nxt, reason=f"解碼耗時 {decode_s:.2f}s，負載已回落")
        return None

    def _move(self, new_level: DegradeLevel, *, reason: str) -> DegradeLevel:
        old = self.level
        if new_level > old:
            self.transitions_up += 1
        else:
            self.transitions_down += 1
        self.level = new_level
        self._changed_at = self._clock()
        self._over_streak = 0
        self._under_streak = 0
        logger.warning("降級階梯 %s → %s（%s）", old.name, new_level.name, reason)
        return new_level
