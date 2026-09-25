# System Audio Subtitle — 實作路線圖

搭配 [ARCHITECTURE.md](ARCHITECTURE.md) 閱讀。

## 排序原則

1. **風險最高的先驗證，但不先完工。** 兩個最大未知數（延遲是否可行、行程級擷取
   能否從 Python 呼叫）在 M0 就用小規模 spike 戳破，而不是等到做完 UI 才發現不行。
2. **先垂直切片，再水平加厚。** M1 要的是一條「有聲音進去、有字出來」的完整細線，
   醜沒關係。三個進程各做一半，好過一個進程做到完美。
3. **契約先行。** `contracts/` 定稿前不寫任何服務邏輯，否則三邊會各自長出私有假設。

---

## M0 — 地基與風險探測

**目標**：把不確定性壓縮成已知數字，並釘死環境。

| 項目 | 內容 |
|------|------|
| 環境 | Python 3.13 venv、CUDA 12 + cuDNN 9、`requirements.txt` 版本全釘死 |
| 契約 | `contracts/messages.py`、`topics.py`、`enums.py`、`langpack_schema.py` 定稿 |
| 匯流排 | `runtime/bus.py`（ZeroMQ PUB/SUB + REQ/REP）、`runtime/clock.py` |
| 骨架 | `runtime/supervisor.py` 能拉起 / 監看 / 重啟三個空進程 |

### Spike A — 延遲可行性（半天）✅ 已完成

不接任何管線，直接量：讀一個 1 秒 wav，`faster-whisper large-v3` int8_float16
在 4070 上 `beam=1` 與 `beam=5` 各跑 50 次。

> **驗收**：beam=1 的 p95 < 250ms。若不達標，當下就要改選 `distil-large-v3`
> 或下修暫定稿頻率，而不是等到 M5 才發現延遲預算是空中樓閣。

**結果**：暫定稿 beam=1 p95 229ms（達標）；定稿 beam=5 典型長度 p95 352ms（達標）。
RTF 只有 0.04-0.05，GPU 完全不是瓶頸。詳細數字與踩到的兩個 Windows DLL/symlink
坑見 ARCHITECTURE.md §10。`scripts/bench_latency.py` 已可重跑。

### Spike B — Process Loopback 可行性（1～2 天）★ 最高風險 ✅ 已完成

用 `ctypes` 呼叫 `ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_PARAMS`，
目標只有一個：**對 chrome.exe 下 INCLUDE_TARGET_PROCESS_TREE，能不能拿到非靜音的 PCM。**

> **驗收**：能存出一個聽得到瀏覽器聲音、且**聽不到同時播放的其他 App 聲音**的 wav。
> 失敗的話，Tier 2 整段砍掉，M4 改為「引導使用者用 VB-CABLE」，架構不需要改
> —— 這正是 `CaptureBackend` 抽象存在的理由。

**結果：可行，且已用真實音訊驗證隔離性。** `scripts/spike_b_process_loopback.py`
完整跑通「呼叫 ActivateAudioInterfaceAsync → IAudioClient → IAudioCaptureClient
輪詢」全鏈路。測試方式：兩個獨立 PowerShell 行程同時播放不同音訊（語音 /
440Hz 純音），分別鎖定兩個 PID 擷取，結果乾淨對應各自的音源、互不污染
（數字見 ARCHITECTURE.md §6）。

過程中踩了三個坑，都已修好並寫進 §6：(1) 必須用 MTA、不能用 STA，否則
`ActivateAudioInterfaceAsync` 同步呼叫就直接失敗；(2) 完成回呼物件必須
實作 `IAgileObject`，否則背景執行緒呼叫回來時失敗，且錯誤訊息完全看不出
是 marshaling 問題；(3) comtypes 對 HRESULT 方法的 `[out]` 參數要當回傳值
接，不能用 C 風格 `byref` 硬填。這三點排查花了本次 Spike 大部分時間，
記錄下來讓 M4 正式實作時直接照做，不用重踩。

**尚未驗證、留給 M4**：瀏覽器分頁（多行程樹）場景、目標行程結束後的
串流重建、`AvSetMmThreadCharacteristics` 執行緒優先權調整對抖動的實際影響。

