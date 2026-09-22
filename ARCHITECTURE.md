# System Audio Subtitle — 軟體架構設計文件

> 針對「媒體聲音」的 Windows 即時翻譯字幕工具。
> 語言方向：**來源與目標語言皆可選、可匯入擴充**（首批內建 英/日/泰 → 繁中，另附 繁中 → 英 反向包，見 §13）
> 翻譯路線：**本地為主 + 雲端精修（混合）**
> 顯示：**透明置頂浮層**　字幕策略：**兩段式（暫定稿 → 定稿）**

---

## 1. 系統總覽

本系統採 **三進程分離架構**。不是為了微服務而微服務 —— 是因為即時音訊管線有三種
彼此衝突的性能需求，硬塞進同一個 Python 進程必然互相傷害（見 §2）。

```
┌────────────────────────────────────────────────────────────────────┐
│  PROCESS 3 :  gateway + ui         (可關可開，不影響上游)            │
│                                                                    │
│   Overlay 浮層 (PySide6)      控制台 / 歷史面板                     │
│   ├ 暫定稿：灰字、會改寫       ├ 逐句歷史、可複製                    │
│   └ 定稿：白字、不再變動       └ SRT / VTT 匯出                     │
│                    ▲                          ▲                    │
│                    └──────────┬───────────────┘                    │
│                      Session（字幕時間軸的單一真相）                 │
└───────────────────────────────┬────────────────────────────────────┘
                                │  ZeroMQ PUB/SUB：Subtitle 事件
                                │  ZeroMQ REQ/REP：控制指令
┌───────────────────────────────┴────────────────────────────────────┐
│  PROCESS 2 :  inference-service    (獨佔 GPU，可崩潰可重啟)          │
│                                                                    │
│   ASR Engine ──→ Stabilizer ──→ Translator ──→ Polisher            │
│   faster-whisper  LocalAgree-2   本地 NLLB-CT2   雲端 LLM（可選）   │
│   large-v3 int8   抑制閃爍       上下文 N 句     3~5 句重譯          │
│                                                                    │
│   ▲ 降級階梯：beam↓ → 模型↓ → 更新頻率↓ → 關精修 → 最後才丟幀       │
└───────────────────────────────┬────────────────────────────────────┘
        PCM：shared_memory 環形緩衝（零複製）  │  事件：ZeroMQ
┌───────────────────────────────┴────────────────────────────────────┐
│  PROCESS 1 :  audio-service        (純 CPU、高優先權、永不阻塞)      │
│                                                                    │
│   CaptureBackend ──→ Resample ──→ VAD ──→ Segmenter                │
│        │              →16k mono   Silero  語音段落切分              │
│        │                                                           │
│   ┌────┴──────────────────────────────────────────┐                │
│   │ Tier 1  WASAPI Loopback（整個輸出端點）        │                │
│   │ Tier 2  Process Loopback（指定 PID ★核心差異化）│                │
│   │ Tier 3  Virtual Cable（VB-CABLE 後備）         │                │
│   └───────────────────────────────────────────────┘                │
└────────────────────────────────────────────────────────────────────┘
                                ▲
                        Windows 音訊引擎
                  （Chrome / mpv / Netflix / VLC …）
```

---

## 2. 為什麼一定要拆成三個進程

這是整份設計最需要被說服的一點，逐條說明：

| 理由 | 具體後果 |
|------|---------|
| **GIL 爭用** | 擷取執行緒必須每 10ms 準時取走音訊，否則 WASAPI 緩衝溢位 → 爆音、丟幀。faster-whisper 的推論是長時間持有 GIL 的 C 擴充呼叫，同進程下會直接把擷取執行緒餓死。**這是硬性隔離，不是優化。** |
| **崩潰隔離** | CUDA OOM、驅動 TDR 重置會殺掉推論進程。此時音訊仍持續寫進 shared memory 環形緩衝，推論進程重啟後可從緩衝補回，使用者只看到字幕停頓幾秒，不是整個程式死掉。 |
| **獨立替換** | ASR 引擎（faster-whisper → Parakeet → 其他）與翻譯引擎的替換，只要遵守 `contracts/` 的訊息 schema，其他兩個進程一行都不用改。 |
| **UI 生命週期解耦** | Overlay 可以隨時關閉、重開、切換螢幕、改字型，上游擷取與推論完全不受影響。 |
| **可跨機器** | ZeroMQ 的 TCP transport 讓 inference-service 之後可以搬到另一台有更強 GPU 的機器，只改 endpoint 設定。 |

