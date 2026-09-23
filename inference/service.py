"""PROCESS 2 進入點：inference-service。見 ARCHITECTURE.md §3、§8、§9。

TODO(M1-M5): 訂閱 AUDIO_UTTERANCE → ASR (asr/faster_whisper_engine.py)
→ stabilizer.py (LocalAgreement-2) → langpack.py 路由 → translate/ →
degrade.py 背壓控制，發布 Transcript/Subtitle。
目前只是讓 runtime/supervisor.py 在 M0 有一個真實 OS 進程可以監看。
"""

from __future__ import annotations

from runtime._stub_common import run_stub_heartbeat

if __name__ == "__main__":
    run_stub_heartbeat("inference-service")
