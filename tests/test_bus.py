"""runtime/bus.py 的整合測試：真的開 tcp://127.0.0.1 socket 收發。

用動態配置的空閒 port，避免測試之間互撞，也避免跟開發機上其他服務衝突。
"""

from __future__ import annotations

import socket
import time

import pytest

from contracts.enums import CaptureTargetKind, SubtitleState
from contracts.messages import AudioSpan, CaptureTarget, Utterance
from runtime.bus import Publisher, Replier, Requester, Subscriber


def free_tcp_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"tcp://127.0.0.1:{port}"


def wait_for_subscription(pub: Publisher, sub: Subscriber, topic: str, probe: Utterance) -> None:
    """PUB/SUB 的 slow joiner 問題：SUB connect 完成訂閱前送出的訊息會被丟棄。

    用重送探測訊息直到收到為止，讓測試不依賴固定的 sleep 時間（脆弱）。
    """
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pub.publish(topic, probe)
        if sub.recv(timeout_ms=50) is not None:
            return
    raise TimeoutError("subscriber 一直沒收到訂閱探測訊息")


def test_pub_sub_roundtrip() -> None:
    endpoint = free_tcp_endpoint()
    pub = Publisher(endpoint)
    sub = Subscriber(endpoint, topics=["audio."])
    try:
        probe = Utterance(
            utt_id="probe", span=AudioSpan(0, 100), closed=False
        )
        wait_for_subscription(pub, sub, "audio.utterance", probe)

        real = Utterance(utt_id="01J010", span=AudioSpan(0, 16000), closed=True)
        pub.publish("audio.utterance", real)
        decoded = sub.recv_typed(Utterance, timeout_ms=1000)

        assert decoded == real
    finally:
        pub.close()
        sub.close()


def test_sub_only_receives_subscribed_topic_prefix() -> None:
    endpoint = free_tcp_endpoint()
    pub = Publisher(endpoint)
    sub = Subscriber(endpoint, topics=["inference.subtitle"])
    try:
        probe = Utterance(utt_id="probe", span=AudioSpan(0, 100), closed=False)
        # 用 sub 自己訂閱的 topic 探測連線就緒，並把探測訊息本身收乾淨，
        # 避免留在佇列裡汙染後面「不該收到」的斷言（之前用另一個 subscriber
        # 探測就是因為這樣才誤判失敗：探測訊息跑進了 sub 自己的佇列）。
        deadline = time.monotonic() + 2.0
        ready = False
        while time.monotonic() < deadline:
            pub.publish("inference.subtitle", probe)
            if sub.recv(timeout_ms=50) is not None:
                ready = True
                break
        assert ready, "publisher 一直沒連上，測試環境有問題"

        # 發一則不同 topic 的訊息，sub 不應該收到
        pub.publish("inference.transcript", probe)
        result = sub.recv(timeout_ms=200)
        assert result is None
    finally:
        pub.close()
        sub.close()


def test_req_rep_roundtrip() -> None:
    endpoint = free_tcp_endpoint()
    rep = Replier(endpoint)
    req = Requester(endpoint, timeout_ms=1000)
    try:
        target = CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=999, include_process_tree=True)
        req_send_result = {}

        # Requester.request() 是同步阻塞呼叫，這裡用簡單的手動交錯而非真的
        # 起執行緒：先讓 Replier 準備好收，用 poll_request 立刻收；REQ/REP
        # 在同一個測試 process、同步呼叫下必須交錯進行，所以用一個小 thread
        # 跑 Requester 端。
        import threading

        def do_request() -> None:
            result = req.request(target, CaptureTarget)
            req_send_result["value"] = result

        t = threading.Thread(target=do_request)
        t.start()

        received = rep.poll_request(CaptureTarget, timeout_ms=2000)
        assert received == target
        rep.reply(received)

        t.join(timeout=2)
        assert req_send_result["value"] == target
    finally:
        rep.close()
        req.close()


def test_req_timeout_returns_none_and_recovers() -> None:
    """REQ 逾時後 socket 要能自我修復，下一次 request 仍然可用（見 bus.py docstring）。"""
    endpoint = free_tcp_endpoint()
    # 200ms 太緊繃：補上 Replier 後的第一次真正握手 + 往返，在測試機上
    # 偶爾會超過 200ms 而誤判成又一次逾時。800ms 對「沒人聽」的第一階段
    # 只是讓測試多等一點，但能讓第二階段的握手+往返穩定完成。
    req = Requester(endpoint, timeout_ms=800)  # 沒有 Replier 在聽，一定逾時
    try:
        target = CaptureTarget(kind=CaptureTargetKind.PROCESS, pid=1)
        result = req.request(target, CaptureTarget)
        assert result is None

        # 逾時後補一個真正的 Replier，同一個 Requester 實例要還能用
        rep = Replier(endpoint)
        try:
            import threading

            outcome = {}

            def do_request() -> None:
                outcome["value"] = req.request(target, CaptureTarget)

            t = threading.Thread(target=do_request)
            t.start()
            received = rep.poll_request(CaptureTarget, timeout_ms=2000)
            rep.reply(received)
            t.join(timeout=2)

            assert outcome["value"] == target
        finally:
            rep.close()
    finally:
        req.close()