**但不要過度拆分。** 三個進程是上限，不是起點。VAD、分段這種微秒級的純 CPU 運算
留在 audio-service 內部當模組即可，拆出去只會增加 IPC 延遲。

---

## 3. 三進程職責界線

| 進程 | 職責 | 生命週期 | 狀態 | 性能目標 |
|------|------|---------|------|---------|
| **audio-service** | 擷取、重採樣、VAD、語音段落切分 | 隨系統常駐 | 環形緩衝（可丟棄） | 抖動 < 5ms，永不阻塞 |
| **inference-service** | ASR、假說穩定化、翻譯、精修 | 可獨立崩潰/重啟 | 無狀態（上下文可重建） | 吞吐優先，允許排隊 |
| **gateway + ui** | 字幕時間軸真相、WS 廣播、渲染、匯出 | 隨使用者開關 | 有狀態（Session 時間軸） | 渲染 60fps |

**單向資料流原則**：音訊只往上游流，控制指令只往下游流。任何反向的直接呼叫都是設計錯誤。

---

## 4. 目錄結構

```
system-audio-subtitle/
├── ARCHITECTURE.md              # 本文件
├── ROADMAP.md                   # 分期實作計畫與驗收標準
├── pyproject.toml
├── requirements.txt
│
├── contracts/                   # ★ 跨進程唯一真相，先寫這層
│   ├── messages.py              #   AudioSpan / Utterance / Transcript / Subtitle
│   ├── topics.py                #   ZMQ topic 常數
│   ├── enums.py                 #   SubtitleState / EngineKind / DegradeLevel
│   └── langpack_schema.py       #   語言包 JSON Schema + 版本相容性規則 ★
│
├── audio/                       # ── PROCESS 1
│   ├── service.py               #   進程進入點與主迴圈
│   ├── capture/
│   │   ├── base.py              #   CaptureBackend 抽象介面
│   │   ├── wasapi_loopback.py   #   Tier 1：端點級（PyAudioWPatch）
│   │   ├── process_loopback.py  #   Tier 2：行程級（ctypes + COM）★
│   │   ├── virtual_cable.py     #   Tier 3：VB-CABLE 後備
│   │   └── enumerate.py         #   裝置/行程列舉，供 UI 選擇音源
│   ├── ringbuffer.py            #   shared_memory 環形緩衝（無鎖 SPSC）
│   ├── resample.py              #   任意取樣率 → 16k mono float32
│   ├── vad.py                   #   Silero VAD 封裝
│   └── segmenter.py             #   語音段落切分（含日/泰文特例）
│
├── inference/                   # ── PROCESS 2
│   ├── service.py
│   ├── langpack.py              #   語言包 registry / 驗證 / 熱載入 ★
│   ├── asr/
│   │   ├── base.py              #   ASREngine 抽象
│   │   ├── faster_whisper_engine.py
│   │   └── hallucination.py     #   幻覺過濾（黑名單來自語言包）
│   ├── stabilizer.py            #   LocalAgreement-2 假說穩定化 ★
│   ├── translate/
│   │   ├── base.py              #   Translator 抽象
│   │   ├── ct2_nllb.py          #   本地 NLLB-200 (CTranslate2)
│   │   ├── llm_api.py           #   雲端 LLM 精修
│   │   ├── context.py           #   前 N 句上下文視窗
│   │   └── glossary.py          #   術語表載入（路徑來自語言包）
│   ├── pipeline.py              #   兩段式編排（draft / final / polished）
│   └── degrade.py               #   降級階梯控制器
│
├── gateway/                     # ── PROCESS 3（後端半部）
│   ├── server.py                #   FastAPI + WebSocket
│   ├── session.py               #   字幕時間軸單一真相
│   └── export.py                #   SRT / VTT / 純文字
│
├── ui/                          # ── PROCESS 3（前端半部）
│   ├── app.py                   #   Qt 應用進入點 + 系統匣
│   ├── overlay/
│   │   ├── window.py            #   無邊框 / 置頂 / 點擊穿透
│   │   ├── renderer.py          #   雙態渲染（暫定灰 / 定稿白）
│   │   └── layout.py            #   多螢幕 + 混合 DPI 定位
│   ├── panel/                   #   控制台、歷史、設定
│   │   └── langpack_manager.py  #   語言包選擇 / 啟用 / 匯入 UI ★
│   └── hotkeys.py               #   全域熱鍵
│
├── runtime/
│   ├── supervisor.py            #   進程生命週期、健康檢查、自動重啟
│   ├── bus.py                   #   ZeroMQ 封裝（PUB/SUB + REQ/REP）
│   └── clock.py                 #   音訊時鐘（樣本數 ↔ 時間）
│
├── config/
│   ├── default.yaml
│   ├── profiles/                #   anime.yaml / meeting.yaml / lecture.yaml
│   └── langpacks/                #   內建語言包（見 §13），使用者可另外匯入
│
├── models/                      # 模型權重（.gitignore）
├── scripts/
│   ├── setup_models.py          #   下載並轉換模型
│   ├── bench_latency.py         #   端到端延遲基準
│   └── probe_audio.py           #   列出可擷取的裝置與行程
├── utils/{logging,metrics}.py
└── tests/
```

