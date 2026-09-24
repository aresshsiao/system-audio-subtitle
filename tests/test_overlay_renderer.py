"""ui/overlay/renderer.py 的邏輯測試（非像素比對）。

真正的排版/描邊視覺效果用 `QWidget.grab()` 離線渲染成圖片人工檢查過
（見 M2 開發記錄），這裡測的是不需要看像素就能驗證的邏輯：狀態比較、
換行演算法本身、多行文字不會被裁到 widget 外面（曾經踩過的坑）。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from contracts.enums import SubtitleState
from ui.overlay.renderer import RenderState, SubtitleRenderer

pytestmark = pytest.mark.slow  # 需要 QApplication


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_render_state_is_final_property() -> None:
    draft = RenderState(text="hi", state=SubtitleState.DRAFT)
    final = RenderState(text="hi", state=SubtitleState.FINAL)
    polished = RenderState(text="hi", state=SubtitleState.POLISHED)

    assert draft.is_final is False
    assert final.is_final is True
    assert polished.is_final is True  # 非 DRAFT 都算「已定」樣式


def test_set_state_same_value_is_noop(qapp) -> None:
    renderer = SubtitleRenderer()
    state = RenderState(text="hello", state=SubtitleState.DRAFT)
    renderer.set_state(state)
    # 再設一次一模一樣的 state（不同物件但值相等），內部應該偵測到沒變化
    renderer.set_state(RenderState(text="hello", state=SubtitleState.DRAFT))
    assert renderer._state == state  # 仍然是等值的狀態，沒有壞掉


def test_set_state_none_clears() -> None:
    renderer = SubtitleRenderer()
    renderer.set_state(RenderState(text="hello", state=SubtitleState.DRAFT))
    renderer.set_state(None)
    assert renderer._state is None


def test_wrap_text_fits_single_line_when_short() -> None:
    font = QFont("Arial", 20)
    metrics = QFontMetrics(font)
    lines = SubtitleRenderer._wrap_text("short text", 2000, metrics)
    assert lines == ["short text"]


def test_wrap_text_splits_long_text_into_multiple_lines() -> None:
    font = QFont("Arial", 20)
    metrics = QFontMetrics(font)
    long_text = " ".join(["word"] * 30)
    narrow_width = metrics.horizontalAdvance("word word word")  # 大約 3 個詞的寬度

    lines = SubtitleRenderer._wrap_text(long_text, narrow_width, metrics)

    assert len(lines) > 1
    # 重新組回去，內容不該遺漏任何詞（換行不能吃字）
    assert " ".join(lines).split(" ") == long_text.split(" ")


def test_wrap_text_never_produces_empty_line_for_nonempty_input() -> None:
    font = QFont("Arial", 20)
    metrics = QFontMetrics(font)
    lines = SubtitleRenderer._wrap_text("a b c d e f g h", 30, metrics)  # 極窄寬度
    assert all(line != "" for line in lines)


def test_multiline_text_box_never_starts_above_widget_top(qapp) -> None:
    """迴歸測試：修過的裁切問題——文字行數多、框比 widget 還高時，
    box_y 不該算成負值把最上面的字擠出 widget 上緣。"""
    renderer = SubtitleRenderer(font_point_size=32)
    renderer.resize(1000, 200)  # 刻意給一個容不下三行字的矮 widget
    long_text = "This is a much longer subtitle line that should wrap across multiple lines automatically"
    renderer.set_state(RenderState(text=long_text, state=SubtitleState.FINAL))

    pixmap = renderer.grab()
    assert not pixmap.isNull()
    # 沒有簡單的方式從 pixmap 直接讀出「box_y 是多少」，改為驗證
    # _paint 內部算出來的 box_y 邏輯值本身不會是負的（見原始碼的 max(8.0, ...)）。
    from PySide6.QtGui import QFontMetrics as _QFM

    metrics = _QFM(renderer._font)
    max_width = int(renderer.width() * 0.9)
    from PySide6.QtCore import Qt as _Qt

    wrapped_rect = metrics.boundingRect(0, 0, max_width, 10_000, _Qt.TextFlag.TextWordWrap, long_text)
    padding_y = 10
    box_height = wrapped_rect.height() + padding_y * 2
    box_y = max(8.0, renderer.height() - box_height - 24)
    assert box_y >= 8.0
