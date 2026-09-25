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
        ctl.observe_draft(interval_s=ctl.params.draft_interval_s * 2, decode_s=0.6)


def test_single_slow_draft_does_not_escalate() -> None:
    clock = FakeClock()
    ctl = make(clock)
    clock.advance(10)
    assert ctl.observe_draft(interval_s=1.0, decode_s=0.9) is None
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
        # 間隔剛好達標（不觸發升級），但解碼耗時 0.3s 高於恢復門檻
        # （低一級升級門檻 0.375s × 0.7 = 0.26s）：不夠鬆，不恢復
        ctl.observe_draft(interval_s=ctl.params.draft_interval_s, decode_s=0.3)
    assert ctl.level == level
    assert (ctl.transitions_up, ctl.transitions_down) == (ups, downs)


def test_min_dwell_blocks_immediate_second_step() -> None:
    clock = FakeClock()
    ctl = DegradeController(clock=clock, min_dwell_up_s=100.0)
    clock.advance(200)
    for _ in range(3):
        ctl.observe_draft(interval_s=1.0, decode_s=0.9)
    assert ctl.level == DegradeLevel.L1_FINAL_BEAM_DOWN
    for _ in range(10):  # 級別剛變動，停留時間還沒到
        ctl.observe_draft(interval_s=1.0, decode_s=0.9)
    assert ctl.level == DegradeLevel.L1_FINAL_BEAM_DOWN