### Spike C — DRM 相容性表（半天）

用 Tier 1 端點 loopback 實測並記錄：YouTube / Netflix / Disney+ / Prime Video /
Crunchyroll / 本地 mpv，在 Chrome 與 Edge 下各自能否取到音訊。

> **驗收**：產出一張相容性表格，寫進 README。**不嘗試繞過任何保護機制**；
> 取不到就是取不到，如實列出。

---

## M1 — 垂直切片：聽得到，印得出 ✅ 核心已完成（兩項待補，見下）

**目標**：三個進程真的跑起來，字幕印在終端機上。沒有 UI、沒有翻譯。

- [x] `audio/capture/base.py` + `wasapi_loopback.py`（Tier 1）
- [x] `audio/ringbuffer.py` — shared_memory SPSC 環形緩衝，**寫入端永不阻塞**
      （另外補了 `peek()`：隨機讀取絕對樣本範圍、不影響 `read()` 的循序游標，
      暫定稿要重複重解碼同一段成長中的音訊，用循序 `read()` 語意不合）
- [x] `audio/resample.py` — 串流重採樣，用「輸入端保留歷史脈絡」處理分批
      呼叫的邊界爆音問題
- [x] `audio/vad.py` — **不依賴 torch**，純 onnxruntime + numpy 直接呼叫
      Silero VAD 的 onnx 模型（省掉 torch/torchaudio 這兩個重依賴，也
      避開它們跟釘死的 torch==2.14.0 版本衝突，見下方踩坑記錄）
- [x] `audio/segmenter.py` — VAD 狀態機，10 個單元測試涵蓋強制斷句、
      邊界回推、短暫掉幀不誤判等情境
- [x] `inference/asr/base.py` + `faster_whisper_engine.py`
- [x] `inference/stabilizer.py`（LocalAgreement-2，含 word/char 兩種粒度）
- [x] `inference/asr/hallucination.py` — `no_speech_prob` 門檻 + 黑名單
- [x] `audio/service.py` + `inference/service.py` — 兩個真正的 PROCESS 主迴圈
- [x] `scripts/setup_models.py` — 只取 VAD 的 onnx 模型檔，不裝 silero-vad
      整個套件（它的 torch 依賴會把釘死版本降版，見下）

**驗收標準**

- [x] **端到端即時驗證**：真的播放測試語音、真的用 WASAPI loopback 擷取、
      真的跑 GPU ASR，終端機即時印出 OPEN → 逐步成長的 DRAFT（`stable_chars`
      隨著證據增加而變長，LocalAgreement-2 行為符合設計）→ CLOSE [FINAL]，
      文字跟原始語句幾乎逐字相符
- [x] **手動 kill inference 進程，supervisor 自動重啟，音訊不中斷**：實測
      0.8 秒偵測到新進程（遠優於 5 秒目標），`audio-service` 全程存活、
      完全不受影響。ASR 模型重新載入另外要 4-8 秒，這段時間發生的
      utterance 事件會被 ZMQ PUB/SUB 靜默遺失（音訊仍在 ring buffer 裡，
      只是沒人記得段落邊界在哪）——這符合 ARCHITECTURE.md 原本「使用者
      只看到字幕停頓幾秒」的容忍設計，不是缺陷，但值得記錄成已知行為
- [x] **幻覺過濾**：純靜音音訊送進 Whisper，真的產生了經典幻覈「Thank
      you.」（`no_speech_prob=0.858`），`is_hallucination()` 正確濾掉。
      這是 ARCHITECTURE.md §7 描述的現象第一次在這個專案裡被真實重現
- [x] 全部 93 個自動化測試通過（含真實 GPU 推論、真實 WASAPI 擷取、真實
      跨進程 supervisor 重啟）
- [ ] **10 分鐘、零崩潰的長時間跑法**：還沒做滿 10 分鐘的連續測試，
      已驗證的是短時間（數十秒級）端到端正確性 + 崩潰恢復。長時間穩定性
      （記憶體洩漏、ring buffer 長時間 wraparound、ASR 累積延遲）需要更長
      的實測，建議用真實影片素材補測
