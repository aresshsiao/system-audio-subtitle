"""字幕文字渲染。見 ARCHITECTURE.md §12：只做兩種狀態（暫定灰、定稿白），
不做淡入淡出動畫——文字持續改寫時，動畫會讓可讀性大幅下降。

可讀性靠描邊（outline）而非陰影，外加可調的半透明底條，這樣在任何背景
（深色電影畫面、亮色網頁）上都看得清楚，不需要偵測背景色。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from contracts.enums import SubtitleState

# 顏色與字型都是可調參數，不寫死——之後 M6 的設定面板要能讓使用者調
# 字體大小/顏色時，改的就是這幾個值，不需要動渲染邏輯本身。
DRAFT_COLOR = QColor(220, 220, 220, 230)  # 暫定稿：灰白、稍透明
FINAL_COLOR = QColor(255, 255, 255, 255)  # 定稿：純白
OUTLINE_COLOR = QColor(0, 0, 0, 220)
BACKGROUND_COLOR = QColor(0, 0, 0, 110)  # 半透明底條


@dataclass(frozen=True)
class RenderState:
    """目前該畫在畫面上的東西。跟 contracts.messages.Transcript 對應，
    但只留渲染需要的欄位——渲染層不需要知道 utt_id/span 這些。
    """

    text: str
    state: SubtitleState

    @property
    def is_final(self) -> bool:
        return self.state != SubtitleState.DRAFT


class SubtitleRenderer(QWidget):
    def __init__(self, *, font_point_size: int = 28) -> None:
        super().__init__()
        self._state: RenderState | None = None
        self._font = QFont("Noto Sans TC", font_point_size, QFont.Weight.Bold)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_state(self, state: RenderState | None) -> None:
        """就地更新要顯示的文字——不是追加，見 §5 的「就地取代」原則。
        `state=None` 代表目前沒有字幕（清空畫面）。
        """
        if state == self._state:
            return
        self._state = state
        self.update()  # 觸發重繪

    def set_font_point_size(self, size: int) -> None:
        self._font.setPointSize(size)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        if self._state is None or not self._state.text:
            return

        painter.setFont(self._font)
        metrics = QFontMetrics(self._font)

        # 文字可能超過視窗寬度，用 Qt 的自動換行排版；寬度抓視窗寬度的
        # 90%，留左右邊界。
        max_width = int(self.width() * 0.9)
        wrapped_rect = metrics.boundingRect(
            0, 0, max_width, 10_000, Qt.TextFlag.TextWordWrap, self._state.text
        )

        padding_x, padding_y = 16, 10
        box_width = wrapped_rect.width() + padding_x * 2
        box_height = wrapped_rect.height() + padding_y * 2
        box_x = (self.width() - box_width) / 2
        # 貼近底部，留一點邊界；但行數多、框比 widget 本身還高時，不能讓
        # box_y 算成負值把最上面幾行擠出 widget 上緣被裁掉——最少留 8px
        # 頂部邊界，寧可整塊往上貼齊，也不要裁字（實測 render_test.py 抓到
        # 這個問題：三行字幕在小 widget 裡最上面一行整個被切掉）。
        box_y = max(8.0, self.height() - box_height - 24)

        bg_rect = QRectF(box_x, box_y, box_width, box_height)
        painter.setBrush(BACKGROUND_COLOR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(bg_rect, 8, 8)

        text_color = FINAL_COLOR if self._state.is_final else DRAFT_COLOR
        text_rect = QRectF(
            box_x + padding_x, box_y + padding_y, wrapped_rect.width(), wrapped_rect.height()
        )
        self._draw_wrapped_outlined_text(painter, text_rect, self._state.text, text_color)

    def _draw_wrapped_outlined_text(
        self, painter: QPainter, rect: QRectF, text: str, color: QColor
    ) -> None:
        metrics = QFontMetrics(self._font)
        lines = self._wrap_text(text, int(rect.width()), metrics)

        pen = QPen(OUTLINE_COLOR)
        pen.setWidthF(3.0)

        y = rect.top() + metrics.ascent()
        for line in lines:
            path = QPainterPath()
            path.addText(QPointF(rect.left(), y), self._font, line)
            painter.strokePath(path, pen)
            painter.fillPath(path, color)
            y += metrics.height()

    @staticmethod
    def _wrap_text(text: str, max_width: int, metrics: QFontMetrics) -> list[str]:
        """簡化的自動換行：按空格分詞塞行，塞不下就換行。

        見 ARCHITECTURE.md §13 的語言特例：日文/泰文沒有空格分詞，這種
        簡化演算法對它們不適用（會整句塞成一行或在奇怪的地方硬斷）。
        M2 先用這個打通渲染流程；語言包的 `policy.line_break` 欄位
        （§13.2）在 M3 接上語言包路由後，會依語言換成對應的斷行策略。
        """
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if metrics.horizontalAdvance(candidate) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines
