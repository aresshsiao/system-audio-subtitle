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

**判定訊號**：暫定稿「實際間隔」（連續兩次暫定稿解碼的起點間距）> 絕對門檻
`overload_interval_s`（預設 1.0s：字幕已經肉眼可見地卡住），連續 N 次才升一級
（單次超標可能只是一次 GC 或長句，不代表持續過載）。**降級的恢復**看「解碼耗時」
< `recover_decode_s`（預設 0.6s）連續 N 次。兩個門檻之間（0.6~1.0s）是遲滯帶：
既不升也不降，避免在邊界反覆震盪；每次級別變動後另有最短停留時間。

**第二個訊號：定稿排隊延遲**（同樣是 M5 實機負載測試補的）：GPU 被壓力程式佔滿時，
暫定稿間隔 p95 只到 ~1.0s，但**定稿要等 7 秒以上才輪到處理**——使用者看到的是字幕
晚了好幾秒，而暫定稿間隔這個訊號完全沒抓到。所以每句定稿開始處理時，量「句末到現在」
的落後時間，連續兩句 > `overload_lag_s`（3s）就升級；落後還沒降下來時也不允許恢復。

**為什麼暫定稿間隔用絕對門檻，不是「間隔 > 目標 250ms × 1.5」**（M5 實機驗證時修正）：
暫定稿每次都要重新解碼「從句首到現在」整段音訊，成本隨句子變長而增加——
GPU 完全空閒時，8 秒長句的一次暫定稿解碼就要 ~450ms。原本以 250ms 目標推算的
門檻（375ms）在**空閒機器上**就會被長句觸發，系統一啟動就掉到 L2，「L0 正常」
形同虛設。250ms 是節奏目標，不是過載判準；過載該以使用者可感知的卡頓為準。

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
        overload_interval_s: float = 1.0,
        recover_decode_s: float = 0.6,
        overload_lag_s: float = 3.0,
        lag_up_count: int = 2,
        up_count: int = 3,
        down_count: int = 20,
        min_dwell_up_s: float = 3.0,
        min_dwell_down_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.l4_available = l4_available
        self._overload_interval_s = overload_interval_s
        self._recover_decode_s = recover_decode_s
        self._overload_lag_s = overload_lag_s
        self._lag_up_count = lag_up_count
        self._lag_over_streak = 0
        self._last_lag_s = 0.0
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
        dwell = self._clock() - self._changed_at

        if interval_s > self._overload_interval_s:
            self._over_streak += 1
            self._under_streak = 0
        else:
            self._over_streak = 0
            lag_ok = self._last_lag_s < self._overload_lag_s / 2
            if self._next_down() is not None and decode_s < self._recover_decode_s and lag_ok:
                self._under_streak += 1
            else:
                self._under_streak = 0

        if self._over_streak >= self._up_count and dwell >= self._min_dwell_up_s:
            nxt = self._next_up()
            if nxt is not None:
                return self._move(
                    nxt, reason=f"暫定稿間隔 {interval_s:.2f}s > {self._overload_interval_s:.1f}s"
                )
        if self._under_streak >= self._down_count and dwell >= self._min_dwell_down_s:
            nxt = self._next_down()
            if nxt is not None:
                return self._move(nxt, reason=f"解碼耗時 {decode_s:.2f}s，負載已回落")
        return None

    def observe_final_lag(self, lag_s: float) -> DegradeLevel | None:
        """每句定稿開始處理時呼叫：`lag_s` = 這句話說完到現在過了多久。"""
        self._last_lag_s = lag_s
        if lag_s > self._overload_lag_s:
            self._lag_over_streak += 1
            self._under_streak = 0
        else:
            self._lag_over_streak = 0
        dwell = self._clock() - self._changed_at
        if self._lag_over_streak >= self._lag_up_count and dwell >= self._min_dwell_up_s:
            nxt = self._next_up()
            if nxt is not None:
                return self._move(nxt, reason=f"定稿落後 {lag_s:.1f}s > {self._overload_lag_s:.0f}s")
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
        self._lag_over_streak = 0
        logger.warning("降級階梯 %s → %s（%s）", old.name, new_level.name, reason)
        return new_level
