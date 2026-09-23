"""跨進程共用的列舉型別。

這些值會被序列化進 ZeroMQ 訊息（見 topics.py），三個進程都要認得同一份定義，
所以只能放在 contracts/ 裡，不能在個別服務內各自定義字串常數。
"""

from __future__ import annotations

from enum import Enum


class SubtitleState(str, Enum):
    """字幕的穩定程度，決定 UI 渲染樣式（見 ARCHITECTURE.md §8、§12）。"""

    DRAFT = "draft"        # 暫定稿：灰字，會被同一 utt_id 的後續 revision 取代
    FINAL = "final"        # 定稿：白字，VAD 收句後產生，理論上不再變動
    POLISHED = "polished"  # 精修：雲端 LLM 重譯後靜默替換定稿內容


class EngineKind(str, Enum):
    """`Subtitle.engine` / `Transcript` 產生來源，供 metrics 與除錯使用。"""

    ASR_FASTER_WHISPER = "asr:faster-whisper"
    TRANSLATE_CT2_NLLB = "translate:ct2-nllb"
    TRANSLATE_LLM_API = "translate:llm-api"


class DegradeLevel(int, Enum):
    """背壓降級階梯，數字越大品質損失越大（見 ARCHITECTURE.md §11）。

    IntEnum 語意：DegradeLevel.L2 >= DegradeLevel.L1 這種比較要成立，
    supervisor / degrade.py 用比較運算判斷「有沒有比目前更糟」。
    """

    L0_NORMAL = 0
    L1_FINAL_BEAM_DOWN = 1       # 定稿 beam_size 5 → 1
    L2_DRAFT_INTERVAL_UP = 2     # 暫定稿更新頻率 250ms → 500ms
    L3_CLOUD_POLISH_OFF = 3      # 關閉雲端精修
    L4_MODEL_DOWNGRADE = 4       # large-v3 → distil-large-v3
    L5_DROP_OLDEST = 5           # 丟棄最舊未處理段落


class CaptureTargetKind(str, Enum):
    """CaptureBackend 的擷取目標種類（見 ARCHITECTURE.md §6）。"""

    ENDPOINT = "endpoint"    # Tier 1: WASAPI loopback，整個輸出端點
    PROCESS = "process"      # Tier 2: 指定 PID (+ 是否含子行程樹)
    VIRTUAL_CABLE = "virtual_cable"  # Tier 3: 使用者手動路由的虛擬裝置