---

## 5. 契約層：訊息模型

**先寫 `contracts/`，再寫任何一個服務。** 這層是三個進程之間唯一的耦合點。

```python
# contracts/messages.py

@dataclass(frozen=True)
class AudioSpan:
    """時間的唯一真相：自擷取起始的樣本數，不是牆上時鐘。"""
    start_sample: int
    end_sample:   int
    sample_rate:  int = 16000

@dataclass(frozen=True)
class Utterance:
    """VAD 判定的一段語音。utt_id 在整條管線中恆定不變。"""
    utt_id: str            # ULID，單調遞增
    span:   AudioSpan
    closed: bool           # False=仍在說話, True=已偵測到句尾靜音

@dataclass(frozen=True)
class Transcript:
    utt_id: str
    revision: int          # 同一 utt_id 的第幾次修訂
    state: SubtitleState   # DRAFT | FINAL
    span: AudioSpan
    text: str
    src_lang: str          # BCP-47，ASR 偵測或使用者指定
    lang_locked: bool      # True=使用者指定, False=自動偵測（見 §13.2）
    stable_chars: int      # LocalAgreement 已確定的前綴長度（字元）
    no_speech_prob: float

@dataclass(frozen=True)
class Subtitle:
    utt_id: str
    revision: int
    state: SubtitleState   # DRAFT | FINAL | POLISHED
    span: AudioSpan
    src_lang: str          # BCP-47，例 "ja"
    tgt_lang: str          # BCP-47，例 "zh-Hant"
    pack_id: str           # 產生此字幕的語言包，例 "ja-zhHant@1.2"
    source_text: str
    target_text: str
    engine: str            # "ct2-nllb" | "llm-api" | ...
```

`src_lang` / `tgt_lang` / `pack_id` 三個欄位讓字幕**自我描述**：UI 不需要去問設定檔
就知道該用哪套字型與換行規則，匯出 SRT 時也能正確標註語言。
語言是**資料，不是編譯期常數** —— 這是 §13 語言包機制的前提。

### 三個欄位撐起整個「不閃爍字幕」

| 欄位 | 作用 |
|------|------|
| `utt_id` | UI 據此**就地取代**而非追加 —— 字幕不會越長越多行 |
| `revision` | 亂序到達時丟棄舊修訂 —— ZMQ 與多 worker 下必要 |
| `state` | 決定渲染樣式（灰/白）與是否允許再被覆寫 |

`AudioSpan` 用樣本數而非牆上時鐘，帶來三個好處：SRT 匯出免對齊、
使用者可整體微調字幕偏移（±ms）、推論進程重啟後時間軸仍然連續。

---

## 6. 音訊擷取：三層 Backend 策略

這是整個專案技術含量最高、也最容易低估的部分。

### Tier 1 — WASAPI Loopback（端點級）

`PyAudioWPatch` 提供的 `*.loopback` 裝置，擷取**整個輸出端點的混音**。

