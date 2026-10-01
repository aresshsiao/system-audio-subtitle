"""inference/service.py 的降級接線測試：用真的 Pipeline / DegradeController /
PolishWorker，只把 ASR 引擎換成「故意很慢」的假引擎，把 ring buffer 與 publisher
換成假的。驗證的是「間隔量測 → 控制器 → 參數套用到 pipeline/polish → L5 丟棄」
這條接線，不需要 GPU，也不受這台機器上其他 GPU 負載干擾（實機負載測試見
ROADMAP.md M5）。
"""

from __future__ import annotations

import time

import numpy as np

from contracts.enums import DegradeLevel, SubtitleState
from contracts.messages import AudioSpan, Utterance
from inference.asr.base import ASRResult
from inference.degrade import DegradeController
from inference.langpack import LangPackRegistry
from inference.pipeline import Pipeline, TranslatorRegistry
from inference.service import (
    STALE_LAG_S,
    _ActiveUtterance,
    _EngineSwitcher,
    _handle_utterance_event,
    _run_draft_pass,
    _Runtime,
    apply_degrade_params,
)
from inference.stabilizer import Stabilizer
from inference.translate.context import TranslationContext
from inference.translate.llm_api import PolishWorker
from utils.metrics import Metrics


class SlowASR:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.beams: list[int] = []

    def transcribe(self, audio, *, language=None, beam_size=1, initial_prompt=None) -> ASRResult:
        self.beams.append(beam_size)
        time.sleep(self.delay_s)
        return ASRResult(text="hello there", language="en", no_speech_prob=0.0)


class FakeRing:
    def __init__(self, write_total: int = 0) -> None:
        self.write_total = write_total
        self.total_overrun_samples = 0

    def peek(self, start: int, end: int) -> np.ndarray:
        return np.zeros(min(max(end - start, 0), 16000), dtype=np.float32)


class FakePublisher:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, topic, message) -> None:
        self.published.append(message)


def make_runtime(asr: SlowASR, ring: FakeRing, **degrade_kw) -> _Runtime:
    registry = LangPackRegistry()
    registry.reload()
    registry.set_enabled(["en-zhHant"])
    metrics = Metrics()
    pipeline = Pipeline(asr, registry, TranslatorRegistry({}), metrics)
    rt = _Runtime(
        ring_reader=ring,
        pipeline=pipeline,
        publisher=FakePublisher(),
        context=TranslationContext(),
        metrics=metrics,
        degrade=DegradeController(min_dwell_up_s=0.0, min_dwell_down_s=0.0, **degrade_kw),
        polish=PolishWorker(None, metrics),
        engines=_EngineSwitcher(asr, lambda: asr),
        llm_config=None,
    )
    apply_degrade_params(rt, rt.degrade.params)
    return rt


def run_draft_loop(rt: _Runtime, n_passes: int) -> None:
    """跟 main() 一樣的節奏：間隔到了才跑一次暫定稿。"""
    active = _ActiveUtterance(utt_id="u1", start_sample=0, stabilizer=Stabilizer())
    done = 0
    while done < n_passes:
        now = time.monotonic()
        interval = rt.degrade.params.draft_interval_s
        if active.last_draft_started_at is None or now - active.last_draft_started_at >= interval:
            _run_draft_pass(active, rt, forced_language="en")
            done += 1
        else:
            time.sleep(0.005)


# 測試用的過載門檻縮小（0.3s），讓 0.4s 的假解碼就算過載，不用真的等 1 秒以上
FAST = dict(overload_interval_s=0.3, up_count=2, l4_available=False)


def test_slow_decode_escalates_and_applies_params_to_pipeline_and_polish() -> None:
    asr = SlowASR(delay_s=0.4)
    rt = make_runtime(asr, FakeRing(write_total=16000 * 5), **FAST)
    assert rt.pipeline.final_beam_size == 5 and rt.polish.allowed_by_degrade

    run_draft_loop(rt, n_passes=6)  # 第 2 次起才有間隔量測：L0→L1（第 3 次）→L2（第 5 次）

    assert rt.degrade.level == DegradeLevel.L2_DRAFT_INTERVAL_UP
    assert rt.pipeline.final_beam_size == 1  # L1 的效果還在（累積）
    assert rt.polish.allowed_by_degrade  # L3 才會關
    assert rt.metrics.snapshot()["counters"]["degrade_transitions_total"] == 2
    assert rt.metrics.snapshot()["gauges"]["draft_interval_target_s"] == 0.5


