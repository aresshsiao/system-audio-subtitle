"""即時效能面板。見 ARCHITECTURE.md §15：可觀測性就是使用者功能。

訂閱 inference-service 每秒發布的 `MetricsSnapshot`，顯示：降級階梯目前級別與
升降次數、各階段延遲（p50/p95/p99）、RTF、幻覺過濾與丟棄計數、雲端精修
成功/失敗。使用者抱怨「今天字幕怪怪的」時，看這裡就知道是不是被降級了。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from contracts.enums import DegradeLevel
from contracts.messages import MetricsSnapshot
from contracts.topics import BUS_ENDPOINTS, INFERENCE_METRICS
from runtime.bus import Subscriber

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 500
_STALE_AFTER_S = 3.0

_LEVEL_TEXT = {
    DegradeLevel.L0_NORMAL: "L0 正常",
    DegradeLevel.L1_FINAL_BEAM_DOWN: "L1 定稿 beam 5→1",
    DegradeLevel.L2_DRAFT_INTERVAL_UP: "L2 暫定稿更新變慢（250→500ms）",
    DegradeLevel.L3_CLOUD_POLISH_OFF: "L3 已暫停雲端精修",
    DegradeLevel.L4_MODEL_DOWNGRADE: "L4 已換成備援小模型",
    DegradeLevel.L5_DROP_OLDEST: "L5 正在丟棄過舊的句子（字幕會缺漏）",
}

# 直方圖名稱 → (顯示名稱, 單位)
_HISTOGRAM_LABELS = {
    "asr_draft_ms": ("暫定稿 ASR", "ms"),
    "asr_final_ms": ("定稿 ASR", "ms"),
    "mt_draft_ms": ("暫定稿翻譯", "ms"),
    "mt_final_ms": ("定稿翻譯（含前文）", "ms"),
    "draft_interval_ms": ("暫定稿實際間隔", "ms"),
    "final_queue_lag_ms": ("句末→開始處理", "ms"),
    "polish_ms": ("雲端精修", "ms"),
    "asr_rtf": ("ASR RTF（>1 = 跟不上）", ""),
}

_COUNTER_LABELS = {
    "hallucination_filtered_total": "幻覺過濾",
    "dropped_utterances_total": "L5 丟棄句數",
    "polish_ok_total": "精修成功",
    "polish_fail_total": "精修失敗",
    "polish_skipped_circuit_total": "熔斷期間略過",
    "polish_unchanged_total": "精修無變更",
}


def format_level(level: int) -> str:
    try:
        return _LEVEL_TEXT[DegradeLevel(level)]
    except ValueError:
        return f"L{level}"


class MetricsPanel(QDialog):
    def __init__(self, parent: QWidget | None = None, *, subscriber: Subscriber | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("效能監控")
        self.resize(560, 460)

        self._subscriber = subscriber
        self._own_subscriber = subscriber is None
        if self._subscriber is None:
            self._subscriber = Subscriber(
                BUS_ENDPOINTS[INFERENCE_METRICS], topics=[INFERENCE_METRICS]
            )

        layout = QVBoxLayout(self)
        self._level_label = QLabel("等待 inference-service 的資料...")
        self._level_label.setWordWrap(True)
        layout.addWidget(self._level_label)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["階段", "樣本數", "p50", "p95", "p99"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        self._counters_label = QLabel("")
        self._counters_label.setWordWrap(True)
        layout.addWidget(self._counters_label)

        self._last_snapshot_at: float | None = None
        self._last_level_text = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(_POLL_INTERVAL_MS)

    def _poll(self) -> None:
        latest = None
        while True:  # 一次撈光，只畫最新的
            try:
                snap = self._subscriber.recv_typed(MetricsSnapshot, timeout_ms=0)
            except Exception as e:  # noqa: BLE001
                logger.warning("讀取 metrics 失敗: %s", e)
                return
            if snap is None:
                break
            latest = snap
        if latest is not None:
            self.update_snapshot(latest)
        self._refresh_staleness()

    def _refresh_staleness(self) -> None:
        """inference-service 主迴圈每秒發布一次快照。太久沒收到 = 它卡在耗時的
        解碼裡（例如 GPU 被遊戲之類的程式佔滿，實測會卡到一分鐘以上）或已經停了。
        這時畫面上的舊數字看起來一切正常，反而會誤導，所以要明講。"""
        if self._last_snapshot_at is None:
            return
        age = time.monotonic() - self._last_snapshot_at
        if age > _STALE_AFTER_S:
            self._level_label.setText(
                f"⚠ inference-service 已 {age:.0f} 秒沒有回報（可能卡在耗時的解碼——"
                "例如 GPU 被其他程式佔滿——或已停止）。以下是最後一次的數字。\n"
                + self._last_level_text
            )

    def update_snapshot(self, snap: MetricsSnapshot) -> None:
        transitions = (
            f"（累計升級 {int(snap.gauges.get('degrade_transitions_up', 0))} 次、"
            f"恢復 {int(snap.gauges.get('degrade_transitions_down', 0))} 次）"
        )
        self._last_snapshot_at = time.monotonic()
        self._last_level_text = f"降級階梯：{format_level(snap.degrade_level)} {transitions}"
        self._level_label.setText(self._last_level_text)

        rows = [(k, v) for k, v in snap.histograms.items() if k in _HISTOGRAM_LABELS and v["count"] > 0]
        self._table.setRowCount(len(rows))
        for row, (name, summary) in enumerate(rows):
            label, unit = _HISTOGRAM_LABELS[name]
            decimals = 2 if unit == "" else 0
            cells = [label, f"{int(summary['count'])}"] + [
                f"{summary[key]:.{decimals}f}{unit}" for key in ("p50", "p95", "p99")
            ]
            for col, text in enumerate(cells):
                self._table.setItem(row, col, QTableWidgetItem(text))

        parts = [
            f"{label} {snap.counters[key]}"
            for key, label in _COUNTER_LABELS.items()
            if snap.counters.get(key)
        ]
        overrun = snap.gauges.get("ring_overrun_samples", 0)
        if overrun:
            parts.append(f"環形緩衝覆蓋 {overrun / 16000:.1f}s 音訊")
        if snap.gauges.get("polish_circuit_open"):
            parts.append("雲端精修：熔斷中")
        self._counters_label.setText("　".join(parts) if parts else "（尚無事件計數）")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        if self._own_subscriber and self._subscriber is not None:
            self._subscriber.close()
        super().closeEvent(event)
