"""測試共用設定。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_data_dir(tmp_path, monkeypatch):
    """每個測試都用獨立的使用者資料目錄：測試不能讀到（或寫進）開發者本機
    真實匯入過的語言包，否則「內建剛好 4 個包」這類斷言會因機器而異。"""
    monkeypatch.setenv("SAS_USER_DATA_DIR", str(tmp_path / "sas-user-data"))
