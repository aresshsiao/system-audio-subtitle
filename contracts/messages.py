"""跨進程訊息模型 —— 三個進程之間唯一的耦合點。

先讀 ARCHITECTURE.md §5 再看這支檔案。規則很簡單：

  - 這裡的型別全部是 frozen dataclass：一旦建立就不可變，
    避免「哪個進程改了共享物件的某個欄位」這種查不出來的 bug。
  - 每個型別都要能透過 `encode()` / `decode()` 序列化成 ZeroMQ 訊息的 bytes。
  - 新增欄位時要問自己：這是「時間的真相」還是「內容的真相」？
    時間一律用 AudioSpan（樣本數），不要加牆上時鐘欄位進來，
    否則 SRT 匯出、推論進程重啟後的時間軸連續性會全部壞掉（見 §5 說明）。
"""

from __future__ import annotations

import json
import types
import typing
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, TypeVar, Union, get_args, get_origin

from contracts.enums import CaptureTargetKind, EngineKind, SubtitleState

T = TypeVar("T", bound="Message")


# ---------------------------------------------------------------------------
# 序列化基底
# ---------------------------------------------------------------------------


class Message:
    """所有跨進程訊息的基底類別，提供 JSON 編解碼。

    用 JSON 而不是 pickle：pickle 會讓接收端被迫信任發送端的 Python 版本與
    class 定義完全一致，且有反序列化任意物件的安全疑慮（尤其 gateway 這種
    之後可能開 TCP endpoint 給其他機器連的角色）。ZeroMQ 訊息量不大
    （字幕文字、控制指令），JSON 的序列化開銷不是瓶頸。

    `decode()` 會依 dataclass 欄位的型別註記，把巢狀 dataclass（如
    `Transcript.span: AudioSpan`）與 Enum 欄位還原成正確的物件，而不是
    留著裸 dict / str —— 否則接收端拿到的「AudioSpan」其實是個 dict，
    要等到某處呼叫 `.duration_seconds` 才會炸，而且離序列化的地方很遠。
    """

    def encode(self) -> bytes:
        return json.dumps(_to_jsonable(self), ensure_ascii=False).encode("utf-8")

    @classmethod
    def decode(cls: type[T], data: bytes) -> T:
        raw = json.loads(data.decode("utf-8"))
        type_hints = typing.get_type_hints(cls)
        kwargs = {
            f.name: _from_jsonable(raw[f.name], type_hints[f.name])
            for f in fields(cls)
            if f.name in raw
        }
        return cls(**kwargs)


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _unwrap_optional(annotation: Any) -> Any:
    """把 `X | None` / `Optional[X]` 化簡成 X，其餘型別原樣回傳。

    兩種寫法都要認：`Optional[X]` / `Union[X, None]` 的 origin 是
    `typing.Union`，但 Python 3.10+ 的 `X | None` 語法產生的是
    `types.UnionType`——只認前者的話，`Enum | None` 這類欄位解碼後會漏還原成
    裸字串（M4 加 `SetCaptureTargetAck.active_kind` 時實測踩到，之前的
    欄位型別剛好都是原始型別，`str | None` 原樣回傳本來就是對的，所以沒被發現）。
    """
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _from_jsonable(value: Any, annotation: Any) -> Any:
    if value is None:
        return None
    annotation = _unwrap_optional(annotation)
    if is_dataclass(annotation) and isinstance(value, dict):
        nested_hints = typing.get_type_hints(annotation)
        return annotation(
            **{k: _from_jsonable(v, nested_hints[k]) for k, v in value.items() if k in nested_hints}
        )
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    return value


# ---------------------------------------------------------------------------
# 時間軸
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioSpan(Message):
    """時間的唯一真相：自擷取起始的樣本數，不是牆上時鐘。

    為什麼不是牆上時鐘：
      - SRT/VTT 匯出直接用樣本數 / sample_rate 算時間碼，不需要對齊任何時鐘
      - 使用者可整體微調字幕偏移（±ms），只是位移這裡的樣本數
      - inference-service 崩潰重啟後，時間軸仍然連續（牆上時鐘重啟後對不上）
    """

    start_sample: int
    end_sample: int
    sample_rate: int = 16000

    def __post_init__(self) -> None:
        if self.end_sample < self.start_sample:
            raise ValueError(
                f"AudioSpan end_sample ({self.end_sample}) < start_sample ({self.start_sample})"
            )
        if self.sample_rate <= 0:
            raise ValueError(f"AudioSpan sample_rate must be positive, got {self.sample_rate}")

    @property
    def duration_seconds(self) -> float:
        return (self.end_sample - self.start_sample) / self.sample_rate

    def start_seconds(self, offset_ms: int = 0) -> float:
        return self.start_sample / self.sample_rate + offset_ms / 1000.0

    def end_seconds(self, offset_ms: int = 0) -> float:
        return self.end_sample / self.sample_rate + offset_ms / 1000.0


