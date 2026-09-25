"""ui/panel/cloud_polish.py 與 ui/panel/metrics_panel.py 的元件測試（假請求函式、
假 subscriber）。真實跨進程行為見 ROADMAP.md M5 的整合驗證。"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from contracts.messages import CloudPolishStatus, MetricsSnapshot, SetCloudPolish
from ui.panel.cloud_polish import CloudPolishDialog
from ui.panel.metrics_panel import MetricsPanel, format_level

pytestmark = pytest.mark.slow  # 需要 QApplication


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def status(**kw) -> CloudPolishStatus:
    base = dict(success=True, enabled=False, configured=True, endpoint_host="api.example.com")
    base.update(kw)
    return CloudPolishStatus(**base)


class FakeInference:
    """模擬 inference-service 端的開關狀態。"""

    def __init__(self, **kw) -> None:
        self.state = status(**kw)
        self.sent: list[SetCloudPolish] = []

    def __call__(self, msg: SetCloudPolish) -> CloudPolishStatus:
        self.sent.append(msg)
        if msg.enabled is not None:
            self.state = status(**{**self.state.__dict__, "enabled": msg.enabled})
        return self.state


def test_default_off_and_nothing_sent_but_a_query(qapp) -> None:
    fake = FakeInference()
    d = CloudPolishDialog(request_fn=fake, confirm_fn=lambda *_: True)
    assert not d._checkbox.isChecked()
    assert d._privacy_label.text() == ""
    assert [m.enabled for m in fake.sent] == [None]  # 只有查詢，沒有偷偷開啟


def test_enabling_requires_consent_naming_the_host(qapp) -> None:
    asked = []
    fake = FakeInference()

    def confirm(_parent, host):
        asked.append(host)
        return True

    d = CloudPolishDialog(request_fn=fake, confirm_fn=confirm)
    d._checkbox.click()

    assert asked == ["api.example.com"]  # 同意對話框明講資料去哪
    assert fake.state.enabled is True
    assert "api.example.com" in d._privacy_label.text()  # 開啟期間持續顯示提示


def test_declining_consent_sends_nothing_and_stays_off(qapp) -> None:
    fake = FakeInference()
    d = CloudPolishDialog(request_fn=fake, confirm_fn=lambda *_: False)
    d._checkbox.click()

    assert not d._checkbox.isChecked()
    assert [m.enabled for m in fake.sent] == [None]  # 沒有送出任何 enabled=True


def test_disabling_needs_no_consent(qapp) -> None:
    fake = FakeInference(enabled=True)
    called = []
    d = CloudPolishDialog(request_fn=fake, confirm_fn=lambda *_: called.append(1) or True)
    assert d._checkbox.isChecked() and d._privacy_label.text() != ""
    d._checkbox.click()

    assert called == [] and fake.state.enabled is False
    assert d._privacy_label.text() == ""


def test_unconfigured_disables_checkbox_and_explains(qapp) -> None:
    d = CloudPolishDialog(
        request_fn=lambda m: status(configured=False, endpoint_host=None), confirm_fn=lambda *_: True
    )
    assert not d._checkbox.isEnabled()
    assert "SAS_LLM_BASE_URL" in d._status_label.text()


def test_breaker_and_degrade_states_are_explained(qapp) -> None:
    d = CloudPolishDialog(
        request_fn=lambda m: status(enabled=True, breaker_open=True), confirm_fn=lambda *_: True
    )
    assert "退回純本地" in d._status_label.text()
    d = CloudPolishDialog(
        request_fn=lambda m: status(enabled=True, blocked_by_degrade=True),
        confirm_fn=lambda *_: True,
    )
    assert "負載" in d._status_label.text()


def test_connection_failure_does_not_crash(qapp) -> None:
    def boom(_m):
        raise OSError("down")

    d = CloudPolishDialog(request_fn=boom)
    assert "連線失敗" in d._status_label.text() and not d._checkbox.isEnabled()

    d = CloudPolishDialog(request_fn=lambda m: None)
    assert "逾時" in d._status_label.text()


# --- MetricsPanel ---


class FakeSubscriber:
    def __init__(self, snaps) -> None:
        self._snaps = list(snaps)

    def recv_typed(self, cls, *, timeout_ms=None):
        return self._snaps.pop(0) if self._snaps else None

    def close(self) -> None:
        pass


def snap(level: int = 0, **kw) -> MetricsSnapshot:
    hist = {"count": 10.0, "p50": 100.0, "p95": 250.0, "p99": 400.0, "max": 500.0}
    return MetricsSnapshot(
        degrade_level=level,
        histograms=kw.get("histograms", {"asr_final_ms": hist}),
        counters=kw.get("counters", {}),
        gauges=kw.get("gauges", {}),
    )


def test_metrics_panel_shows_level_and_percentiles(qapp) -> None:
    p = MetricsPanel(subscriber=FakeSubscriber([]))
    p.update_snapshot(snap(level=2, gauges={"degrade_transitions_up": 2.0}))

    assert "L2" in p._level_label.text() and "升級 2 次" in p._level_label.text()
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "定稿 ASR"
    assert [p._table.item(0, c).text() for c in (2, 3, 4)] == ["100ms", "250ms", "400ms"]


def test_metrics_panel_draws_only_latest_snapshot_and_counters(qapp) -> None:
    p = MetricsPanel(subscriber=FakeSubscriber([snap(0), snap(5, counters={"dropped_utterances_total": 3})]))
    p._poll()
    assert "L5" in p._level_label.text()
    assert "L5 丟棄句數 3" in p._counters_label.text()


def test_metrics_panel_skips_empty_histograms_and_unknown_levels(qapp) -> None:
    p = MetricsPanel(subscriber=FakeSubscriber([]))
    p.update_snapshot(snap(histograms={"asr_final_ms": {"count": 0.0, "p50": 0, "p95": 0, "p99": 0, "max": 0}}))
    assert p._table.rowCount() == 0
    assert format_level(99) == "L99"


def test_metrics_panel_warns_when_snapshots_stop_arriving(qapp) -> None:
    import time as _time

    p = MetricsPanel(subscriber=FakeSubscriber([]))
    p.update_snapshot(snap(level=1))
    p._last_snapshot_at = _time.monotonic() - 10  # 10 秒前收到最後一次
    p._poll()
    assert "沒有回報" in p._level_label.text() and "L1" in p._level_label.text()