- ✅ 實作簡單、相容所有 Windows 10/11、無需額外權限
- ❌ 會混進 Discord 語音、通知音效、遊戲音效 —— **這正是「媒體聲音」訴求要解決的問題**
- 📌 用途：Phase 1 打通管線、以及 Tier 2 不可用時的後備

### Tier 2 — Process Loopback Capture（行程級）★ 核心差異化

Windows 10 build 20348+ / Windows 11 提供 `ActivateAudioInterfaceAsync` 搭配
`AUDIOCLIENT_ACTIVATION_PARAMS`，可**只擷取指定 PID 的播放串流**。

```
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE  ← 抓 chrome.exe 全家
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE  ← 抓「除了 Discord 以外」
```

這才是真正的「只翻譯媒體聲音」。目前**沒有任何 Python 套件包裝這個 API**，
必須自己用 `ctypes` 打 COM，或寫一個小 pybind11 擴充。

已知陷阱（實作前必讀）：

1. **瀏覽器是多行程的** —— Chrome / Edge 的音訊實際由 audio service 子行程輸出，
   必須用 `INCLUDE_TARGET_PROCESS_TREE` 對主行程下手，抓單一 PID 會得到靜音。
2. **不支援事件驅動模式** —— 不能用 `AUDCLNT_STREAMFLAGS_EVENTCALLBACK`，
   只能輪詢。輪詢週期直接決定抖動，建議 10ms 並配合 `avrt.dll` 的
   `AvSetMmThreadCharacteristics("Pro Audio")` 提升執行緒優先權。
3. **目標沒出聲時拿到的是靜音而非「無資料」** —— 不能靠「沒資料」判斷沒在播放。
4. **目標行程結束後串流不會自己收掉** —— 需要監看 PID 存活並主動重建。

### Tier 3 — Virtual Cable（後備）

VB-CABLE 等虛擬音效裝置。需要使用者手動安裝並把播放器輸出指過去。
只在前兩者都失敗時提示使用者，不是主要路徑。

### 統一介面

```python
class CaptureBackend(Protocol):
    def open(self, target: CaptureTarget) -> None: ...
    def read(self) -> np.ndarray | None: ...   # float32, 原生取樣率
    @property
    def format(self) -> AudioFormat: ...
    def close(self) -> None: ...
```

`CaptureTarget` 可以是 `Endpoint(device_id)` 或 `Process(pid, include_tree=True)`。
上層完全不知道底下是哪一層 —— 這是 backend 可以分期實作的前提。

---

## 7. VAD 與語音段落切分

```
音訊 10ms 幀
   ↓
Silero VAD（ONNX，CPU 推論 < 1ms/幀）
   ↓
狀態機：SILENCE ──語音≥100ms──→ SPEAKING ──靜音≥350ms──→ 收句
                                    └──長度≥12s──────────→ 強制斷句
```

**為什麼必須有 VAD**：不是為了省算力，而是為了**抑制 Whisper 幻覺**。
對純靜音段做 ASR，Whisper 會憑空產生文字（日文的「ご視聴ありがとうございました」
是最著名案例）。靜音不送進 ASR，是品質提升最大的單一開關。

**強制斷句上限 12 秒**：講者不停頓時不能無限等下去，否則定稿永遠不出現。

---

## 8. 兩段式字幕與假說穩定化 ★

這是「平衡」路線的技術核心。同一段語音會產生兩種輸出：

| | 暫定稿 DRAFT | 定稿 FINAL | 精修 POLISHED（可選） |
|---|---|---|---|
| **觸發** | 每 250ms 滑動視窗 | VAD 收句 / 12s 上限 | 定稿後 3~5 句批次 |
| **解碼** | `beam_size=1`, greedy | `beam_size=5` + 前文 prompt | — |
| **翻譯** | 本地 NLLB，無上下文 | 本地 NLLB + 前 N 句上下文 | 雲端 LLM 整段重譯 |
| **渲染** | 灰字，會改寫 | 白字，不再變動 | 白字，靜默替換 |
| **延遲** | ~0.9s | 句末後 ~0.7s | +1~3s |

### LocalAgreement-2：為什麼字幕不會瘋狂閃爍

