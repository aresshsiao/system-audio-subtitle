"""ui/panel/langpack_manager.py 的測試：假的請求函式與檔案挑選函式，不需要跑
inference-service。真實跨進程行為見 ROADMAP.md M3/M6。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from contracts.messages import SetActiveLangPacks, SetActiveLangPacksAck
from inference.langpack import LangPackRegistry
from ui.panel.langpack_manager import LangPackManagerDialog

pytestmark = pytest.mark.slow  # 需要 QApplication

ROOT = Path(__file__).resolve().parent.parent
KO_EXAMPLE = ROOT / "docs" / "examples" / "langpacks" / "ko-zhHant"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class FakeInference:
    def __init__(self, active: list[str] | None = None, fail: bool = False) -> None:
        self.active = active if active is not None else ["en-zhHant", "ja-zhHant", "th-zhHant", "zhHant-en"]
        self.fail = fail
        self.sent: list[SetActiveLangPacks] = []

    def __call__(self, msg: SetActiveLangPacks):
        self.sent.append(msg)
        if self.fail:
            raise OSError("down")
        if msg.pack_ids is not None:
            self.active = list(msg.pack_ids)
        return SetActiveLangPacksAck(success=True, active_pack_ids=list(self.active))


def make(fake: FakeInference, **kw) -> LangPackManagerDialog:
    return LangPackManagerDialog(request_fn=fake, **kw)


def test_lists_all_builtin_langpacks(qapp) -> None:
    dialog = make(FakeInference())
    registry = LangPackRegistry()
    registry.reload()
    assert set(dialog._checkboxes) == {p.id for p in registry.all_packs()}
    assert len(dialog._checkboxes) == 4


def test_reflects_actual_active_state_not_a_default_guess(qapp) -> None:
    """M3 的已知限制：舊版開啟時預設全選，不知道實際狀態。現在會查詢。"""
    dialog = make(FakeInference(active=["en-zhHant", "ja-zhHant"]))
    checked = {pid for pid, cb in dialog._checkboxes.items() if cb.isChecked()}
    assert checked == {"en-zhHant", "ja-zhHant"}
    assert dialog._auto_radio.isChecked()


def test_single_active_pack_opens_in_lock_mode(qapp) -> None:
    dialog = make(FakeInference(active=["th-zhHant"]))
    assert dialog._lock_radio.isChecked()
    assert dialog._lock_combo.currentData() == "th-zhHant"
    assert dialog.selected_pack_ids() == ["th-zhHant"]
    assert not any(cb.isEnabled() for cb in dialog._checkboxes.values())


def test_query_failure_falls_back_to_all_checked_and_says_so(qapp) -> None:
    dialog = make(FakeInference(fail=True))
    assert all(cb.isChecked() for cb in dialog._checkboxes.values())
    assert "連不上" in dialog._status_label.text()


def test_apply_sends_selection_with_reload_and_reports_mode(qapp) -> None:
    fake = FakeInference()
    dialog = make(fake)
    for pid, cb in dialog._checkboxes.items():
        cb.setChecked(pid in ("en-zhHant", "ja-zhHant"))
    dialog._apply()

    sent = fake.sent[-1]
    assert sorted(sent.pack_ids) == ["en-zhHant", "ja-zhHant"] and sent.reload is True
    assert "自動路由" in dialog._status_label.text()

    dialog._lock_radio.setChecked(True)
    dialog._lock_combo.setCurrentIndex(dialog._lock_combo.findData("ja-zhHant"))
    dialog._apply()
    assert fake.sent[-1].pack_ids == ["ja-zhHant"] and "鎖定" in dialog._status_label.text()


def test_apply_with_nothing_selected_shows_warning_not_crash(qapp, monkeypatch) -> None:
    dialog = make(FakeInference())
    for cb in dialog._checkboxes.values():
        cb.setChecked(False)
    warned = {}
    monkeypatch.setattr("ui.panel.langpack_manager.QMessageBox.warning", lambda *a, **k: warned.setdefault("called", True))
    dialog._apply()
    assert warned.get("called") is True


def test_import_korean_pack_shows_up_and_can_be_applied(qapp) -> None:
    fake = FakeInference()
    dialog = make(fake, pick_folder_fn=lambda _p: str(KO_EXAMPLE))
    assert "ko-zhHant" not in dialog._checkboxes

    dialog._import_folder()

    assert "ko-zhHant" in dialog._checkboxes
    assert "（匯入）" in dialog._checkboxes["ko-zhHant"].text()
    assert "已匯入" in dialog._status_label.text()

    dialog._lock_radio.setChecked(True)
    dialog._lock_combo.setCurrentIndex(dialog._lock_combo.findData("ko-zhHant"))
    dialog._apply()
    assert fake.sent[-1].pack_ids == ["ko-zhHant"] and fake.sent[-1].reload


def test_bad_import_shows_readable_error_and_changes_nothing(qapp, monkeypatch, tmp_path) -> None:
    (tmp_path / "pack.yaml").write_text("schema_version: 1\nid: broken\n", encoding="utf-8")
    shown = []
    monkeypatch.setattr("ui.panel.langpack_manager.QMessageBox.warning", lambda _p, title, text: shown.append(text))
    dialog = make(FakeInference(), pick_folder_fn=lambda _p: str(tmp_path))
    before = set(dialog._checkboxes)

    dialog._import_folder()

    assert shown and "缺少必要欄位" in shown[0]
    assert "匯入失敗" in dialog._status_label.text()
    assert set(dialog._checkboxes) == before


def test_remove_imported_pack(qapp) -> None:
    dialog = make(FakeInference(), pick_folder_fn=lambda _p: str(KO_EXAMPLE))
    dialog._import_folder()
    dialog._remove("ko-zhHant")
    assert "ko-zhHant" not in dialog._checkboxes
    assert "已移除" in dialog._status_label.text()
