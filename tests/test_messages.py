"""contracts/messages.py 的單元測試。

重點測「encode → decode 往返後型別沒有退化成裸 dict/str」，因為那類 bug
不會在型別檢查時被抓到，只會在執行到某個 .attribute 存取時才爆炸，
而且往往是在跟序列化完全無關的程式碼裡爆炸。
"""

from __future__ import annotations

import pytest

from contracts.enums import CaptureTargetKind, EngineKind, SubtitleState
from contracts.messages import AudioSpan, CaptureTarget, Subtitle, Transcript, Utterance


def make_span(start: int = 0, end: int = 16000) -> AudioSpan:
    return AudioSpan(start_sample=start, end_sample=end, sample_rate=16000)


def test_audio_span_duration() -> None:
    span = make_span(0, 32000)
    assert span.duration_seconds == 2.0


def test_audio_span_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        AudioSpan(start_sample=100, end_sample=50)


def test_audio_span_rejects_bad_sample_rate() -> None:
    with pytest.raises(ValueError):
        AudioSpan(start_sample=0, end_sample=100, sample_rate=0)


def test_utterance_roundtrip_preserves_nested_dataclass() -> None:
    original = Utterance(utt_id="01J000", span=make_span(0, 8000), closed=False)
    decoded = Utterance.decode(original.encode())

    assert decoded == original
    # 這是關鍵斷言：span 必須真的是 AudioSpan，不是還原成 dict
    assert isinstance(decoded.span, AudioSpan)
    assert decoded.span.duration_seconds == 0.5


def test_transcript_roundtrip_preserves_enum() -> None:
    original = Transcript(
        utt_id="01J001",
        revision=2,
        state=SubtitleState.DRAFT,
        span=make_span(),
        text="I think we shall",
        src_lang="en",
        lang_locked=False,
        stable_chars=10,
        no_speech_prob=0.02,
    )
    decoded = Transcript.decode(original.encode())

    assert decoded == original
    assert isinstance(decoded.state, SubtitleState)
    assert decoded.state is SubtitleState.DRAFT


def test_transcript_rejects_polished_state() -> None:
    with pytest.raises(ValueError):
        Transcript(
            utt_id="x",
            revision=0,
            state=SubtitleState.POLISHED,
            span=make_span(),
            text="x",
            src_lang="en",
            lang_locked=False,
            stable_chars=0,
            no_speech_prob=0.0,
        )


def test_transcript_rejects_stable_chars_overflow() -> None:
    with pytest.raises(ValueError):
        Transcript(
            utt_id="x",
            revision=0,
            state=SubtitleState.DRAFT,
            span=make_span(),
            text="ab",
            src_lang="en",
            lang_locked=False,
            stable_chars=99,
            no_speech_prob=0.0,
        )


def test_subtitle_roundtrip_preserves_everything() -> None:
    original = Subtitle(
        utt_id="01J002",
        revision=1,
        state=SubtitleState.FINAL,
        span=make_span(16000, 48000),
        src_lang="ja",
        tgt_lang="zh-Hant",
        pack_id="ja-zhHant",
        source_text="行きます",
        target_text="我要去了",
        engine=EngineKind.TRANSLATE_CT2_NLLB,
    )
    decoded = Subtitle.decode(original.encode())

    assert decoded == original
    assert isinstance(decoded.engine, EngineKind)
    assert isinstance(decoded.span, AudioSpan)


def test_capture_target_endpoint_requires_device_id() -> None:
    with pytest.raises(ValueError):
        CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id=None)


def test_capture_target_process_requires_pid() -> None:
    with pytest.raises(ValueError):
        CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=None)


def test_capture_target_process_roundtrip() -> None:
    original = CaptureTarget(
        kind=CaptureTargetKind.PROCESS, pid=1234, include_process_tree=True
    )
    decoded = CaptureTarget.decode(original.encode())

    assert decoded == original
    assert isinstance(decoded.kind, CaptureTargetKind)


def test_encode_produces_utf8_readable_json() -> None:
    original = Utterance(utt_id="01J003", span=make_span(), closed=True)
    raw = original.encode()

    # 中文/日文文字不應該被跳脫成 \uXXXX —— 方便直接看 log 除錯
    assert b"\\u" not in Transcript(
        utt_id="x",
        revision=0,
        state=SubtitleState.FINAL,
        span=make_span(),
        text="行きます",
        src_lang="ja",
        lang_locked=True,
        stable_chars=4,
        no_speech_prob=0.0,
    ).encode()
    assert isinstance(raw, bytes)


def test_optional_enum_field_roundtrips_as_enum_not_str() -> None:
    """迴歸測試：`Enum | None`（PEP 604 寫法）解碼後要還原成 Enum，
    不能漏成裸字串——`_unwrap_optional` 之前只認 typing.Union。"""
    from contracts.messages import SetCaptureTargetAck

    ack = SetCaptureTargetAck(
        success=True,
        active_kind=CaptureTargetKind.PROCESS,
        description="行程 chrome.exe (PID 1234)",
        warning=None,
        error=None,
    )
    decoded = SetCaptureTargetAck.decode(ack.encode())

    assert decoded == ack
    assert isinstance(decoded.active_kind, CaptureTargetKind)


def test_optional_enum_field_none_stays_none() -> None:
    from contracts.messages import SetCaptureTargetAck

    ack = SetCaptureTargetAck(success=False, error="boom")
    decoded = SetCaptureTargetAck.decode(ack.encode())

    assert decoded.active_kind is None
    assert decoded.error == "boom"
