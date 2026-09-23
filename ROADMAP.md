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

## M1 — 垂直切片：聽得到，印得出

**目標**：三個進程真的跑起來，字幕印在終端機上。沒有 UI、沒有翻譯。

- `audio/capture/wasapi_loopback.py`（Tier 1）
- `audio/ringbuffer.py` — shared_memory SPSC 環形緩衝，**寫入端永不阻塞**
- `audio/resample.py`、`audio/vad.py`、`audio/segmenter.py`
- `inference/asr/faster_whisper_engine.py` + `stabilizer.py`（LocalAgreement-2）
- `inference/asr/hallucination.py` — 幻覺片語黑名單 + `no_speech_prob` 門檻
- `scripts/bench_latency.py` — 量到**每一格**，不只端到端

**驗收標準**

- [ ] 播放 10 分鐘英文影片，終端機持續輸出暫定稿與定稿，**零崩潰**
- [ ] 暫定稿 p95 延遲 < 1.2s，定稿 p95（句末後）< 1.0s
- [ ] 手動 kill inference 進程，supervisor 自動重啟，音訊不中斷，字幕在 5s 內恢復
- [ ] 全靜音 5 分鐘 → **零字幕輸出**（幻覺過濾生效）
- [ ] 日文、泰文素材各跑一次，人工評估辨識可用性並記錄

> 這一里程碑結束時，這個專案的技術可行性已完全確定。後面都是工程量，不是風險。

---

## M2 — Overlay：看得到

**目標**：字幕離開終端機，變成螢幕上的浮層。

- `ui/overlay/window.py` — 無邊框 / 置頂 / 點擊穿透 / 不搶焦點 / 不進 Alt-Tab
- `ui/overlay/renderer.py` — 雙態渲染（暫定灰、定稿白）、描邊、半透明底條
- `ui/overlay/layout.py` — 多螢幕與混合 DPI 定位
- `ui/hotkeys.py` — 全域熱鍵：顯示/隱藏、編輯模式、暫停
- `gateway/server.py` + `gateway/session.py` — 字幕時間軸單一真相
- 系統匣圖示與最小控制選單

**驗收標準**

- [ ] 影片全螢幕（無邊框視窗模式）時字幕正常顯示於上層
- [ ] 滑鼠可正常點擊字幕底下的播放器控制列（穿透生效）
- [ ] 主副螢幕 DPI 不同時拖曳過去，字體大小與位置正確
- [ ] 關閉 overlay 再開啟，上游擷取與推論**完全不受影響**（可從 log 驗證）
- [ ] 暫定稿改寫時是**就地取代**，字幕不會往下長

---

## M3 — 翻譯層與語言包機制：看得懂、可切換

**目標**：出現繁體中文，且語言方向從此是資料而非程式碼。

- `inference/langpack.py` — 語言包 registry：掃描 `config/langpacks/`、驗證 schema、
  熱重載；`stabilizer.py` 的比對粒度、`pipeline.py` 的 draft_translate 開關
  改為讀語言包欄位，砍掉 M1/M2 期間可能存在的任何 per-language if/else
- `config/langpacks/{en-zhHant,ja-zhHant,th-zhHant,zhHant-en}/pack.yaml` — 四個內建包
- `inference/translate/ct2_nllb.py` — NLLB-200-distilled-600M 轉 CT2 int8，
  語言代碼從語言包的 `translate.src_code`/`tgt_code` 讀取
- `inference/translate/context.py` — 前 5 句上下文視窗
- `inference/translate/glossary.py` — 術語表載入（路徑來自語言包）
- `inference/pipeline.py` — 兩段式編排接上翻譯 + 語言包路由
- `ui/panel/langpack_manager.py`（最小版）— 下拉選單切換啟用的語言包，
  完整匯入 UI 留到 M6
- `scripts/setup_models.py` — 一鍵下載並轉換模型

**驗收標準**

- [ ] 英文 / 日文 / 泰文素材各 10 分鐘，全程有中文字幕；`zhHant-en` 包用中文素材測試出英文字幕
- [ ] 加上術語表後，指定人名在整段影片中**譯法一致**
- [ ] 有上下文 vs 無上下文各跑一次同素材，人工對比並記錄差異（驗證上下文真的有用）
- [ ] 本地 MT p95 < 150ms，未把總延遲推出預算
- [ ] UI 下拉切換語言包後，下一句字幕立即套用新設定，**不需重啟任何進程**

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
