"""audio/segmenter.py 的狀態機測試。

用合成的機率序列（不是真的 VAD 輸出）——這裡要驗證的是狀態機本身的轉移
邏輯對不對，跟 VAD 模型的辨識品質無關，用假資料反而更能精準控制邊界條件
（見 test_vad.py 才是測真的語音辨識能力）。
"""

from __future__ import annotations

from contracts.messages import Utterance
from audio.segmenter import FRAME_SAMPLES, Segmenter, SegmenterConfig

FRAME_MS = FRAME_SAMPLES / 16000 * 1000  # 32ms


def default_config(**overrides: object) -> SegmenterConfig:
    base = dict(min_speech_ms=100.0, min_silence_ms=350.0, max_utterance_s=12.0)
    base.update(overrides)
    return SegmenterConfig(**base)


def feed(segmenter: Segmenter, probs: list[float]) -> list[Utterance]:
    events = []
    for p in probs:
        ev = segmenter.process_frame(p)
        if ev is not None:
            events.append(ev)
    return events


def test_pure_silence_produces_no_events() -> None:
    seg = Segmenter(default_config())
    events = feed(seg, [0.0] * 50)
    assert events == []


def test_brief_speech_below_min_duration_produces_no_events() -> None:
    """只有 2 框語音（64ms < 100ms 門檻），不該被判定成一句話。"""
    seg = Segmenter(default_config())
    events = feed(seg, [0.9, 0.9] + [0.0] * 50)
    assert events == []


def test_speech_then_silence_produces_open_then_close() -> None:
    seg = Segmenter(default_config())
    # 5 框語音（160ms，超過 100ms 門檻）+ 12 框靜音（384ms，超過 350ms 門檻）
    events = feed(seg, [0.9] * 5 + [0.0] * 12)

    assert len(events) == 2
    opened, closed = events
    assert opened.closed is False
    assert closed.closed is True
    assert opened.utt_id == closed.utt_id


def test_open_event_start_sample_backdated_to_run_start() -> None:
    """開啟事件的起點應該是「連續語音真正開始」那一框，不是確認當下那一框。"""
    seg = Segmenter(default_config(min_speech_ms=100.0))
    # 每框 32ms，100ms 門檻要滿 4 框（128ms）才會跨過去，3 框只有 96ms 不夠。
    events = feed(seg, [0.9] * 4 + [0.0] * 12)

    opened = events[0]
    assert opened.span.start_sample == 0  # 從第一框（樣本 0）就算起點
    assert opened.span.end_sample == 4 * FRAME_SAMPLES


def test_close_event_trims_trailing_silence() -> None:
    """收句時應該扣掉尾端那段靜音，不要把靜音算進 utterance 範圍。"""
    seg = Segmenter(default_config(min_speech_ms=100.0, min_silence_ms=350.0))
    n_speech = 5
    n_silence = 11  # round(350/32) = 11 框才會觸發收句
    events = feed(seg, [0.9] * n_speech + [0.0] * n_silence)

    closed = events[-1]
    # utterance 結尾應該落在語音框結束的地方，不含後面 11 框靜音
    assert closed.span.end_sample == n_speech * FRAME_SAMPLES


def test_utt_id_differs_across_separate_utterances() -> None:
    seg = Segmenter(default_config())
    events = feed(seg, [0.9] * 5 + [0.0] * 12 + [0.9] * 5 + [0.0] * 12)

    assert len(events) == 4
    first_utt_id = events[0].utt_id
    second_utt_id = events[2].utt_id
    assert first_utt_id != second_utt_id
    assert events[1].utt_id == first_utt_id
    assert events[3].utt_id == second_utt_id


def test_brief_dip_during_speech_does_not_close_utterance() -> None:
    """語音中間偶爾一兩框機率掉下去（VAD 雜訊），不該立刻誤判成收句。"""
    seg = Segmenter(default_config(min_silence_ms=350.0))
    # 5 框語音 + 2 框「掉下去」(64ms < 350ms 門檻) + 5 框語音 + 12 框真正靜音
    events = feed(seg, [0.9] * 5 + [0.0] * 2 + [0.9] * 5 + [0.0] * 12)

    assert len(events) == 2  # 只有一次開啟、一次收句，中間的短暫掉幀沒觸發收句
    opened, closed = events
    assert opened.closed is False
    assert closed.closed is True


def test_force_cut_at_max_utterance_length() -> None:
    """講者持續不停頓，達到長度上限要強制斷句，不能無限等靜音。"""
    max_s = 1.0  # 縮小到 1 秒方便測試，不用真的跑 12 秒
    seg = Segmenter(default_config(max_utterance_s=max_s))
    # 剛好餵到觸發強制斷句那一框就停，不多餵——多餵的話語音仍在持續，
    # 下一句會立刻重新開始累積（見 test_force_cut_followed_by_continued_speech_
    # starts_new_utterance），這裡只想單獨驗證斷句本身觸發的時機與範圍。
    import math

    n_frames_to_hit_1s = math.ceil(max_s * 1000 / FRAME_MS)
    events = feed(seg, [0.9] * n_frames_to_hit_1s)

    assert len(events) == 2  # 開啟 + 強制斷句收句
    opened, closed = events
    assert opened.closed is False
    assert closed.closed is True
    duration_s = (closed.span.end_sample - closed.span.start_sample) / 16000
    assert duration_s >= max_s
    assert duration_s < max_s + FRAME_MS / 1000 * 2  # 誤差不該超過一兩框


def test_force_cut_followed_by_continued_speech_starts_new_utterance() -> None:
    """強制斷句後講者還在講：應該再開一個新的 utterance，且因為「起點回推」
    的邏輯，新 utterance 的起點會緊接在強制斷句的終點之後，沒有漏掉音訊。
    """
    max_s = 1.0
    seg = Segmenter(default_config(max_utterance_s=max_s, min_speech_ms=100.0))
    n_frames_for_1s = round(1.0 * 1000 / FRAME_MS)

    # 持續講超過 2 秒，會先強制斷句一次，講者仍持續發聲，應該再開一句新的
    events = feed(seg, [0.9] * (n_frames_for_1s * 2 + 10))

    closes = [e for e in events if e.closed]
    opens = [e for e in events if not e.closed]
    assert len(closes) >= 1
    assert len(opens) >= 2  # 第一句的開啟 + 強制斷句後新一句的開啟

    first_close = closes[0]
    # 找出強制斷句「之後」開啟的那一個 utterance
    reopened = next(e for e in opens if e.span.start_sample >= first_close.span.end_sample)
    assert reopened.utt_id != first_close.utt_id
    # 起點回推邏輯應該讓新 utterance 緊接在強制斷句終點之後，不留缺口。
    assert reopened.span.start_sample == first_close.span.end_sample


def test_reset_clears_in_progress_utterance() -> None:
    seg = Segmenter(default_config())
    feed(seg, [0.9] * 5)  # 開了一句但還沒收
    seg.reset()

    # reset 後應該回到全新狀態：同樣的輸入要能重新正確判定
    events = feed(seg, [0.9] * 5 + [0.0] * 12)
    assert len(events) == 2
    assert events[0].span.start_sample == 0  # 不是接著 reset 前的樣本數繼續算
