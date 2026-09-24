"""字幕時間軸單一真相。見 ARCHITECTURE.md §3、§5、§12。

M2 現況：翻譯（M3）還沒接上，inference-service 目前只發布 `Transcript`
（原文，沒有譯文）。Session 先處理 Transcript，UI 暫時顯示原文——等 M3
接上 `Subtitle`（含譯文）後，gateway 改訂閱 `INFERENCE_SUBTITLE`，
Session 的邏輯（依 utt_id/revision 決定要不要更新、「目前該顯示哪一句」）
不需要跟著改，因為兩種訊息都有相同的 utt_id/revision 語意。
"""

from __future__ import annotations

from contracts.messages import Transcript


class Session:
    """單一 process 內的記憶體狀態，不是資料庫——gateway 進程重啟就重來，
    這是刻意的：字幕是即時的東西，沒有「歷史」需要跨進程重啟保留
    （逐句歷史列表見 §14 的歷史面板，那是 M6 的事，且來源會是這裡
    累積的 `history()`，不是額外的持久化層）。
    """

    def __init__(self) -> None:
        self._latest_by_utt: dict[str, Transcript] = {}
        self._order: list[str] = []  # utt_id 第一次出現的順序

    def apply(self, transcript: Transcript) -> bool:
        """套用一則新收到的 Transcript。

        回傳 True 表示狀態真的變了（呼叫端該廣播出去）；False 表示這是
        過期的 revision（見 ARCHITECTURE.md §5：revision 用來讓亂序到達
        時可以丟棄舊修訂），什麼都不做。
        """
        existing = self._latest_by_utt.get(transcript.utt_id)
        if existing is not None and transcript.revision < existing.revision:
            return False
        if existing is None:
            self._order.append(transcript.utt_id)
        self._latest_by_utt[transcript.utt_id] = transcript
        return True

    def latest(self, utt_id: str) -> Transcript | None:
        return self._latest_by_utt.get(utt_id)

    def current_display(self) -> Transcript | None:
        """目前該顯示在浮層上的那一句：永遠是「最後出現的 utt_id」的最新
        狀態，不管它是 DRAFT 還是 FINAL。新的 utterance 一開始（第一次
        收到它的 Transcript），顯示焦點就切過去——定稿的句子會留在畫面上，
        直到下一句開始說話為止，符合一般即時字幕的閱讀習慣。
        """
        if not self._order:
            return None
        return self._latest_by_utt[self._order[-1]]

    def history(self) -> list[Transcript]:
        """依出現順序排列的逐句歷史，每句都是該 utt_id 目前已知的最新狀態。"""
        return [self._latest_by_utt[uid] for uid in self._order]

    def clear(self) -> None:
        self._latest_by_utt.clear()
        self._order.clear()
