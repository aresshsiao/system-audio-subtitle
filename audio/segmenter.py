"""VAD 語音段落切分狀態機。見 ARCHITECTURE.md §7：

    音訊 10ms 幀
       ↓
    Silero VAD（ONNX，CPU 推論 < 1ms/幀）
       ↓
    狀態機：SILENCE ──語音≥100ms──→ SPEAKING ──靜音≥350ms──→ 收句
                                        └──長度≥12s──────────→ 強制斷句

只吐兩種事件：utterance 開始（`closed=False`）與 utterance 結束
（`closed=True`）。中間 inference-service 要不要做暫定稿解碼、解碼到
哪裡，是它自己按時間輪詢 ring buffer 的事，不需要 segmenter 一直發
「utterance 還在繼續、現在延伸到哪」這種中繼事件——事件愈少，跨進程
訊息量愈小，狀態機也愈單純。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from ulid import ULID

from audio.vad import FRAME_SAMPLES, SAMPLE_RATE
from contracts.messages import AudioSpan, Utterance


class _State(Enum):
    SILENCE = auto()
    SPEAKING = auto()


@dataclass(frozen=True)
class SegmenterConfig:
    sample_rate: int = SAMPLE_RATE
    speech_prob_threshold: float = 0.5
    min_speech_ms: float = 100.0
    min_silence_ms: float = 350.0
    max_utterance_s: float = 12.0


class Segmenter:
    """逐框餵 VAD 機率，吐出 `Utterance` 開始/結束事件。

    呼叫端必須按順序、逐框餵（每框固定 `FRAME_SAMPLES` 個樣本，不能跳幀、
    不能倒退）——Segmenter 內部靠呼叫次數自己累加目前處理到第幾個樣本，
    沒有另外收樣本數當參數，就是為了強迫呼叫端遵守「連續、不跳幀」這個
    前提，跳幀的話這裡的樣本位置換算全部會錯。
    """

    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self.config = config or SegmenterConfig()
        self._frame_ms = FRAME_SAMPLES / self.config.sample_rate * 1000
        self._reset_state()

    def _reset_state(self) -> None:
        self._state = _State.SILENCE
        self._next_frame_start_sample = 0
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._utt_id: str | None = None
        self._utt_start_sample: int | None = None

    def reset(self) -> None:
        """換音源、或偵測到擷取中斷時呼叫。不保留任何正在進行中的 utterance——
        呼叫端如果需要，要自己決定要不要在 reset 前先手動收掉目前的 utterance。
        """
        self._reset_state()

    def flush(self) -> Utterance | None:
        """收掉目前正在進行的 utterance（如果有），但**不**重設樣本計數。

        換音源時用：時間軸（樣本數）必須跨音源連續——ring buffer 的寫入位置
        不會歸零，所以這裡的計數也不能歸零，否則新音源的 span 會指到
        ring buffer 裡舊音源的位置。`reset()` 適合整條管線重來的情況，不是這個。
        """
        if self._state != _State.SPEAKING:
            self._speech_run_ms = 0.0
            return None
        assert self._utt_id is not None and self._utt_start_sample is not None

        silence_frames = round(self._silence_run_ms / self._frame_ms)
        end_sample = max(
            self._next_frame_start_sample - silence_frames * FRAME_SAMPLES,
            self._utt_start_sample,
        )
        event = Utterance(
            utt_id=self._utt_id,
            span=AudioSpan(self._utt_start_sample, end_sample, self.config.sample_rate),
            closed=True,
        )
        self._state = _State.SILENCE
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._utt_id = None
        self._utt_start_sample = None
        return event

    def process_frame(self, prob: float) -> Utterance | None:
        """餵一框的語音機率（0~1），回傳這一框觸發的事件，沒事件就回傳 None。"""
        frame_start = self._next_frame_start_sample
        frame_end = frame_start + FRAME_SAMPLES
        self._next_frame_start_sample = frame_end

        is_speech = prob >= self.config.speech_prob_threshold

        if self._state == _State.SILENCE:
            return self._process_silence_frame(frame_start, frame_end, is_speech)
        return self._process_speaking_frame(frame_end, is_speech)

    def _process_silence_frame(
        self, frame_start: int, frame_end: int, is_speech: bool
    ) -> Utterance | None:
        if not is_speech:
            self._speech_run_ms = 0.0
            return None

        self._speech_run_ms += self._frame_ms
        if self._speech_run_ms < self.config.min_speech_ms:
            return None

        # 累積語音達到門檻，正式開啟 utterance；起點回推到這段連續語音
        # 真正開始的那一框，不是「確認的當下」那一框。
        n_frames_in_run = round(self._speech_run_ms / self._frame_ms)
        utt_start_sample = frame_end - n_frames_in_run * FRAME_SAMPLES

        self._utt_id = str(ULID())
        self._utt_start_sample = utt_start_sample
        self._state = _State.SPEAKING
        self._silence_run_ms = 0.0

        return Utterance(
            utt_id=self._utt_id,
            span=AudioSpan(utt_start_sample, frame_end, self.config.sample_rate),
            closed=False,
        )

    def _process_speaking_frame(self, frame_end: int, is_speech: bool) -> Utterance | None:
        assert self._utt_id is not None and self._utt_start_sample is not None

        if is_speech:
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += self._frame_ms

        utt_duration_s = (frame_end - self._utt_start_sample) / self.config.sample_rate
        hit_silence = self._silence_run_ms >= self.config.min_silence_ms
        hit_max_length = utt_duration_s >= self.config.max_utterance_s

        if not (hit_silence or hit_max_length):
            return None

        end_sample = frame_end
        if hit_silence:
            # 收句點要扣掉尾端那段靜音，不要把靜音算進 utterance 本身——
            # 否則每句字幕結尾都會拖一截沒意義的靜音進最終解碼範圍。
            silence_frames = round(self._silence_run_ms / self._frame_ms)
            end_sample = max(frame_end - silence_frames * FRAME_SAMPLES, self._utt_start_sample)
        # hit_max_length 但還沒判定靜音的情況：直接在目前位置強制斷句，
        # 不扣減——講者可能根本沒停頓，扣了反而會誤刪還在講的內容。
        #
        # 強制斷句後如果講者確實沒停頓，下一句要重新累積 min_speech_ms 才會
        # 「確認」開啟，但這不會漏音訊：開啟事件的起點會回推到語音真正開始
        # 的那一框（見 `_process_silence_frame` 的起點回推邏輯），剛好就是
        # 強制斷句的終點，所以兩個 utterance 的 span 會無縫銜接，只有
        # 「確認新 utterance 已經開始」這件事會延遲 min_speech_ms，span
        # 範圍本身沒有缺口（見 test_force_cut_followed_by_continued_speech_
        # starts_new_utterance 驗證這一點）。

        event = Utterance(
            utt_id=self._utt_id,
            span=AudioSpan(self._utt_start_sample, end_sample, self.config.sample_rate),
            closed=True,
        )
        self._state = _State.SILENCE
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._utt_id = None
        self._utt_start_sample = None
        return event
