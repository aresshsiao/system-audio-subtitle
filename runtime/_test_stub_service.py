"""純粹給 tests/test_supervisor.py 用的假進程入口，不對應任何真正的服務。

M0 階段「假進程」借用的是 audio.service / inference.service / gateway.server
自己（那時它們都還是空殼，見 runtime/_stub_common.py）。M1 把它們換成真正
的實作後，繼續借用真正的服務模組來測 Supervisor 的重啟邏輯就不合適了——
會真的去開 WASAPI 裝置、真的載入 GPU 上的 ASR 模型，把一個「測進程生命
週期管理」的測試拖慢，也拖進一堆跟 Supervisor 本身無關的真實資源依賴
（而且多個測試同時搶同一個 shared_memory 名稱、同一個音效裝置，會互相
干擾）。所以另外留一個專門的輕量假進程，行為完全交給
`runtime._stub_common.run_stub_heartbeat`。
"""

from __future__ import annotations

from runtime._stub_common import run_stub_heartbeat

if __name__ == "__main__":
    run_stub_heartbeat("test-stub-service")
