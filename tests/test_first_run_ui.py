"""ui/panel/first_run.py"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from runtime.preflight import CheckResult, Status
from runtime.profiles import Profile
from ui.panel.first_run import FirstRunDialog, mark_first_run_done, should_show_first_run

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


RESULTS = [
    CheckResult("NVIDIA GPU", Status.OK, "RTX 4070"),
    CheckResult("VAD 模型", Status.FAIL, "找不到", "跑 setup_models.py"),
    CheckResult("連接埠", Status.WARN, "被佔用：[8765]", "結束舊實例"),
]


def test_shows_status_icons_and_fix_only_for_problems(qapp) -> None:
    d = FirstRunDialog(check_fn=lambda: RESULTS)
    t = d._table
    assert [t.item(r, 0).text() for r in range(3)] == ["✔", "✖", "⚠"]
    assert "→" not in t.item(0, 2).text()
    assert "→ 跑 setup_models.py" in t.item(1, 2).text()
    assert "必要項目沒通過" in d._summary.text()  # 最差狀態決定摘要


def test_all_ok_summary(qapp) -> None:
    d = FirstRunDialog(check_fn=lambda: RESULTS[:1])
    assert "全部通過" in d._summary.text()


def test_recheck_reruns_checks(qapp) -> None:
    calls = []
    d = FirstRunDialog(check_fn=lambda: calls.append(1) or RESULTS[:1])
    d.run_checks()
    assert len(calls) == 2


def test_profile_selection_calls_back(qapp) -> None:
    chosen = []
    profiles = [Profile("a", "A", ()), Profile("b", "B", ("en-zhHant",))]
    d = FirstRunDialog(profiles=profiles, on_profile=chosen.append, check_fn=lambda: RESULTS[:1])
    d._profile_combo.setCurrentIndex(1)
    d._apply_profile()
    assert chosen == [profiles[1]]


def test_first_run_marker_written_when_dialog_closes(qapp) -> None:
    assert should_show_first_run()
    d = FirstRunDialog(check_fn=lambda: RESULTS[:1])
    d.reject()
    assert not should_show_first_run()
    mark_first_run_done()  # 冪等
    assert not should_show_first_run()