- [x] **泰文素材人工評估：可用**。用兩份獨立真實素材各跑過一次完整管線
      （resample → VAD → segmenter → ASR，非合成音檔）：
        1. 使用者自己的螢幕錄影（oCam，11.6s，直播/遊戲情境對話）——
           Whisper 自動語言偵測正確判定為泰文，VAD 切出 9.2s 主要語句，
           `no_speech_prob=0.06`（高信心真語音），文字連貫、無亂碼
        2. 使用者提供的 99 秒 YouTube 影片——VAD 切出 23 句完整語音
           （0.35–4.32s），其中 6 句過短片段（<0.7s）被幻覺過濾正確攔下
           （`no_speech_prob` 0.52–0.87），通過過濾的 17 句 `no_speech_prob`
           多數落在 0.002–0.42，字數與音訊長度成合理比例，沒有 Whisper
           常見的重複跳針現象
      兩次獨立素材結論一致：分段合理、幻覺過濾確實攔住該攔的、通過過濾
      的內容信心度高。**額外發現**：暫定稿（DRAFT）逐步解碼時，Whisper
      有時會改寫句子開頭的用字（不只在句尾延伸），導致字元級 LocalAgreement-2
      的穩定前綴長度忽高忽低，比英文更容易「跳字」——狀態機本身邏輯沒問題
      （最終都收斂到正確答案），但這是泰文使用者體感上值得之後調校的點
      （例如可考慮從句尾往回比對穩定度，而非嚴格要求前綴完全比對）
- [ ] **日文素材人工評估**：這台開發機沒有安裝日文的 Windows 語音合成語音
      （`Get-WinUserLanguageList` 只有 `zh-Hant-TW`/`en-US`），沒辦法在不裝
      額外語言套件的情況下生成測試音檔。需要使用者提供真實日文素材才能補測

**意外發現（值得記錄）**：WASAPI 端點 loopback（Tier 1）在沒有播放任何
測試音訊時，偶爾擷取到系統背景音效並正確轉錄出文字（例如 Windows 內建
語音助理的通知音）——這不是 bug，是 ARCHITECTURE.md §6 一開始就指出的
Tier 1 已知限制（「會混進 Discord 語音、通知音效」），現在有了第一手的
實機證據，也讓 M4 的 Tier 2（行程級擷取）訴求更具體：不是理論上的擔憂，
是真的會發生。

> 兩項待補（10 分鐘長跑、日文/泰文人工評估）都需要更長時間或使用者提供
> 素材，不是技術可行性的疑慮——垂直切片本身、崩潰恢復、幻覺過濾都已經
> 用真實 GPU 推論、真實音訊驗證過。後面大部分是工程量，不是風險。

---

## M2 — Overlay：看得到 ✅ 核心已完成（多螢幕切換待補，見下）

**目標**：字幕離開終端機，變成螢幕上的浮層。

- [x] `ui/overlay/window.py` — 無邊框 / 置頂 / 點擊穿透 / 不搶焦點 / 不進 Alt-Tab
- [x] `ui/overlay/renderer.py` — 雙態渲染（暫定灰、定稿白）、描邊、半透明底條
- [x] `ui/overlay/layout.py` — 多螢幕與混合 DPI 定位
- [x] `ui/hotkeys.py` — 全域熱鍵：顯示/隱藏、編輯模式（`RegisterHotKey` + native event filter）
- [x] `gateway/server.py` + `gateway/session.py` — 字幕時間軸單一真相
- [x] 系統匣圖示與最小控制選單（`ui/app.py`）

**驗收標準**

- [x] 滑鼠可正常點擊字幕底下的播放器控制列（穿透生效）——**用原生
      Win32 樣式旗標查詢驗證，不是肉眼看**（見下方踩坑記錄，這件事
      肉眼幾乎看不出來對不對）
- [x] 關閉 overlay 再開啟，上游擷取與推論**完全不受影響**——gateway
      是獨立行程，UI 斷線重連走 WebSocket 重試，不影響 audio/inference