# ---------------------------------------------------------------------------
# audio-service → inference-service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Utterance(Message):
    """VAD 判定的一段語音。utt_id 在整條管線中恆定不變。

    audio-service 在偵測到語音起點時就會送出 closed=False 的版本（讓
    inference-service 可以開始跑暫定稿的滑動視窗），句尾靜音判定或強制斷句
    上限觸發時再送一次 closed=True。
    """

    utt_id: str  # ULID，字典序即時間序
    span: AudioSpan
    closed: bool  # False=仍在說話, True=已偵測到句尾靜音或觸發強制斷句


# ---------------------------------------------------------------------------
# inference-service 內部與對外
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transcript(Message):
    """ASR 產生的原文（尚未翻譯）。"""

    utt_id: str
    revision: int  # 同一 utt_id 的第幾次修訂，從 0 開始
    state: SubtitleState  # 只會是 DRAFT 或 FINAL，Transcript 沒有 POLISHED
    span: AudioSpan
    text: str
    src_lang: str  # BCP-47，ASR 偵測到的來源語言，例 "ja"
    lang_locked: bool  # True=使用者指定語言（見 §13.4 單包鎖定模式）, False=自動偵測
    stable_chars: int  # LocalAgreement-2 已確定的前綴長度（字元數，§8）
    no_speech_prob: float  # Whisper 的靜音/幻覺機率，供 hallucination 過濾使用

    def __post_init__(self) -> None:
        if self.state == SubtitleState.POLISHED:
            raise ValueError("Transcript.state 不可為 POLISHED（那是 Subtitle 的狀態）")
        if self.revision < 0:
            raise ValueError(f"revision 不可為負數: {self.revision}")
        if self.stable_chars < 0 or self.stable_chars > len(self.text):
            raise ValueError(
                f"stable_chars ({self.stable_chars}) 超出 text 長度 ({len(self.text)})"
            )


@dataclass(frozen=True)
class Subtitle(Message):
    """翻譯後、準備送進 gateway/UI 的字幕。

    三個欄位撐起「不閃爍字幕」（見 §5）：
      - utt_id   → UI 用來就地取代而非追加
      - revision → 亂序到達時丟棄舊修訂
      - state    → 決定渲染樣式與是否還會被覆寫

    src_lang / tgt_lang / pack_id 讓字幕自我描述是哪個語言包產生的（見 §13.3），
    UI 不需要另外查設定檔就知道該用什麼字型、換行規則。
    """

    utt_id: str
    revision: int
    state: SubtitleState
    span: AudioSpan
    src_lang: str  # BCP-47，例 "ja"
    tgt_lang: str  # BCP-47，例 "zh-Hant"
    pack_id: str  # 產生此字幕的語言包 id，例 "ja-zhHant"
    source_text: str
    target_text: str
    engine: EngineKind

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError(f"revision 不可為負數: {self.revision}")


# ---------------------------------------------------------------------------
# UI/Gateway → audio-service：控制指令
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureTarget(Message):
    """擷取目標描述，對應 ARCHITECTURE.md §6 的三層 Backend。

    這是唯一允許「反向」流動的訊息（UI → audio-service），走 REQ/REP
    而非 PUB/SUB，因為需要確認擷取端真的切換成功了才能回報 UI。
    """

    kind: CaptureTargetKind
    device_id: str | None = None  # kind=ENDPOINT 時使用
    pid: int | None = None  # kind=PROCESS 時使用
    include_process_tree: bool = True  # kind=PROCESS 時使用（見 §6 陷阱 1）

    def __post_init__(self) -> None:
        if self.kind == CaptureTargetKind.ENDPOINT and self.device_id is None:
            raise ValueError("CaptureTargetKind.ENDPOINT 需要 device_id")
        if self.kind == CaptureTargetKind.PROCESS and self.pid is None:
            raise ValueError("CaptureTargetKind.PROCESS 需要 pid")


@dataclass(frozen=True)
class SetCaptureTargetAck(Message):
    """audio-service 對 `CaptureTarget` 切換請求的回覆。

    `success=True` 不代表「照使用者要的那層擷取」——Tier 2 啟用失敗時會自動退
    回 Tier 1（見 ARCHITECTURE.md §16），此時仍是 `success=True`（有在擷取），
    但 `active_kind` 會跟請求的不同，`warning` 說明退回的原因，UI 要把這句
    話明確顯示給使用者，不能靜默退回讓人以為還是行程級擷取。
    """

    success: bool
    active_kind: CaptureTargetKind | None = None
    description: str = ""  # 給 UI 顯示用，例如 "行程 chrome.exe (PID 1234)"
    warning: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# UI → inference-service：語言包控制（見 ARCHITECTURE.md §13.4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetActiveLangPacks(Message):
    """設定目前啟用的語言包集合（取代，不是疊加）。走 REQ/REP，UI 下拉選單
    切換後要能確認 inference-service 真的收到了、套用了——不是發出去就算。
    """

    pack_ids: list[str]


@dataclass(frozen=True)
class SetActiveLangPacksAck(Message):
    success: bool
    active_pack_ids: list[str]
    error: str | None = None
