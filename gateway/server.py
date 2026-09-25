"""PROCESS 3（後端半部）進入點：gateway。見 ARCHITECTURE.md §3、§12。

FastAPI + WebSocket：訂閱 inference-service 的 `Subtitle`（含譯文），
維護 `session.Session`（字幕時間軸單一真相），廣播給所有連上的
WebSocket client（Overlay、控制台、未來的網頁 UI）。client 都是無狀態
消費者——斷線重連只要重新接上，連上的當下會先收到目前的顯示狀態，
不需要自己記得漏收了什麼。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Windows 上 asyncio 預設用 ProactorEventLoop，但 pyzmq 的 zmq.asyncio
# 需要 `add_reader` 這組方法，Proactor 沒實作、只有 SelectorEventLoop 有
# ——不設這個會在第一次 `await socket.recv_multipart()` 時整個炸掉
# `RuntimeError: Proactor event loop does not implement add_reader`。
# 必須在任何 asyncio 事件迴圈建立「之前」設定（模組載入時就設，不能等到
# `main()` 才設，否則 pytest 用 TestClient 隱含建立的迴圈會搶先用到
# 錯的 policy）。見 tests/test_gateway_server.py 的整合測試踩到這個坑。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import zmq
import zmq.asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from contracts.messages import Subtitle
from contracts.topics import (
    BUS_ENDPOINTS,
    GATEWAY_HTTP_HOST,
    GATEWAY_HTTP_PORT,
    GATEWAY_WS_PATH,
    INFERENCE_SUBTITLE,
)
from gateway.session import Session
from utils.logging import setup_logging

logger = logging.getLogger(__name__)

app = FastAPI()
session = Session()
_clients: set[WebSocket] = set()
_zmq_ctx = zmq.asyncio.Context()


@app.websocket(GATEWAY_WS_PATH)
async def ws_subtitles(websocket: WebSocket) -> None:
    await websocket.accept()
    _clients.add(websocket)
    logger.info("client connected, total=%d", len(_clients))
    try:
        current = session.current_display()
        if current is not None:
            await websocket.send_text(current.encode().decode("utf-8"))
        while True:
            # 純粹保持連線活著；client 不需要送任何東西過來，收到就丟掉。
            # 斷線時 receive_text() 會丟 WebSocketDisconnect，藉此偵測離線。
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
        logger.info("client disconnected, total=%d", len(_clients))


async def _broadcast(subtitle: Subtitle) -> None:
    if not _clients:
        return
    message = subtitle.encode().decode("utf-8")
    dead: list[WebSocket] = []
    for ws in _clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _zmq_subscriber_loop() -> None:
    socket = _zmq_ctx.socket(zmq.SUB)
    socket.connect(BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    socket.setsockopt(zmq.SUBSCRIBE, INFERENCE_SUBTITLE.encode("utf-8"))
    logger.info("subscribed to %s at %s", INFERENCE_SUBTITLE, BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    try:
        while True:
            _topic, payload = await socket.recv_multipart()
            subtitle = Subtitle.decode(payload)
            if session.apply(subtitle):
                await _broadcast(subtitle)
    except asyncio.CancelledError:
        pass
    finally:
        socket.close()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    zmq_task = asyncio.create_task(_zmq_subscriber_loop())
    try:
        yield
    finally:
        zmq_task.cancel()
        try:
            await zmq_task
        except asyncio.CancelledError:
            pass


app.router.lifespan_context = _lifespan


def main() -> None:
    import uvicorn

    setup_logging("gateway")
    uvicorn.run(app, host=GATEWAY_HTTP_HOST, port=GATEWAY_HTTP_PORT, log_level="warning")


if __name__ == "__main__":
    main()
