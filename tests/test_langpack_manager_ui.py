"""ui/panel/langpack_manager.py 的測試：只測元件建構邏輯（列出的語言包
是否跟 registry 一致、預設勾選狀態），不需要真的跑 inference-service。
`_apply()` 送出控制請求的部分需要真實 GPU 推論的 inference-service 才能
完整驗證，已經用手動腳本測過（見 ROADMAP.md M3），這裡不重複做。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from inference.langpack import LangPackRegistry
from ui.panel.langpack_manager import LangPackManagerDialog

pytestmark = pytest.mark.slow  # 需要 QApplication


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_lists_all_builtin_langpacks(qapp) -> None:
    dialog = LangPackManagerDialog()
    registry = LangPackRegistry()
    registry.reload()

    expected_ids = {p.id for p in registry.all_packs()}
    assert set(dialog._checkboxes.keys()) == expected_ids
    assert len(expected_ids) == 4  # en-zhHant, ja-zhHant, th-zhHant, zhHant-en


def test_all_checkboxes_default_checked(qapp) -> None:
    """見檔案開頭的「已知限制」：沒有查詢端點，預設全選。"""
    dialog = LangPackManagerDialog()
    assert all(cb.isChecked() for cb in dialog._checkboxes.values())


def test_apply_with_nothing_selected_shows_warning_not_crash(qapp, monkeypatch) -> None:
    dialog = LangPackManagerDialog()
    for cb in dialog._checkboxes.values():
        cb.setChecked(False)

    warned = {}

    def fake_warning(*args, **kwargs):
        warned["called"] = True

    monkeypatch.setattr("ui.panel.langpack_manager.QMessageBox.warning", fake_warning)
    dialog._apply()  # 不該丟例外，該跳警告
    assert warned.get("called") is True