- [x] 暫定稿改寫時是**就地取代**：`Session.apply()` 依 `utt_id` 覆寫、
      `SubtitleRenderer.set_state()` 直接重繪同一塊區域，8 個 session 測試
      + 7 個 renderer 測試涵蓋
- [ ] **影片全螢幕時字幕正常顯示於上層**：這台機器沒有現成可長時間播放
      的全螢幕影片場景可以反覆測試，浮層本身的置頂/穿透/無邊框機制已經
      用原生 API 驗證過，但「跟真正的播放器疊在一起」這個組合情境還沒
      實測，需要使用者自己在看影片時開著浮層跑一段時間確認
- [ ] **主副螢幕 DPI 不同時拖曳過去**：這台開發機只有單一螢幕
      （2560×1440 @ 150% DPI），`ui/overlay/layout.py` 的 per-monitor DPI
      邏輯已經在單螢幕情境下驗證正確（見下方 DPI 踩坑記錄），但「拖到
      DPI 設定不同的另一個螢幕」這個轉換情境沒辦法在這台機器上測，
      需要使用者有多螢幕環境時協助驗證

**踩到兩個坑，都已修好並補了迴歸測試**：

1. **`QWidget.screenChanged` 不存在**——那是 `QWindow` 的 signal，`QWidget`
   要先 `show()`、有底層原生視窗（`windowHandle()`）之後才能接到。
   `ScreenTracker.start()` 因此必須在 `window.show()` 之後才呼叫，
   `__init__` 裡不能提前接（見 `ui/overlay/layout.py`）。
2. **`Qt.WA_TransparentForMouseEvents` 不會自動轉成原生
   `WS_EX_TRANSPARENT`**——這是這次 M2 最有價值的發現。只設這個 Qt
   屬性，介面看起來完全正常（視窗確實半透明、確實置頂），但滑鼠事件
   實際上**完全沒有**穿透到底下其他應用程式的視窗，肉眼從畫面完全看不
   出來，只有真的去點擊底下的東西才會發現點不穿。修法是在 `showEvent`
   裡直接用 `ctypes` 呼叫 `SetWindowLongW` 設定原生 `WS_EX_TRANSPARENT`
   位元（見 `ui/overlay/window.py` 的 `_set_native_click_through()`）。
   `tests/test_overlay_window.py` 直接查詢原生視窗樣式位元做迴歸測試，
   不能被「表面上看起來對」騙過去。

**另外還修了一個 renderer 的裁切 bug**：用 `QWidget.grab()` 離線渲染成
圖片檢查排版時（不截真實螢幕——全螢幕截圖會把使用者當下畫面上的所有
內容都拍進去，包括完全無關、可能很私密的東西，這件事本身也是這次的
教訓，全螢幕截圖不該是預設的驗證手段），發現字幕行數多、框比視窗本身
還高時，最上面一行會被裁到視窗外面去，已修正（`box_y` 加下限）。

---

## M3 — 翻譯層與語言包機制：看得懂、可切換 ✅ 已完成（10 分鐘長跑測試待補）

**目標**：出現繁體中文，且語言方向從此是資料而非程式碼。

- [x] `inference/langpack.py` — 語言包 registry：掃描 `config/langpacks/`、
      驗證 schema、熱重載；`Pipeline` 依語言包的 `stabilizer.granularity`/
      `policy.draft_translate` 決定行為，沒有任何寫死的 per-language 判斷
- [x] `config/langpacks/{en-zhHant,ja-zhHant,th-zhHant,zhHant-en}/pack.yaml`
      — 四個內建包（M0 就寫好了，M3 是第一次真的被讀取使用）
- [x] `inference/translate/ct2_nllb.py` — NLLB-200-distilled-600M 轉 CT2 int8，
      語言代碼從語言包的 `translate.src_code`/`tgt_code` 讀取
- [x] `inference/translate/context.py` — 前 5 句上下文視窗（用「前文原文
      + 這句原文一起送進 NLLB、按句尾標點切回這句的翻譯」這個 heuristic，
      見檔案內的詳細說明與限制）