滑動視窗重複解碼會讓文字不斷跳動。解法是**只顯示連續兩次解碼一致的前綴**：

```
t=0.25s  解碼 → "I think we should"
t=0.50s  解碼 → "I think we shall consider"
         共同前綴 "I think we" → 標記為 stable，之後不再改寫
         "shall consider" 仍為 unstable，可能再變
t=0.75s  解碼 → "I think we shall consider the"
         共同前綴延伸至 "I think we shall consider"
```

`Transcript.stable_chars` 就是這個前綴長度。UI 可以把 stable 部分用正常灰、
unstable 部分用更淡的灰，視覺上讓使用者知道哪裡還會變。

**比對粒度按語言而定**：泰文沒有詞間空格，前綴比對**必須在字元層級**做，不能切空格；
日文同理。英文可用詞層級。`Stabilizer` 的粒度不是寫死的 if/else，而是讀
`LanguagePack.stabilizer_granularity`（見 §13）—— 新增一個沒有空格分詞的語言，
不需要改這支程式碼。

### 語序問題：為什麼有些語言暫定稿不能先翻

日文是 SOV，動詞與否定在句尾。「行きます」vs「行きません」只差句尾兩個字，
語意完全相反 —— 暫定稿階段翻譯日文幾乎必錯。這類「語序會反轉語意」的語言，
暫定稿階段應**只顯示原文**，等定稿才出翻譯；其他語言則暫定稿就可以翻。

**設計決策**：這是語言配對的固有特性，不是使用者情境（profile）的事，
所以是語言包裡的一個宣告欄位 `draft_translate: bool`，而不是寫死在程式碼或
`config/default.yaml` 裡的全域規則。完整機制見 §13。

---

## 9. 翻譯層

### 為什麼逐句翻譯一定翻得爛

字幕是**對話流**，不是獨立句子。代名詞（it / 彼 / เขา）、省略主詞、跨句指涉，
單句送進 MT 必然丟失。所有真正堪用的字幕翻譯都必須帶上下文。

```python
class TranslationContext:
    """滾動保留最近 N 組已定稿的 (原文, 譯文)。"""
    window: deque[tuple[str, str]]   # maxlen = 5
    glossary: dict[str, str]         # 人名 / 專有名詞強制對應
```

- **本地即時**：預設引擎是 NLLB-200-distilled-600M 轉成 CTranslate2 int8。
  它支援的語言代碼（如 `eng_Latn` / `jpn_Jpan` / `tha_Thai` / `zho_Hant`）**不寫死在程式碼裡**，
  而是由每個語言包的 `translate.src_code` / `translate.tgt_code` 宣告 —— 翻譯方向本身
  就是資料，所以「繁中 → 英」這種反向配對只是另一個語言包，不是另一套程式碼。短句 < 100ms。
- **雲端精修**：定稿累積 3~5 句後，連同上下文一次送 LLM 重譯，
  回來後**靜默替換**已顯示的定稿（state 升為 POLISHED）。
  使用者看到的是字幕「悄悄變好」，而不是延遲變長。
- **術語表**：動畫人名、公司名、技術術語。本地引擎用強制替換，
  雲端引擎寫進 system prompt。這是「專屬工具」相對通用服務最大的優勢。

`Translator` 介面統一，雲端不可用（斷網、額度用盡）時自動降級回本地，
使用者只損失品質不損失功能 —— 這是選「混合」路線的全部意義。

---

## 10. 延遲預算

目標：**暫定稿 < 1.2s，定稿在句末後 < 0.8s**。逐項拆解（RTX 4070）：

| 階段 | 預算 | 備註 |
|------|------|------|
| WASAPI 擷取緩衝 | 20–30 ms | 10ms 輪詢 + 抖動餘裕 |
| 環形緩衝 + IPC | < 2 ms | shared_memory 零複製 |
| 重採樣 → 16k mono | < 3 ms | |
| VAD | < 1 ms | Silero ONNX CPU |
| **視窗累積（暫定稿）** | **500–700 ms** | **主要延遲來源，可調** |
| ASR large-v3 int8, beam=1 | 120–250 ms | 1s 音訊 |
| 本地 MT（NLLB-CT2 int8） | 60–150 ms | 短句 |
| ZMQ 廣播 + Qt 渲染 | < 20 ms | |
| **暫定稿合計** | **≈ 0.9–1.2 s** | |
| 句末靜音判定 | 350 ms | VAD 門檻 |
| ASR beam=5 + 前文 prompt | 250–400 ms | |
| MT 帶上下文 | 100–200 ms | |
| **定稿合計（句末後）** | **≈ 0.7–1.0 s** | |

