"""ZeroMQ 封裝：PUB/SUB 廣播 + REQ/REP 控制指令。

其他程式碼不應該直接 `import zmq`——一律透過這支檔案的 Publisher /
Subscriber / Requester / Replier，原因：

  1. 訊息一律是 contracts.messages.Message 子類別，序列化規則統一在這裡做，
     不要讓每個呼叫端各自 json.dumps。
  2. topic 用 multipart frame 傳（[topic, payload]），不是把 topic 前綴黏進
     payload 字串裡切——多語言字幕文字本身可能包含任何 byte 序列，用
     multipart 才不會跟「topic 分隔符」衝突。
  3. 之後要換成 tcp://其他機器IP 做跨機器部署時，只改 contracts/topics.py，
     這支檔案完全不用動。
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import TypeVar

import zmq

from contracts.messages import Message

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Message)

# REQ/REP 預設逾時。控制指令（例如切換擷取目標）不應該無限期卡住呼叫端——
# audio-service 沒回應時，UI 要能在有限時間內告知使用者「切換失敗」，
# 而不是整個介面凍結。
DEFAULT_REQUEST_TIMEOUT_MS = 3000


class Publisher:
    """PUB socket，bind 在指定 endpoint。同一個 Publisher 可以發多種 topic。"""

    def __init__(self, endpoint: str, *, context: zmq.Context | None = None) -> None:
        self._context = context or zmq.Context.instance()
        self._socket = self._context.socket(zmq.PUB)
        self._socket.bind(endpoint)
        self._endpoint = endpoint
        logger.info("Publisher bound at %s", endpoint)

    def publish(self, topic: str, message: Message) -> None:
        self._socket.send_multipart([topic.encode("utf-8"), message.encode()])

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "Publisher":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Subscriber:
    """SUB socket，connect 到一個 endpoint 並訂閱指定的 topic 前綴。"""

    def __init__(
        self, endpoint: str, topics: list[str], *, context: zmq.Context | None = None
    ) -> None:
        self._context = context or zmq.Context.instance()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.connect(endpoint)
        for topic in topics:
            self._socket.setsockopt(zmq.SUBSCRIBE, topic.encode("utf-8"))
        self._endpoint = endpoint
        logger.info("Subscriber connected to %s, topics=%s", endpoint, topics)

    def recv(self, *, timeout_ms: int | None = None) -> tuple[str, bytes] | None:
        """收下一則訊息，回傳 (topic, raw_payload)。timeout 到期回傳 None。

        呼叫端自己決定用哪個 Message 子類別的 .decode() 解析 payload——
        Subscriber 不知道每個 topic 對應哪個型別，那是呼叫端（pipeline 編排
        邏輯）的責任，不該寫死在傳輸層。
        """
        if timeout_ms is not None:
            if self._socket.poll(timeout=timeout_ms) == 0:
                return None
        topic_bytes, payload = self._socket.recv_multipart()
        return topic_bytes.decode("utf-8"), payload

    def recv_typed(self, message_cls: type[T], *, timeout_ms: int | None = None) -> T | None:
        result = self.recv(timeout_ms=timeout_ms)
        if result is None:
            return None
        _topic, payload = result
        return message_cls.decode(payload)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "Subscriber":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Requester:
    """REQ socket：同步控制指令（例如切換擷取目標），一來一往。"""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
        context: zmq.Context | None = None,
    ) -> None:
        self._context = context or zmq.Context.instance()
        self._endpoint = endpoint
        self._timeout_ms = timeout_ms
        self._socket = self._make_socket()

    def _make_socket(self) -> zmq.Socket:
        socket = self._context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(self._endpoint)
        return socket

    def request(self, message: Message, response_cls: type[T]) -> T | None:
        """送出請求並等回應。逾時回傳 None（呼叫端要處理逾時 = 失敗）。

        REQ socket 逾時後必須整顆重建（libzmq 的已知限制：REQ 逾時後
        狀態機會卡在「等回應」，同一個 socket 送下一個請求會直接噴錯），
        所以這裡逾時時重新建立 socket，呼叫端不需要知道這個細節。
        """
        self._socket.send(message.encode())
        if self._socket.poll(timeout=self._timeout_ms) == 0:
            logger.warning("Request to %s timed out after %dms", self._endpoint, self._timeout_ms)
            self._socket.close()
            self._socket = self._make_socket()
            return None
        raw = self._socket.recv()
        return response_cls.decode(raw)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "Requester":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Replier:
    """REP socket：處理 Requester 送來的控制指令。"""

    def __init__(self, endpoint: str, *, context: zmq.Context | None = None) -> None:
        self._context = context or zmq.Context.instance()
        self._socket = self._context.socket(zmq.REP)
        self._socket.bind(endpoint)
        logger.info("Replier bound at %s", endpoint)

    def poll_request(
        self, request_cls: type[T], *, timeout_ms: int | None = None
    ) -> T | None:
        if timeout_ms is not None:
            if self._socket.poll(timeout=timeout_ms) == 0:
                return None
        raw = self._socket.recv()
        return request_cls.decode(raw)

    def reply(self, message: Message) -> None:
        self._socket.send(message.encode())

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "Replier":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
