"""gateway/server.py 的整合測試：真的啟動 gateway 子行程（跟正式部署完全
一樣走 `python -m gateway.server` + uvicorn），用真正的 WebSocket client
連過去，搭配真的 ZMQ Publisher 發布 Subtitle——驗證「ZMQ → gateway →
WebSocket」這條真實路徑通不通。

一開始嘗試用 FastAPI 的 TestClient（in-process、不用真的開 port）測試，
但 TestClient 背後用 anyio 在另一個執行緒跑事件迴圈，跟我們在
gateway/server.py 模組層級設定的 Windows event loop policy（zmq.asyncio
在 Windows 上需要 SelectorEventLoop，見該檔案的說明）搭配起來不可靠，
訊息一直收不到。改用真的子行程 + 真的網路連線更貼近實際部署路徑，
也順便再次驗證「以 uvicorn 方式啟動」這個生產環境會走的路徑本身沒問題。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from websockets.sync.client import connect as ws_connect

from contracts.enums import EngineKind, SubtitleState
from contracts.messages import AudioSpan, Subtitle
from contracts.topics import BUS_ENDPOINTS, INFERENCE_SUBTITLE
from runtime.bus import Publisher

pytestmark = pytest.mark.slow  # 真的開子行程 + 真的網路 socket

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_PORT = 8765
WS_URL = f"ws://127.0.0.1:{GATEWAY_PORT}/ws/subtitles"


def make_subtitle(utt_id: str = "u1", revision: int = 0, text: str = "hello") -> Subtitle:
    return Subtitle(
        utt_id=utt_id,
        revision=revision,
        state=SubtitleState.DRAFT,
        span=AudioSpan(0, 16000),
        src_lang="en",
        tgt_lang="zh-Hant",
        pack_id="en-zhHant",
        source_text=text,
        target_text=text,
        engine=EngineKind.TRANSLATE_CT2_NLLB,
    )


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def gateway_process():
    proc = subprocess.Popen([sys.executable, "-m", "gateway.server"], cwd=str(ROOT))
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _port_is_listening(GATEWAY_PORT):
                break
            time.sleep(0.1)
        else:
            proc.kill()
            raise TimeoutError("gateway 子行程沒有在時限內開始監聽")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_websocket_receives_subtitle_published_over_zmq(gateway_process) -> None:
    publisher = Publisher(BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    try:
        with ws_connect(WS_URL, open_timeout=5) as ws:
            # 等 gateway 的 ZMQ SUB socket 真的連上（slow joiner，見
            # test_bus.py 的同樣說明），用重送探測直到收到為止。
            probe = make_subtitle(utt_id="probe", text="probe")
            deadline = time.monotonic() + 5.0
            received = None
            while time.monotonic() < deadline:
                publisher.publish(INFERENCE_SUBTITLE, probe)
                try:
                    received = ws.recv(timeout=0.3)
                    break
                except TimeoutError:
                    continue
            assert received is not None, "gateway 一直沒收到 ZMQ 探測訊息"

            real = make_subtitle(utt_id="u1", revision=0, text="Consider the results")
            publisher.publish(INFERENCE_SUBTITLE, real)

            found = None
            deadline2 = time.monotonic() + 5.0
            while time.monotonic() < deadline2:
                try:
                    msg = ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                decoded = Subtitle.decode(msg.encode("utf-8"))
                if decoded.utt_id == "u1":
                    found = decoded
                    break
            assert found is not None, "沒收到真正發布的 Subtitle"
            assert found.target_text == "Consider the results"
    finally:
        publisher.close()


def test_new_connection_receives_current_display_immediately(gateway_process) -> None:
    """已經有字幕在顯示時，新連上的 client 應該立刻收到目前狀態，
    不用等下一句話才看得到東西。"""
    publisher = Publisher(BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    try:
        with ws_connect(WS_URL, open_timeout=5) as ws1:
            t = make_subtitle(utt_id="u2", text="already showing")
            deadline = time.monotonic() + 5.0
            got_it = False
            while time.monotonic() < deadline:
                publisher.publish(INFERENCE_SUBTITLE, t)
                try:
                    msg = ws1.recv(timeout=0.3)
                except TimeoutError:
                    continue
                if Subtitle.decode(msg.encode("utf-8")).utt_id == "u2":
                    got_it = True
                    break
            assert got_it, "第一個 client 沒收到訊息，測試前提不成立"

            # 現在第二個 client 連上，應該立刻收到 session 目前的狀態
            with ws_connect(WS_URL, open_timeout=5) as ws2:
                msg = ws2.recv(timeout=3.0)
                decoded = Subtitle.decode(msg.encode("utf-8"))
                assert decoded.utt_id == "u2"
                assert decoded.target_text == "already showing"
    finally:
        publisher.close()


def test_stale_revision_is_not_broadcast(gateway_process) -> None:
    publisher = Publisher(BUS_ENDPOINTS[INFERENCE_SUBTITLE])
    try:
        with ws_connect(WS_URL, open_timeout=5) as ws:
            newer = make_subtitle(utt_id="u3", revision=5, text="newer")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                publisher.publish(INFERENCE_SUBTITLE, newer)
                try:
                    msg = ws.recv(timeout=0.3)
                except TimeoutError:
                    continue
                if Subtitle.decode(msg.encode("utf-8")).utt_id == "u3":
                    break

            stale = make_subtitle(utt_id="u3", revision=2, text="stale")
            publisher.publish(INFERENCE_SUBTITLE, stale)

            # 過期訊息不該被廣播出來；收到逾時代表「真的沒收到」
            with pytest.raises(TimeoutError):
                ws.recv(timeout=1.5)
    finally:
        publisher.close()