`scripts/bench_latency.py` 必須能量到**每一格**，不能只量端到端 ——
沒有分項數字就無法知道該優化哪裡。

---

## 11. 背壓與降級階梯

**鐵則：音訊擷取永不阻塞。** 環形緩衝滿了就覆蓋最舊的資料，絕不回壓上游。
絕不讓佇列無限增長 —— 那會導致延遲隨時間單調遞增，是即時系統最常見的死法。

當 inference 跟不上（以「暫定稿實際間隔 > 目標間隔 × 1.5」判定），
`degrade.py` **自動**逐級降級，而不是直接丟幀：

| 級別 | 動作 | 品質損失 |
|------|------|---------|
| L0 | 正常 | — |
| L1 | 定稿 `beam_size` 5 → 1 | 定稿略差 |
| L2 | 暫定稿更新 250ms → 500ms | 字幕較跳 |
| L3 | 關閉雲端精修 | 失去潤飾 |
| L4 | large-v3 → distil-large-v3 | 辨識率下降 |
| L5 | 丟棄最舊未處理段落 | **字幕缺漏** |

負載回落後逐級恢復，並加遲滯（hysteresis）避免在邊界反覆震盪。
每次升降級都寫 log 與 metric —— 使用者抱怨「今天字幕怪怪的」時才查得出原因。

---

## 12. Overlay 渲染：Windows 的真實陷阱

透明置頂浮層看似簡單，實際有一串平台細節：

| 問題 | 解法 |
|------|------|
| 滑鼠點不到底下的影片 | `WS_EX_TRANSPARENT` + Qt `WA_TransparentForMouseEvents`，另設熱鍵切「編輯模式」暫時關閉穿透以供拖曳 |
| 點字幕會搶走播放器焦點 | `WS_EX_NOACTIVATE` |
| 出現在 Alt-Tab / 工作列 | `WS_EX_TOOLWINDOW` + Qt `Qt.Tool` |
| **獨佔全螢幕蓋掉浮層** | 無解，屬 Windows 行為。UI 需明確引導使用者改用「無邊框視窗全螢幕」 |
| 多螢幕混合 DPI 字體錯位 | per-monitor DPI aware v2 manifest + `QScreen.devicePixelRatio()`，螢幕切換時重算 |
| 錄影 / 直播時字幕被錄進去 | `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` 可選排除 |
| 深色影片上看不清字 | 描邊（outline）而非陰影，外加可調半透明底條 |

字幕文字渲染只做**兩種狀態**：暫定灰、定稿白。不要做淡入淡出動畫 ——
文字持續改寫時，動畫會讓可讀性大幅下降。

---

## 13. 語言選擇與匯入：語言包機制 ★

### 13.1 為什麼語言不能寫死在程式碼裡

上一版設計把「英/日/泰 → 繁中」直接寫進 stabilizer 的 if/else 與 config 的
`languages:` 表。使用者提出的需求很準確：**語言方向應該是可選、可匯入的**，
不是重新編譯才能加一種語言。凡是「這個語言配對怎麼處理」的知識——
ASR 選型、比對粒度、暫定稿要不要先翻、換行規則、術語表、幻覺黑名單、
翻譯引擎的語言代碼——全部收斂成**一份宣告式資料**，程式碼只認界面，不認語言。

### 13.2 語言包（Language Pack）的組成

一個語言包是一個資料夾（或匯入時打包成單一 `.langpack.zip`）：

```
ja-zhHant/
├── pack.yaml            # 唯一必要檔案
├── glossary.tsv         # 選用：人名/專有名詞強制對應
└── hallucination.txt    # 選用：幻覺片語黑名單（每行一句）
```

