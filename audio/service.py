"""PROCESS 1 進入點：audio-service。見 ARCHITECTURE.md §3、§6。

TODO(M1): 換成真正的擷取 → 重採樣 → VAD → segmenter 主迴圈
（capture/wasapi_loopback.py → resample.py → vad.py → segmenter.py），
把 Utterance 發布到 contracts.topics.AUDIO_UTTERANCE。
目前只是讓 runtime/supervisor.py 在 M0 有一個真實 OS 進程可以監看。
"""

from __future__ import annotations

from runtime._stub_common import run_stub_heartbeat

if __name__ == "__main__":
    run_stub_heartbeat("audio-service")
