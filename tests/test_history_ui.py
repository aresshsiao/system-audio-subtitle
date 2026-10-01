"""ui/panel/history.py：歷史列表、搜尋、複製、匯出、偏移共用設定。"""

from __future__ import annotations

import urllib.error

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from ui.panel.history import (
    ExportSettings,
    GatewayClient,
    HistoryDialog,
    format_offset,
    rows_from_history,
)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def item(utt: str, start_s: float, target: str, source: str = "src", state: str = "final") -> dict:
    return {
        "utt_id": utt,
        "state": state,
        "span": {"start_sample": int(start_s * 16000), "end_sample": int((start_s + 1) * 16000), "sample_rate": 16000},
        "target_text": target,
        "source_text": source,
    }


class FakeGateway:
    def __init__(self, history: list[dict]) -> None:
        self.history = history
        self.calls: list[tuple] = []

    def __call__(self, path: str, *, method: str = "GET") -> bytes:
        import json

        self.calls.append((method, path))
        if path == "/history":
            return json.dumps(self.history).encode()
        if path == "/history/clear":
            self.history = []
            return b'{"ok": true}'
        if path.startswith("/export"):
            return "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode()
        raise AssertionError(path)


HISTORY = [item("a", 1.0, "你好", "こんにちは"), item("b", 65.0, "再見", "さようなら"), item("c", 70.0, "殘留", state="draft")]


def make(fake, tmp_path=None, save_to=None, settings=None):
    settings = settings or ExportSettings()
    return HistoryDialog(
        settings, client=GatewayClient(fake), save_path_fn=lambda _p, fmt: str(save_to) if save_to else None
    ), settings


def test_rows_skip_drafts_and_format_timecodes(qapp) -> None:
    rows = rows_from_history(HISTORY)
    assert [r.label for r in rows] == ["[00:00:01] 你好", "[00:01:05] 再見"]


def test_dialog_lists_and_searches_source_or_target(qapp) -> None:
    d, _ = make(FakeGateway(HISTORY))
    assert d._list.count() == 2
    d._search.setText("さよう")  # 搜原文
    assert d._list.count() == 1 and "再見" in d._list.item(0).text()
    d._search.setText("你好")
    assert d._list.count() == 1
    d._search.setText("沒有這句")
    assert d._list.count() == 0


class FakeClipboard:
    text_value = ""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API 名稱
        self.text_value = text


def test_copy_selected_or_all(qapp, monkeypatch) -> None:
    # 不碰真的系統剪貼簿：測試不該覆蓋開發者正在用的剪貼簿內容，
    # 而且 Windows 剪貼簿是跨行程共享的，斷言會受其他程式影響
    clip = FakeClipboard()
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: clip))
    d, _ = make(FakeGateway(HISTORY))
    d.copy_selected()  # 沒選 → 全部
    assert clip.text_value == "[00:00:01] 你好\n[00:01:05] 再見"
    d._list.item(1).setSelected(True)
    d.copy_selected()
    assert clip.text_value == "[00:01:05] 再見"


def test_export_writes_file_with_current_offset_and_options(qapp, tmp_path) -> None:
    fake = FakeGateway(HISTORY)
    out = tmp_path / "out.srt"
    settings = ExportSettings()
    settings.set_offset_ms(-300)
    d, _ = make(fake, save_to=out, settings=settings)
    d._bilingual.setChecked(True)
    d._rebase.setChecked(True)

    assert d.export_to_file() == out

    method, path = fake.calls[-1]
    assert "fmt=srt" in path and "offset_ms=-300" in path and "bilingual=true" in path and "rebase=true" in path
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # SRT 用 UTF-8 BOM，舊播放器也認得
    assert "你好" in raw.decode("utf-8-sig")
    assert "-0.3s" in d._status.text()


def test_export_cancelled_or_empty_writes_nothing(qapp, tmp_path) -> None:
    d, _ = make(FakeGateway(HISTORY), save_to=None)
    assert d.export_to_file() is None

    class Empty(FakeGateway):
        def __call__(self, path, *, method="GET"):
            return b"" if path.startswith("/export") else super().__call__(path, method=method)

    out = tmp_path / "e.srt"
    d, _ = make(Empty(HISTORY), save_to=out)
    assert d.export_to_file() is None and not out.exists()
    assert "沒有可匯出" in d._status.text()


def test_gateway_down_is_reported_not_raised(qapp) -> None:
    def down(path, *, method="GET"):
        raise urllib.error.URLError("refused")

    d, _ = make(down)
    assert "連不上 gateway" in d._status.text() and d._list.count() == 0


def test_clear_history_refreshes(qapp) -> None:
    fake = FakeGateway(HISTORY)
    d, _ = make(fake)
    d._clear()
    assert ("POST", "/history/clear") in fake.calls and d._list.count() == 0


def test_offset_shared_between_hotkey_settings_and_spinbox(qapp) -> None:
    d, settings = make(FakeGateway(HISTORY))
    settings.nudge(+100)
    settings.nudge(+100)
    assert d._offset.value() == 200  # 熱鍵改的，面板跟著變
    d._offset.setValue(-500)
    assert settings.offset_ms == -500  # 面板改的，熱鍵狀態跟著變
    settings.set_offset_ms(10**9)
    assert settings.offset_ms == 600_000  # 有上限
    assert format_offset(-500) == "-0.5s" and format_offset(1200) == "+1.2s"
