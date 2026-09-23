"""幻覺過濾。見 ARCHITECTURE.md §7、§16：VAD 前置閘門只能濾掉「完全靜音」
的段落，Whisper 仍然可能對低品質語音、背景音樂、或短促雜訊產生幻覺文字
（日文的「ご視聴ありがとうございました」是最典型案例，見 §7）。這裡是
第二道防線：`no_speech_prob` 門檻 + 已知幻覺片語黑名單。

黑名單來自語言包的 `asr.hallucination_blacklist`（見 §13.2），M1 階段
還沒有完整的語言包路由，先讓這支模組接受一個現成的 frozenset，
呼叫端（M1 是 inference/service.py，M3 之後是 pipeline.py）自己決定
黑名單從哪裡載入。
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_NO_SPEECH_THRESHOLD = 0.6


def load_blacklist(path: Path | str | None) -> frozenset[str]:
    """讀取語言包裡的 hallucination_blacklist.txt（每行一句，`#` 開頭是註解）。"""
    if path is None:
        return frozenset()
    path = Path(path)
    if not path.is_file():
        return frozenset()
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
    )


def is_hallucination(
    text: str,
    no_speech_prob: float,
    *,
    no_speech_threshold: float = DEFAULT_NO_SPEECH_THRESHOLD,
    blacklist: frozenset[str] = frozenset(),
) -> bool:
    """回傳 True 代表這段解碼結果應該被丟棄、不送去顯示成字幕。

    兩個獨立條件，任一成立就濾掉：
      1. `no_speech_prob` 過高——Whisper 自己都認為這段大概是靜音。
      2. 文字完全等於黑名單裡的已知幻覺片語（完全比對，不是子字串比對——
         子字串比對容易誤殺「剛好講到類似句子」的正常內容）。
    """
    if no_speech_prob >= no_speech_threshold:
        return True
    normalized = text.strip()
    if not normalized:
        return True  # 空字串沒有字幕價值，一併視為要濾掉
    if normalized in blacklist:
        return True
    return False