def test_sustained_overload_reaches_l3_polish_off_then_l5_skipping_uncached_l4() -> None:
    asr = SlowASR(delay_s=0.4)
    rt = make_runtime(asr, FakeRing(write_total=16000 * 5), **FAST)

    run_draft_loop(rt, n_passes=10)

    assert rt.degrade.level == DegradeLevel.L5_DROP_OLDEST
    assert not rt.polish.allowed_by_degrade  # 經過 L3
    assert rt.degrade.params.use_fallback_model is False  # L4 被跳過


def test_l5_drops_stale_utterance_instead_of_decoding_it() -> None:
    asr = SlowASR(delay_s=0.0)
    end = 16000 * 3
    ring = FakeRing(write_total=end + int(16000 * (STALE_LAG_S + 2)))  # 已經落後很久
    rt = make_runtime(asr, ring)
    rt.degrade.level = DegradeLevel.L5_DROP_OLDEST

    closed = Utterance(utt_id="old", span=AudioSpan(0, end), closed=True)
    result = _handle_utterance_event(closed, None, rt, forced_language="en")

    assert result is None
    assert asr.beams == []  # 沒有去解碼
    assert rt.publisher.published == []  # 也沒有發布任何字幕
    assert rt.metrics.snapshot()["counters"]["dropped_utterances_total"] == 1


def test_below_l5_the_same_stale_utterance_is_still_processed() -> None:
    asr = SlowASR(delay_s=0.0)
    end = 16000 * 3
    ring = FakeRing(write_total=end + int(16000 * (STALE_LAG_S + 2)))
    rt = make_runtime(asr, ring)  # L0

    closed = Utterance(utt_id="old", span=AudioSpan(0, end), closed=True)
    _handle_utterance_event(closed, None, rt, forced_language="en")

    assert len(asr.beams) == 1
    assert [m.state for m in rt.publisher.published] == [SubtitleState.FINAL]
    assert rt.metrics.snapshot()["histograms"]["final_queue_lag_ms"]["count"] == 1.0


def test_final_uses_degraded_beam_size() -> None:
    asr = SlowASR(delay_s=0.0)
    rt = make_runtime(asr, FakeRing(write_total=16000 * 4))
    rt.degrade.level = DegradeLevel.L1_FINAL_BEAM_DOWN
    apply_degrade_params(rt, rt.degrade.params)

    closed = Utterance(utt_id="u", span=AudioSpan(0, 16000 * 2), closed=True)
    _handle_utterance_event(closed, None, rt, forced_language="en")

    assert asr.beams == [1]


def test_recovery_restores_full_quality_params() -> None:
    asr = SlowASR(delay_s=0.0)
    rt = make_runtime(asr, FakeRing())
    rt.degrade.level = DegradeLevel.L3_CLOUD_POLISH_OFF
    apply_degrade_params(rt, rt.degrade.params)
    assert not rt.polish.allowed_by_degrade and rt.pipeline.final_beam_size == 1

    rt.degrade.level = DegradeLevel.L0_NORMAL
    apply_degrade_params(rt, rt.degrade.params)
    assert rt.polish.allowed_by_degrade and rt.pipeline.final_beam_size == 5


def test_engine_switcher_swaps_and_loads_fallback_lazily() -> None:
    built = []
    primary, fallback = object(), object()

    def factory():
        built.append(1)
        return fallback

    sw = _EngineSwitcher(primary, factory)
    assert sw.select(use_fallback=False) is primary and built == []
    assert sw.select(use_fallback=True) is fallback
    assert sw.select(use_fallback=True) is fallback and built == [1]  # 只載入一次
    assert sw.select(use_fallback=False) is primary  # 恢復時換回主模型
