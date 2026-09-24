"""ZeroMQ topic 常數。

PUB/SUB 的 topic 是訊息 bytes 的前綴（多段式用 "." 分隔），SUB 端用
`socket.setsockopt(zmq.SUBSCRIBE, topic.encode())` 訂閱前綴。命名規則：

    <來源進程>.<訊息類型>

這樣 gateway 可以只訂閱 `inference.` 開頭的東西，不需要知道 audio-service
內部還有哪些 topic。REQ/REP 走固定 endpoint，不需要 topic。
"""

from __future__ import annotations

# --- PUB/SUB：audio-service 發布 ---
AUDIO_UTTERANCE = "audio.utterance"  # Utterance，見 messages.py

# --- PUB/SUB：inference-service 發布 ---
INFERENCE_TRANSCRIPT = "inference.transcript"  # Transcript
INFERENCE_SUBTITLE = "inference.subtitle"  # Subtitle
INFERENCE_DEGRADE_LEVEL = "inference.degrade_level"  # DegradeLevel 變化通知

# --- REQ/REP endpoint 名稱（非 topic，是 runtime/bus.py 拿來查 socket 位址表）---
CONTROL_AUDIO_CAPTURE_TARGET = "control.audio.capture_target"  # UI → audio-service
CONTROL_LANGPACK_RELOAD = "control.inference.langpack_reload"  # UI → inference-service

# --- shared_memory 環形緩衝（見 audio/ringbuffer.py）---
# audio-service 是唯一的建立者（create=True），inference-service attach
# 進來（create=False）。名稱、容量兩邊都要一致，所以放在 contracts 這層。
AUDIO_RING_BUFFER_NAME = "sas-audio-pcm"
AUDIO_RING_BUFFER_SAMPLE_RATE = 16000
AUDIO_RING_BUFFER_SECONDS = 30  # 留 30 秒緩衝：足夠涵蓋 12s 強制斷句上限 + 餘裕
AUDIO_RING_BUFFER_CAPACITY_SAMPLES = AUDIO_RING_BUFFER_SAMPLE_RATE * AUDIO_RING_BUFFER_SECONDS

# --- Gateway（PROCESS 3 後端半部）對外的 HTTP/WebSocket 位址 ---
# 見 ARCHITECTURE.md §12：UI（Overlay/控制台）都是無狀態消費者，連這個
# WebSocket 端點拿字幕；斷線重連只要重新接上就好，Session 的狀態在
# gateway 這一端，不需要 client 自己記得漏收了什麼。
GATEWAY_HTTP_HOST = "127.0.0.1"
GATEWAY_HTTP_PORT = 8765
GATEWAY_WS_PATH = "/ws/subtitles"


def gateway_ws_url() -> str:
    return f"ws://{GATEWAY_HTTP_HOST}:{GATEWAY_HTTP_PORT}{GATEWAY_WS_PATH}"


# --- ZeroMQ endpoint 位址 ---
# 一律用 tcp://127.0.0.1，不用 ipc://：libzmq 的 ipc:// transport 在 Windows 上
# 支援度不一致（依編譯選項而定），tcp://127.0.0.1 才是 Windows 上唯一穩定可靠的
# 本機 transport。之後要跨機器搬到別台 GPU 主機時，只改這裡的 IP，不改任何服務碼。
BUS_ENDPOINTS: dict[str, str] = {
    AUDIO_UTTERANCE: "tcp://127.0.0.1:5701",
    INFERENCE_TRANSCRIPT: "tcp://127.0.0.1:5702",
    INFERENCE_SUBTITLE: "tcp://127.0.0.1:5702",  # 與 TRANSCRIPT 共用同一個 PUB socket，靠 topic 前綴區分
    INFERENCE_DEGRADE_LEVEL: "tcp://127.0.0.1:5702",
    CONTROL_AUDIO_CAPTURE_TARGET: "tcp://127.0.0.1:5711",
    CONTROL_LANGPACK_RELOAD: "tcp://127.0.0.1:5712",
}
