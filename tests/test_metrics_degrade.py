"""utils/metrics.py 與 inference/degrade.py 的單元測試（假時鐘、假觀測值）。"""

from __future__ import annotations

from contracts.enums import DegradeLevel
from inference.degrade import DegradeController, params_for
from utils.metrics import Histogram, Metrics


def test_histogram_percentiles_are_nearest_rank() -> None:
    h = Histogram()
    for v in range(1, 101):
        h.observe(float(v))
    s = h.summary()
    assert (s.count, s.p50, s.p95, s.p99, s.max) == (100, 50.0, 95.0, 99.0, 100.0)


def test_histogram_is_sliding_window_not_lifetime_average() -> None:
    h = Histogram(window=10)
    for _ in range(100):
        h.observe(1000.0)  # 舊的慢樣本
    for _ in range(10):
        h.observe(5.0)  # 現在的狀況
    assert h.summary().p99 == 5.0


def test_empty_histogram_is_zeros() -> None:
    assert Histogram().summary().count == 0


def test_metrics_snapshot_shape_and_timer() -> None:
    m = Metrics()
    m.incr("x_total")
    m.incr("x_total", 2)
    m.set_gauge("g", 1.5)
    with m.timer("stage_ms") as t:
        pass
    snap = m.snapshot()
    assert snap["counters"]["x_total"] == 3
    assert snap["gauges"]["g"] == 1.5
    assert snap["histograms"]["stage_ms"]["count"] == 1.0
    assert t.elapsed_ms >= 0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def make(clock: FakeClock, **kw) -> DegradeController:
    return DegradeController(clock=clock, min_dwell_up_s=1.0, min_dwell_down_s=5.0, **kw)


def overload(ctl: DegradeController, clock: FakeClock, n: int) -> None:
    for _ in range(n):
        clock.advance(1.0)
        ctl.observe_draft(interval_s=2.0, decode_s=1.8)


def test_single_slow_draft_does_not_escalate() -> None:
    clock = FakeClock()
    ctl = make(clock)
    clock.advance(10)
    assert ctl.observe_draft(interval_s=2.0, decode_s=1.8) is None
    assert ctl.observe_draft(interval_s=0.25, decode_s=0.1) is None
    assert ctl.level == DegradeLevel.L0_NORMAL


def test_sustained_overload_escalates_one_level_at_a_time() -> None:
    clock = FakeClock()
    ctl = make(clock, l4_available=True)
    levels = []
    for _ in range(40):
        overload(ctl, clock, 1)
        levels.append(ctl.level)
    assert levels[-1] == DegradeLevel.L5_DROP_OLDEST
    # 逐級：相鄰兩步最多差一級
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:]))
    assert ctl.transitions_up == 5


def test_l4_skipped_when_fallback_model_not_cached() -> None:
    clock = FakeClock()
    ctl = make(clock, l4_available=False)
    seen = set()
    for _ in range(40):
        overload(ctl, clock, 1)
        seen.add(ctl.level)
    assert DegradeLevel.L4_MODEL_DOWNGRADE not in seen
    assert ctl.level == DegradeLevel.L5_DROP_OLDEST


def test_params_accumulate_across_levels() -> None:
    l0, l1, l2, l3, l4, l5 = (params_for(DegradeLevel(i)) for i in range(6))
    assert l0.final_beam_size == 5 and l1.final_beam_size == 1
    assert l1.draft_interval_s == 0.25 and l2.draft_interval_s == 0.5
    assert l2.cloud_polish_allowed and not l3.cloud_polish_allowed
    assert not l3.use_fallback_model and l4.use_fallback_model
    assert not l4.drop_stale and l5.drop_stale
    assert l5.final_beam_size == 1 and not l5.cloud_polish_allowed  # 累積


def test_recovers_stepwise_after_load_drops() -> None:
    clock = FakeClock()
    ctl = make(clock)
    overload(ctl, clock, 12)
    peak = ctl.level
    assert peak >= DegradeLevel.L2_DRAFT_INTERVAL_UP

    for _ in range(400):
        clock.advance(1.0)
        ctl.observe_draft(interval_s=ctl.params.draft_interval_s, decode_s=0.05)
    assert ctl.level == DegradeLevel.L0_NORMAL
    assert ctl.transitions_down == ctl.transitions_up  # 一級一級退回，沒有跳級