- [x] `inference/translate/glossary.py` — 術語表載入 + 佔位符替換
- [x] `inference/pipeline.py` — 兩段式編排接上翻譯 + 語言包路由
- [x] `scripts/setup_models.py` — 一鍵下載並轉換 NLLB 模型
- [x] UI 語言包熱切換的**控制通道**（`SetActiveLangPacks` REQ/REP，
      `CONTROL_LANGPACK_RELOAD`）——`ui/panel/langpack_manager.py` 本身
      （下拉選單 UI 元件）還沒做，見下方待補項

**踩到的坑**：`transformers` 這個版本的 `__init__` 內部無條件
`import torch`（不是延遲載入），NLLB 的 tokenizer 沒辦法只用
`transformers.AutoTokenizer` 又完全避開 torch。這跟 M1 刻意讓
audio-service 避開 torch 是不同層級的考量——inference-service 本來就要
載入 GPU 上的 ASR 模型、啟動要好幾秒，多一個 torch import 的一次性開銷
不影響翻譯熱路徑本身（tokenize/translate/detokenize 完全不會用到 torch
的運算，那是 ctranslate2 做的）。已經把 `requirements.txt` 的註解更新為
「inference-service 執行期真的需要 transformers/torch」，不再是只有
`scripts/setup_models.py` 用得到。

**驗收標準**

- [x] **本地 MT 延遲**：p50=56.5ms、max=86.4ms（10 次英→繁中），**遠低於
      150ms 目標**，模型載入本身約 2.3 秒（一次性開銷，不計入單句延遲）
- [x] **上下文有沒有用：有，且有具體可證的案例**。無上下文時
      "It is red and very fast." 翻成「紅色,而且非常快.」（主詞整個消失）；
      加上前一句「My sister bought a new car yesterday.」當上下文後，
      翻成「它是紅色的,而且很快.」（正確補回代名詞「它」指代車子）。
      這是機制設計時就預期的效果，這次用真實案例驗證了
- [x] **端到端真實驗證**：完整四進程（audio→inference→gateway→ui）+ 真實
      GPU 推論跑通，播放英文測試語音，DRAFT 逐步翻譯改善、FINAL
      定稿「我認為我們應該在決定明年預算之前考慮季度結果」跟原文語意
      吻合，log 全程可見
- [x] **控制通道端到端驗證**：`SetActiveLangPacks` 成功切到單包鎖定模式、
      正確拒絕不存在的 pack id（且沒有破壞原本啟用狀態，回傳目前實際
      狀態而非假裝成功）、切回多包皆正常
- [ ] **英文 / 日文 / 泰文素材各 10 分鐘，全程有中文字幕**：三個語言的
      翻譯本身都已經個別驗證過會動（含真實英文語音的完整端到端），但
      沒有做滿 10 分鐘的連續測試
- [ ] **術語表在整段影片中譯法一致**：佔位符替換機制本身有單元測試
      覆蓋（含「長詞優先比對」「佔位符沒被模型動過」等情境），但沒有
      用真實長影片素材做端到端驗證
- [x] **UI 下拉選單**：`ui/panel/langpack_manager.py`（最小版）已完成——
      系統匣選單「語言包設定...」開啟一個複選勾選框對話框，列出全部
      內建語言包，按下 Apply 真的送出 `SetActiveLangPacks` 控制請求。
      用真實跑起來的四進程 + 真正的 Qt Dialog 元件端到端驗證過：勾選
      只留 `ja-zhHant`、按 Apply、用獨立探測確認 inference-service
      真的切換成只啟用該包——不是繞過 UI 直接呼叫腳本測的。已知限制：
      控制通道目前只有「設定」沒有「查詢」，對話框開啟時沒辦法準確
      反映目前實際狀態，預設全選（多數情況下這剛好符合
      inference-service 自己的預設行為）；要做到精確狀態同步，
      完整版（M6）再補查詢端點

**意外發現（值得記錄）**：NLLB-200-distilled-600M（本地即時翻譯用的小
模型）對極短問候語有已知弱點——「สวัสดีครับ」（泰文「你好」）翻出
「您的位置:首頁」這種完全無關的網頁導覽列幻覺文字，但同一次測試裡
其他 3 句泰文（感謝、天氣、自我介紹）都翻得正確流暢。這是蒸餾小模型
在極短輸入上的已知限制，不是我們程式碼的 bug，剛好也呼應了上下文機制
的價值——短句缺乏語境更容易被誤譯。