```yaml
# pack.yaml
schema_version: 1              # contracts/langpack_schema.py 檢查相容性
id: ja-zhHant
display_name: "日文 → 繁體中文"
version: "1.2.0"

src_lang: ja                   # BCP-47
tgt_lang: zh-Hant

asr:
  engine: faster-whisper        # 對應 inference/asr/ 底下的實作
  model: large-v3
  hallucination_blacklist: hallucination.txt

stabilizer:
  granularity: char             # word | char —— 日/泰用 char

policy:
  draft_translate: false        # SOV 語序，暫定稿只顯示原文（見 §8）
  line_break: phrase            # space | phrase | width

translate:
  local_engine: ct2-nllb
  src_code: jpn_Jpan             # 引擎專屬的語言代碼，由包宣告，不寫死在程式碼
  tgt_code: zho_Hant
  cloud_engine: llm-api          # 選用
  glossary: glossary.tsv

typography:
  font_hint: "Noto Sans TC"
```

`contracts/langpack_schema.py` 定義這份 YAML 的 schema 並做驗證；
`inference/langpack.py` 是 registry：啟動時掃描 `config/langpacks/`
與使用者匯入目錄，驗證、載入、供 UI 查詢。

**語言包是純資料，不含任何可執行程式碼**——這是刻意的安全邊界（見 §16）。
`engine` 欄位只能是白名單裡已知的實作名稱，不是任意 Python 路徑。

### 13.3 三個欄位如何串起 §5 的訊息契約

`Subtitle.src_lang` / `tgt_lang` / `pack_id` 讓每一則字幕自我描述是哪個包產生的。
運作方式：

1. `inference/langpack.py` 在啟動與熱重載時建立 registry：`{pack_id: LanguagePack}`
2. 使用者在 UI 啟用一或多個包（見 §13.4）
3. ASR 完成語言偵測後，`pipeline.py` 用 `src_lang`（+ 使用者鎖定的 `tgt_lang`，
   若同一來源語言有多個包可選）查 registry 挑對應包
4. 找不到匹配包 → 顯示原文並標記「未翻譯」，而不是靜默丟棄或用錯的包硬翻
5. 找到的包決定接下來每一步：stabilizer 粒度、要不要先翻暫定稿、
   翻譯引擎與語言代碼、術語表、換行規則

**新增一個語言配對，不需要碰 `audio/`、`inference/pipeline.py` 或 `ui/` 任何一行**——
只要一份 `pack.yaml`。這是整個機制唯一的存在理由，也是驗收標準（見 ROADMAP M6）。

### 13.4 選擇與匯入：使用者體驗

- **選擇**：`ui/panel/langpack_manager.py` 列出所有已驗證的包（內建 + 已匯入），
  可複選啟用（例如同時開「日→繁中」與「英→繁中」，看混合語言的直播來源自動路由）。
  也可以只鎖定一個包，關閉自動偵測、強制所有語音都當作該語言處理。
- **匯入**：「匯入語言包」對話框接受資料夾或 `.langpack.zip`，流程固定為
  **驗證 → 複製進使用者本機目錄 → 出現在清單**，全程不執行任何包內代碼、
  不需要重啟主程式（`langpack.py` 支援熱重載）。
- **版本衝突**：`schema_version` 高於目前程式支援版本時，匯入被拒並給出
  明確錯誤（而不是嘗試硬解析導致行為未定義）。

### 13.5 內建語言包彙整

首批隨程式附上四個包，彼此獨立、互不依賴：

| pack_id | 方向 | Whisper 辨識品質 | 分詞 | 比對粒度 | 暫定稿即時翻譯 | 主要風險 |
|---|---|---|---|---|---|---|
| `en-zhHant` | 英 → 繁中 | 最佳 | 空格 | 詞 | ✅ | — |
| `ja-zhHant` | 日 → 繁中 | 佳 | 無空格 | 字元 | ❌（SOV，等定稿） | 靜音幻覺、敬語層級 |
| `th-zhHant` | 泰 → 繁中 | 中等，聲調符號易漏 | 無空格 | 字元 | ✅ | 辨識率、換行斷點 |
| `zhHant-en` | 繁中 → 英 | 佳 | — | 詞 | ✅ | 中文口語省略主詞，MT 較弱 |

