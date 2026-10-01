"""字幕歷史與匯出面板。見 ARCHITECTURE.md §14。

逐句回看、搜尋、複製；匯出成 SRT / VTT / 純文字。資料來自 gateway 的 HTTP
端點（`/history`、`/export`）——UI 是無狀態消費者，Session 在 gateway 那一端。

**偏移微調**：字幕檔的時間軸原點是「開始擷取」的那一刻，不是影片開頭
（見 gateway/export.py）。`ExportSettings.offset_ms` 由全域熱鍵（app.py）與這個
面板的數字欄共用同一份，兩邊改的是同一個值。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from contracts.topics import GATEWAY_HTTP_HOST, GATEWAY_HTTP_PORT
from utils.paths import user_data_dir

logger = logging.getLogger(__name__)

OFFSET_STEP_MS = 100
_OFFSET_LIMIT_MS = 600_000


class ExportSettings(QObject):
    """熱鍵與面板共用的匯出偏移。"""

    changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._offset_ms = 0

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    def set_offset_ms(self, value: int) -> None:
        value = max(-_OFFSET_LIMIT_MS, min(_OFFSET_LIMIT_MS, int(value)))
        if value != self._offset_ms:
            self._offset_ms = value
            self.changed.emit(value)

    def nudge(self, delta_ms: int) -> int:
        self.set_offset_ms(self._offset_ms + delta_ms)
        return self._offset_ms


def format_offset(offset_ms: int) -> str:
    return f"{offset_ms / 1000:+.1f}s"


def _default_fetch(path: str, *, method: str = "GET") -> bytes:
    url = f"http://{GATEWAY_HTTP_HOST}:{GATEWAY_HTTP_PORT}{path}"
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


class GatewayClient:
    def __init__(self, fetch: Callable[..., bytes] = _default_fetch) -> None:
        self._fetch = fetch

    def history(self) -> list[dict]:
        return json.loads(self._fetch("/history"))

    def export(
        self, fmt: str, *, offset_ms: int = 0, bilingual: bool = False, rebase: bool = False
    ) -> str:
        query = urllib.parse.urlencode(
            {
                "fmt": fmt,
                "offset_ms": offset_ms,
                "bilingual": str(bilingual).lower(),
                "rebase": str(rebase).lower(),
            }
        )
        return self._fetch(f"/export?{query}").decode("utf-8")

    def clear(self) -> None:
        self._fetch("/history/clear", method="POST")


def _timecode(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class HistoryRow:
    start_s: float
    state: str
    target: str
    source: str

    @property
    def label(self) -> str:
        return f"[{_timecode(self.start_s)}] {self.target}"

    def matches(self, needle: str) -> bool:
        needle = needle.strip().lower()
        return not needle or needle in self.target.lower() or needle in self.source.lower()


def rows_from_history(history: list[dict], *, include_drafts: bool = False) -> list[HistoryRow]:
    rows = []
    for item in history:
        if item["state"] == "draft" and not include_drafts:
            continue
        span = item["span"]
        rows.append(
            HistoryRow(
                start_s=span["start_sample"] / span["sample_rate"],
                state=item["state"],
                target=item["target_text"],
                source=item["source_text"],
            )
        )
    return rows


class HistoryDialog(QDialog):
    def __init__(
        self,
        settings: ExportSettings,
        parent: QWidget | None = None,
        *,
        client: GatewayClient | None = None,
        save_path_fn: Callable[[QWidget, str], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("字幕歷史與匯出")
        self.resize(560, 500)
        self._settings = settings
        self._client = client or GatewayClient()
        self._save_path_fn = save_path_fn or self._ask_save_path
        self._rows: list[HistoryRow] = []

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜尋（原文或譯文）")
        self._search.textChanged.connect(self._render)
        refresh = QPushButton("重新整理")
        refresh.clicked.connect(self.refresh)
        top.addWidget(self._search, 1)
        top.addWidget(refresh)
        layout.addLayout(top)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self._list, 1)

        actions = QHBoxLayout()
        copy = QPushButton("複製選取（沒選就複製全部）")
        copy.clicked.connect(self.copy_selected)
        clear = QPushButton("清除歷史")
        clear.clicked.connect(self._clear)
        actions.addWidget(copy)
        actions.addWidget(clear)
        actions.addStretch(1)
        layout.addLayout(actions)

        export_row = QHBoxLayout()
        self._format = QComboBox()
        for fmt in ("srt", "vtt", "txt"):
            self._format.addItem(fmt.upper(), userData=fmt)
        self._bilingual = QCheckBox("雙語（譯文下附原文）")
        self._rebase = QCheckBox("第一句從 0 開始")
        self._offset = QSpinBox()
        self._offset.setRange(-_OFFSET_LIMIT_MS, _OFFSET_LIMIT_MS)
        self._offset.setSingleStep(OFFSET_STEP_MS)
        self._offset.setSuffix(" ms")
        self._offset.setValue(settings.offset_ms)
        self._offset.valueChanged.connect(settings.set_offset_ms)
        settings.changed.connect(self._on_settings_changed)
        export_button = QPushButton("匯出...")
        export_button.clicked.connect(self.export_to_file)
        for w in (self._format, self._bilingual, self._rebase, QLabel("偏移"), self._offset, export_button):
            export_row.addWidget(w)
        layout.addLayout(export_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self.refresh()

    def _on_settings_changed(self, value: int) -> None:
        if self._offset.value() != value:
            self._offset.setValue(value)

    def refresh(self) -> None:
        try:
            self._rows = rows_from_history(self._client.history())
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._rows = []
            self._status.setText(f"連不上 gateway，先啟動 gateway：{e}")
        else:
            self._status.setText(f"共 {len(self._rows)} 句")
        self._render()

    def _visible_rows(self) -> list[HistoryRow]:
        needle = self._search.text()
        return [r for r in self._rows if r.matches(needle)]

    def _render(self) -> None:
        self._list.clear()
        for row in self._visible_rows():
            self._list.addItem(row.label)
        if self._list.count():
            self._list.scrollToBottom()

    def selected_text(self) -> str:
        selected = self._list.selectedItems()
        lines = [i.text() for i in selected] if selected else [self._list.item(n).text() for n in range(self._list.count())]
        return "\n".join(lines)

    def copy_selected(self) -> None:
        text = self.selected_text()
        QGuiApplication.clipboard().setText(text)
        self._status.setText(f"已複製 {len(text.splitlines())} 行")

    def _clear(self) -> None:
        try:
            self._client.clear()
        except (urllib.error.URLError, OSError) as e:
            self._status.setText(f"清除失敗：{e}")
            return
        self.refresh()

    def _ask_save_path(self, parent: QWidget, fmt: str) -> str | None:
        default_dir = user_data_dir() / "exports"
        default_dir.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            parent, "匯出字幕", str(default_dir / f"subtitles.{fmt}"), f"{fmt.upper()} (*.{fmt})"
        )
        return path or None

    def export_to_file(self) -> Path | None:
        fmt = self._format.currentData()
        path = self._save_path_fn(self, fmt)
        if not path:
            return None
        try:
            text = self._client.export(
                fmt,
                offset_ms=self._settings.offset_ms,
                bilingual=self._bilingual.isChecked(),
                rebase=self._rebase.isChecked(),
            )
        except (urllib.error.URLError, OSError) as e:
            self._status.setText(f"匯出失敗：{e}")
            return None
        if not text.strip() or text.strip() == "WEBVTT":
            self._status.setText("目前沒有可匯出的定稿字幕。")
            return None
        # SRT 給播放器讀：UTF-8 with BOM 讓舊版播放器/記事本也能正確辨識中日文
        out = Path(path)
        out.write_text(text, encoding="utf-8-sig" if fmt == "srt" else "utf-8")
        self._status.setText(f"已匯出 {out}（偏移 {format_offset(self._settings.offset_ms)}）")
        return out