---

## M4 — 行程級擷取：只聽媒體聲音 ★

**目標**：兌現「專門針對媒體聲音」這個核心訴求。前提是 Spike B 通過。

- `audio/capture/process_loopback.py` — 正式實作（含 PID 存活監看與重建）
- `audio/capture/enumerate.py` — 列出正在發聲的行程供 UI 選取
- `audio/capture/virtual_cable.py` — Tier 3 後備與引導
- UI 音源選擇器：端點 / 行程 / 虛擬裝置三選一，可即時切換

**驗收標準**

- [ ] 同時播放 YouTube 與 Discord 語音，字幕**只出現 YouTube 的內容**
- [ ] 播放中關閉目標瀏覽器再重開，擷取自動重建，無需手動介入
- [ ] Tier 2 不可用的機器（舊版 Windows）自動退回 Tier 1，並在 UI 明確告知

---

## M5 — 雲端精修、降級與可觀測性

**目標**：品質上限拉高，並確保跟不上時優雅降級而非崩潰。

- `inference/translate/llm_api.py` — 3~5 句批次重譯，POLISHED 靜默替換
- 斷網 / 額度用盡 → 自動退回本地，功能不中斷
- `inference/degrade.py` — L0~L5 降級階梯 + 遲滯
- `utils/metrics.py` — 分階段延遲直方圖、RTF、緩衝水位、丟幀數
- 控制台面板顯示即時 metrics

**驗收標準**

- [ ] 人工製造負載（同時跑 GPU 壓力測試），系統逐級降級而非丟幀，log 完整記錄
- [ ] 拔網路線，精修自動停用，本地字幕不中斷
- [ ] 精修預設**關閉**，開啟時 UI 明確提示內容會送往第三方
- [ ] 精修後字幕替換是靜默的，不造成視覺跳動

---

## M6 — 打磨與封裝

- `gateway/export.py` — SRT / VTT / 純文字匯出（用 `AudioSpan` 樣本數算時間軸）
- 字幕整體偏移微調（±ms 熱鍵）
- `config/profiles/` — anime / meeting / lecture / streaming-out 四套 preset
- `ui/panel/langpack_manager.py`（完整版）— 匯入對話框（資料夾 / `.langpack.zip`）、
  驗證錯誤訊息、多包複選啟用、單包鎖定模式
- 歷史面板：逐句回看、複製、搜尋
- 首次啟動精靈：偵測 GPU、下載模型、測試音源、選擇初始語言包
- PyInstaller 打包 + 設定檔外置

**驗收標準**

- [ ] 在一台乾淨的 Windows 機器上照 README 安裝，30 分鐘內可用
- [ ] 匯出的 SRT 用播放器載入，時間軸與原影片對得上
- [ ] **零程式碼新增語言**：手寫一個全新語言包（例如韓文 → 繁中，僅 `pack.yaml`
      + 選用 glossary），透過 UI 匯入後可直接選用、產生字幕 —— 不改 `audio/`、
      `inference/`、`ui/` 任何一行程式碼。這是驗證 §13 抽象是否成立的關鍵測試
- [ ] 匯入格式錯誤的語言包（如缺必要欄位）時，UI 顯示明確錯誤而非崩潰

---

## 第一週具體要做的事

1. 建 venv、釘死 `requirements.txt`、確認 `import ctranslate2` 能看到 CUDA
2. 寫 `contracts/` 四個檔案並定稿（含 `langpack_schema.py`）
3. **Spike A**（延遲數字）
4. **Spike B**（行程級擷取可行性）← 最該優先戳破的未知數
5. 用 Spike 的結論回頭修正 ARCHITECTURE.md 的延遲預算表與 §6

> Spike B 的結果會決定這個工具是「又一個系統音字幕工具」還是「專門翻媒體聲音的工具」。
> 這是整個專案最值得先花時間的地方。