泰文若辨識率不足，備案是換一個泰文專用 ASR 模型——只要改 `th-zhHant/pack.yaml`
裡的 `asr.engine`/`asr.model`，`ASREngine` 抽象已經留好 per-pack 路由的插槽，
不需要動核心程式碼。`zhHant-en` 反向包用於自己開直播 / 會議時對外掛英文字幕，
與正向三包共用完全相同的機制，沒有任何特殊處理路徑。

---

## 14. 設定與 Profile

**Profile 與語言包是兩個正交的軸**，不要混在一起：

- **語言包**（§13）回答「這個語言配對怎麼處理」——ASR 選型、翻譯方向、比對粒度。
- **Profile** 回答「這個使用情境要多即時、多精準」——延遲/品質的取捨、UI 行為。

同一個語言包可以搭配不同 profile；profile 只是引用一個或多個預設啟用的
`langpack_ids`，不重複定義任何語言相關欄位：

- `anime.yaml` —— 預設啟用 `ja-zhHant`、更新頻率拉高、字體偏大
- `meeting.yaml` —— 預設啟用 `en-zhHant`、暫定稿優先、自動存逐字稿
- `lecture.yaml` —— 精準優先、允許較長延遲、開啟雲端精修（語言包依素材手動選）
- `streaming-out.yaml` —— 預設啟用 `zhHant-en`，給自己開直播時對外掛字幕用

---

## 15. 可觀測性

沒有數字就無法調延遲。`utils/metrics.py` 至少要有：

- 各階段延遲**直方圖**（p50 / p95 / p99），不是平均值
- 環形緩衝水位、覆蓋（丟幀）次數
- ASR RTF（real-time factor），> 1.0 就是跟不上
- 降級階梯當前級別與升降次數
- 幻覺過濾命中次數
- 雲端精修成功率與延遲

控制台面板直接顯示這些 —— 這是自己用的工具，可觀測性就是使用者功能。

---

## 16. 風險與已知邊界

| 風險 | 影響 | 對策 |
|------|------|------|
| **Process Loopback 無 Python 綁定** | Tier 2 需自寫 COM 呼叫，是最大未知數 | 先做 Tier 1 打通全管線，Tier 2 排在 M4 且有 Tier 1/3 後備 |
| **DRM 內容音訊路徑** | Netflix / Disney+ 等可能走受保護音訊路徑，loopback 取到靜音 | M1 就實測各平台並記錄相容性表；不繞過保護機制，取不到就誠實提示使用者 |
| **Whisper 幻覺** | 靜音 / 音樂段產生假字幕，日文最嚴重 | VAD 前置閘門 + `no_speech_prob` 門檻 + 已知幻覺片語黑名單 |
| **CUDA / cuDNN 版本相依** | CTranslate2 需特定 CUDA 12 + cuDNN 9 | M0 就驗證並把版本釘死在 `requirements.txt`，寫進安裝文件 |
| **12GB VRAM 同時載 ASR + MT** | large-v3 int8 約 1.5GB + NLLB-600M 約 0.8GB，尚有餘裕；但若日後換大型 LLM 會爆 | VRAM 預算納入 metrics，超限時降級階梯 L4 介入 |
| **泰文辨識率** | 可能達不到堪用水準 | M1 就用真實素材量測，不堪用則走 §13.5 備案 |
| **雲端精修的隱私** | 音訊轉出的文字會送出第三方 | 預設關閉，需使用者明確開啟；提供 per-profile 開關 |
| **背景音樂 / 多人重疊語音** | ASR 品質劣化 | 明確列為不支援情境，不做語者分離（超出範圍） |
| **匯入語言包的資料完整性** | 格式錯誤或惡意欄位（如超長字串、glossary 注入奇怪字元）造成崩潰或渲染異常 | 語言包是**純資料**、無可執行程式碼；`engine` 類欄位限定白名單；匯入時先驗證 schema 再載入，驗證失敗直接拒絕並顯示原因 |

---

## 17. 不做什麼（範圍界線）

明確排除，避免範圍蔓延：

- ❌ 語者分離（diarization）
- ❌ 麥克風輸入 / 雙向對話翻譯
- ❌ 語音合成（TTS 配音）
- ❌ 影片檔離線批次處理（本工具只做即時串流）
- ❌ 跨平台（macOS / Linux）—— `CaptureBackend` 抽象有留門，但不在計畫內
