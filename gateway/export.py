"""字幕匯出：SRT / VTT / 純文字。見 ARCHITECTURE.md §5、§14。

時間碼一律由 `AudioSpan` 的**樣本數 / 取樣率**算出——這就是 §5 把「時間的
真相」定為樣本數而不是牆上時鐘的回報：匯出不需要對齊任何時鐘，使用者要整體
挪動字幕（影片開始播放晚於擷取啟動）只是在這裡加一個 `offset_ms`。

**時間軸的原點**是 audio-service 開始擷取的那一刻，不是影片開頭。若擷取啟動早於
影片開始播放，用 `rebase=True`（讓第一句字幕從 0 開始），或用 `offset_ms` 手動
微調；兩者可以併用（先 rebase，再 offset）。

匯出的內容：每個 utterance 只取「目前已知的最新狀態」，而且預設**只匯出
FINAL / POLISHED**。暫定稿（DRAFT）不是定案，且定稿被幻覺過濾丟掉的句子會
永遠停在 DRAFT——那些多半是不該出現在字幕檔裡的東西。
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Literal

from contracts.enums import SubtitleState
from contracts.messages import Subtitle

ExportFormat = Literal["srt", "vtt", "txt"]

_MIN_CUE_DURATION_S = 0.5
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿]")
_CJK_BREAK_AFTER = "，。、；：！？,.;:!?"


@dataclass(frozen=True)
class ExportOptions:
    fmt: ExportFormat = "srt"
    offset_ms: int = 0
    bilingual: bool = False  # 譯文下面加一行原文
    rebase: bool = False  # 讓第一句字幕從 00:00:00 開始
    include_drafts: bool = False
    max_line_chars: int | None = 24  # None = 不折行；CJK 以字數、拉丁文字以字元數計


@dataclass(frozen=True)
class Cue:
    start_s: float
    end_s: float
    lines: list[str]


def format_timestamp(seconds: float, *, fmt: ExportFormat) -> str:
    """SRT 用逗號、VTT 用句點分隔毫秒。用整數毫秒運算避免浮點進位誤差
    （例如 1.9996 秒不能顯示成 00:00:01,1000）。"""
    total_ms = max(0, round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    sep = "," if fmt == "srt" else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def wrap_text(text: str, width: int | None) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    if width is None or len(text) <= width:
        return [text]
    if _CJK_RE.search(text):
        # 無空格文字：一行滿了就往回找最後一個標點（限後半段），在標點後斷；
        # 找不到才硬斷在 width
        lines, rest = [], text
        while len(rest) > width:
            window = rest[:width]
            cut = max((i for i, ch in enumerate(window) if ch in _CJK_BREAK_AFTER and i >= width // 2 - 1), default=-1)
            cut = cut + 1 if cut != -1 else width
            lines.append(rest[:cut])
            rest = rest[cut:]
        if rest:
            lines.append(rest)
        return lines
    return textwrap.wrap(text, width=width) or [text]


def build_cues(history: list[Subtitle], opts: ExportOptions) -> list[Cue]:
    wanted = [
        s
        for s in history
        if s.target_text.strip()
        and (opts.include_drafts or s.state in (SubtitleState.FINAL, SubtitleState.POLISHED))
    ]
    wanted.sort(key=lambda s: s.span.start_sample / s.span.sample_rate)
    if not wanted:
        return []

    origin_s = wanted[0].span.start_seconds() if opts.rebase else 0.0
    offset_s = opts.offset_ms / 1000.0

    cues: list[Cue] = []
    for s in wanted:
        start = max(0.0, s.span.start_seconds() - origin_s + offset_s)
        end = max(0.0, s.span.end_seconds() - origin_s + offset_s)
        end = max(end, start + _MIN_CUE_DURATION_S)
        lines = wrap_text(s.target_text, opts.max_line_chars)
        if opts.bilingual and s.source_text.strip() and s.source_text != s.target_text:
            lines += wrap_text(s.source_text, opts.max_line_chars)
        cues.append(Cue(start_s=start, end_s=end, lines=lines))

    # 最短時長可能把結尾撐過下一句的開頭：截到下一句開始前，字幕檔不能互相重疊
    for i in range(len(cues) - 1):
        if cues[i].end_s > cues[i + 1].start_s:
            cues[i] = Cue(cues[i].start_s, max(cues[i + 1].start_s, cues[i].start_s), cues[i].lines)
    return cues


def export_subtitles(history: list[Subtitle], opts: ExportOptions | None = None) -> str:
    opts = opts or ExportOptions()
    cues = build_cues(history, opts)

    if opts.fmt == "txt":
        return "".join(
            f"[{format_timestamp(c.start_s, fmt='vtt')[:8]}] {' '.join(c.lines)}\n" for c in cues
        )

    blocks = []
    for i, cue in enumerate(cues, start=1):
        ts = f"{format_timestamp(cue.start_s, fmt=opts.fmt)} --> {format_timestamp(cue.end_s, fmt=opts.fmt)}"
        body = "\n".join(cue.lines)
        blocks.append(f"{i}\n{ts}\n{body}\n" if opts.fmt == "srt" else f"{ts}\n{body}\n")

    if opts.fmt == "vtt":
        return "WEBVTT\n\n" + "\n".join(blocks)
    return "\n".join(blocks)
