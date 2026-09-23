"""PROCESS 3（後端半部）進入點：gateway。見 ARCHITECTURE.md §3、§12。

TODO(M2): FastAPI + WebSocket，訂閱 INFERENCE_SUBTITLE，維護
session.py 的字幕時間軸單一真相，廣播給所有連上的 UI client。
目前只是讓 runtime/supervisor.py 在 M0 有一個真實 OS 進程可以監看。
"""

from __future__ import annotations

from runtime._stub_common import run_stub_heartbeat

if __name__ == "__main__":
    run_stub_heartbeat("gateway")
