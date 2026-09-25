"""CaptureController 切換決策 + 換來源時管線對齊的單元測試（全用假 backend）。"""

from __future__ import annotations

import numpy as np
import pytest

from audio.capture.base import AudioFormat
from audio.capture_controller import CaptureController, parse_target_spec
from audio.segmenter import Segmenter
from audio.service import realign_pipeline
from audio.vad import FRAME_SAMPLES
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget


class FakeBackend:
    def __init__(self, kind: CaptureTargetKind, fail: bool = False) -> None:
        self.kind = kind
        self.fail = fail
        self.opened = False
        self.closed = False
        self.format = AudioFormat(sample_rate=48000, channels=2)

    def open(self, target: CaptureTarget) -> None:
        if self.fail:
            raise OSError("boom")
        self.opened = True

    def read(self):
        return None

    def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, fail_kinds: set[CaptureTargetKind] = frozenset()) -> None:
        self.fail_kinds = fail_kinds
        self.made: list[FakeBackend] = []

    def __call__(self, kind: CaptureTargetKind) -> FakeBackend:
        backend = FakeBackend(kind, fail=kind in self.fail_kinds)
        self.made.append(backend)
        return backend


ENDPOINT = CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="default")
PROCESS = CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=99999999)


def test_switch_closes_old_only_after_new_opened() -> None:
    factory = Factory()
    ctl = CaptureController(factory)
    ctl.open_initial(ENDPOINT)
    first = ctl.backend

    ack = ctl.switch(PROCESS)

    assert ack.success and ack.warning is None
    assert ack.active_kind == CaptureTargetKind.PROCESS
    assert first.closed and ctl.backend is not first and ctl.backend.opened


def test_process_failure_falls_back_to_endpoint_with_warning() -> None:
    factory = Factory(fail_kinds={CaptureTargetKind.PROCESS})
    ctl = CaptureController(factory)
    ctl.open_initial(ENDPOINT)

    ack = ctl.switch(PROCESS)

    assert ack.success
    assert ack.active_kind == CaptureTargetKind.ENDPOINT
    assert ack.warning and "boom" in ack.warning
    assert ctl.target.kind == CaptureTargetKind.ENDPOINT


def test_process_failure_at_startup_also_falls_back() -> None:
    ctl = CaptureController(Factory(fail_kinds={CaptureTargetKind.PROCESS}))
    ack = ctl.open_initial(PROCESS)
    assert ack.active_kind == CaptureTargetKind.ENDPOINT and ack.warning


def test_endpoint_failure_keeps_old_backend_running() -> None:
    factory = Factory()
    ctl = CaptureController(factory)
    ctl.open_initial(ENDPOINT)
    old = ctl.backend
    factory.fail_kinds = {CaptureTargetKind.ENDPOINT}

    ack = ctl.switch(CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="7"))

    assert not ack.success and ack.error
    assert ctl.backend is old and not old.closed


def test_total_failure_keeps_old_backend() -> None:
    factory = Factory()
    ctl = CaptureController(factory)
    ctl.open_initial(ENDPOINT)
    old = ctl.backend
    factory.fail_kinds = {CaptureTargetKind.PROCESS, CaptureTargetKind.ENDPOINT}

    ack = ctl.switch(PROCESS)

    assert not ack.success
    assert ctl.backend is old and not old.closed
    # 失敗開啟的 backend 也要被關掉，不能洩漏
    assert all(b.closed for b in factory.made if b.fail)


def test_virtual_cable_resolved_to_endpoint_backend() -> None:
    factory = Factory()
    ctl = CaptureController(factory, cable_finder=lambda: "42")

    ack = ctl.switch(CaptureTarget(kind=CaptureTargetKind.VIRTUAL_CABLE))

    assert ack.success and ack.active_kind == CaptureTargetKind.VIRTUAL_CABLE
    assert ctl.target.device_id == "42"


def test_virtual_cable_missing_reports_guidance() -> None:
    ctl = CaptureController(Factory(), cable_finder=lambda: None)

    ack = ctl.switch(CaptureTarget(kind=CaptureTargetKind.VIRTUAL_CABLE))

    assert not ack.success and "VB-CABLE" in ack.error
    assert ctl.backend is None


def test_parse_target_spec() -> None:
    assert parse_target_spec(None).device_id == "default"
    assert parse_target_spec("endpoint:5").device_id == "5"
    t = parse_target_spec("process:123:notree")
    assert t.kind == CaptureTargetKind.PROCESS and t.pid == 123 and not t.include_process_tree
    with pytest.raises(ValueError):
        parse_target_spec("bogus")


# --- Segmenter.flush / realign_pipeline -----------------------------------


def _speak(seg: Segmenter, frames: int):
    events = [seg.process_frame(0.9) for _ in range(frames)]
    return [e for e in events if e]


def test_flush_closes_open_utterance_and_keeps_counter() -> None:
    seg = Segmenter()
    opened = _speak(seg, 10)
    assert len(opened) == 1 and not opened[0].closed

    closing = seg.flush()

    assert closing.closed and closing.utt_id == opened[0].utt_id
    assert closing.span.end_sample == 10 * FRAME_SAMPLES
    # 計數沒有歸零：下一句的 span 接在後面
    nxt = _speak(seg, 10)
    assert nxt[0].span.start_sample >= 10 * FRAME_SAMPLES


def test_flush_when_silent_returns_none() -> None:
    seg = Segmenter()
    seg.process_frame(0.0)
    assert seg.flush() is None


class FakeVad:
    def __init__(self, pending: int) -> None:
        self.pending_samples = pending
        self.pushed: list[int] = []
        self.was_reset = False

    def push(self, samples: np.ndarray) -> list[float]:
        self.pushed.append(len(samples))
        total = self.pending_samples + len(samples)
        self.pending_samples = total % FRAME_SAMPLES
        return [0.9] * (total // FRAME_SAMPLES)

    def reset(self) -> None:
        self.was_reset = True
        self.pending_samples = 0


class FakeRing:
    def __init__(self) -> None:
        self.written = 0

    def write(self, samples: np.ndarray) -> None:
        self.written += len(samples)


def test_realign_pads_pending_flushes_and_resets_vad() -> None:
    seg = Segmenter()
    _speak(seg, 10)
    vad, ring = FakeVad(pending=100), FakeRing()

    event = realign_pipeline(vad, seg, ring)

    assert ring.written == FRAME_SAMPLES - 100  # 補零也寫進 ring，兩邊位置才會一致
    assert vad.was_reset
    assert event is not None and event.closed
    assert event.span.end_sample == 11 * FRAME_SAMPLES  # 多算了補零那一框


def test_realign_with_no_pending_writes_nothing() -> None:
    seg = Segmenter()
    vad, ring = FakeVad(pending=0), FakeRing()
    assert realign_pipeline(vad, seg, ring) is None
    assert ring.written == 0 and vad.was_reset