def test_hysteresis_prevents_flapping_at_the_boundary() -> None:
    """解碼耗時落在「不夠鬆也不夠緊」的灰色地帶：既不升也不降。"""
    clock = FakeClock()
    ctl = make(clock)
    overload(ctl, clock, 6)
    level = ctl.level
    ups, downs = ctl.transitions_up, ctl.transitions_down

    for _ in range(200):
        clock.advance(1.0)
        # 遲滯帶：間隔 0.85s 沒過升級門檻（1.0s），解碼耗時 0.8s 也沒低於恢復門檻
        # （0.6s）——不升也不降
        ctl.observe_draft(interval_s=0.85, decode_s=0.8)
    assert ctl.level == level
    assert (ctl.transitions_up, ctl.transitions_down) == (ups, downs)


def test_min_dwell_blocks_immediate_second_step() -> None:
    clock = FakeClock()
    ctl = DegradeController(clock=clock, min_dwell_up_s=100.0)
    clock.advance(200)
    for _ in range(3):
        ctl.observe_draft(interval_s=2.0, decode_s=1.8)
    assert ctl.level == DegradeLevel.L1_FINAL_BEAM_DOWN
    for _ in range(10):  # 級別剛變動，停留時間還沒到
        ctl.observe_draft(interval_s=2.0, decode_s=1.8)
    assert ctl.level == DegradeLevel.L1_FINAL_BEAM_DOWN


def test_idle_machine_with_long_utterances_stays_at_l0() -> None:
    """迴歸測試（M5 實機驗證踩到）：GPU 完全空閒時，8 秒長句的暫定稿解碼要 ~450ms、
    實際間隔 ~550ms。這是正常狀態，不能被當成過載——舊版用「間隔 > 250ms×1.5」
    當門檻，系統一啟動就掉到 L2。"""
    clock = FakeClock()
    ctl = make(clock)
    for _ in range(500):
        clock.advance(0.6)
        assert ctl.observe_draft(interval_s=0.55, decode_s=0.47) is None
    assert ctl.level == DegradeLevel.L0_NORMAL


def test_sustained_final_lag_escalates_even_when_draft_interval_looks_fine() -> None:
    """實機負載測試：暫定稿間隔 p95 ~1.0s（沒過門檻），定稿卻落後 7 秒才處理。"""
    clock = FakeClock()
    ctl = make(clock)
    clock.advance(10)
    assert ctl.observe_final_lag(7.0) is None  # 單次不動
    assert ctl.observe_final_lag(7.4) == DegradeLevel.L1_FINAL_BEAM_DOWN
    assert ctl.observe_draft(interval_s=0.6, decode_s=0.5) is None


def test_single_lag_spike_or_normal_lag_does_not_escalate() -> None:
    clock = FakeClock()
    ctl = make(clock)
    clock.advance(10)
    for lag in (0.5, 2.9, 0.6, 3.5, 0.7, 3.2, 0.5):  # 偶發超標、沒有連續
        assert ctl.observe_final_lag(lag) is None
    assert ctl.level == DegradeLevel.L0_NORMAL


def test_no_recovery_while_final_lag_is_still_high() -> None:
    clock = FakeClock()
    ctl = make(clock)
    overload(ctl, clock, 6)
    level = ctl.level
    ctl.observe_final_lag(2.0)  # 還沒降到 3s 的一半（1.5s）以下
    for _ in range(100):
        clock.advance(1.0)
        ctl.observe_draft(interval_s=0.3, decode_s=0.05)
    assert ctl.level == level
    ctl.observe_final_lag(0.5)
    for _ in range(100):
        clock.advance(1.0)
        ctl.observe_draft(interval_s=0.3, decode_s=0.05)
    assert ctl.level < level


def test_histogram_forgets_samples_older_than_max_age() -> None:
    clock = FakeClock()
    h = Histogram(max_age_s=60.0, clock=clock)
    h.observe(7000.0)  # 壓力期間的慢樣本
    clock.advance(30)
    h.observe(500.0)
    assert h.summary().max == 7000.0
    clock.advance(45)  # 慢樣本已是 75 秒前
    assert h.summary().max == 500.0
    clock.advance(100)
    assert h.summary().count == 0
