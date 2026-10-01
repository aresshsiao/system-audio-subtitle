# System Audio Subtitle — 使用說明

> 目前進度：M0（環境/地基）+ M1（擷取→ASR 垂直細線）+ M2（浮層 UI）已完成。
> **還沒有翻譯**——目前浮層顯示的是原文（英/日/泰皆可，看語音內容自動偵測），
> 中文翻譯要等 M3。完整技術細節見 [ARCHITECTURE.md](ARCHITECTURE.md)，
> 各里程碑驗收狀況見 [ROADMAP.md](ROADMAP.md)。

---

## 1. 環境需求

- Windows 10/11
- NVIDIA GPU（實測環境：RTX 4070 12GB，`int8_float16` 跑 large-v3）
- Python 3.13

## 2. 安裝

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python scripts\probe_gpu.py      # 確認 GPU/cuDNN 抓得到
.venv\Scripts\python scripts\setup_models.py   # 下載 VAD 模型
```

`faster-whisper` 的 ASR 模型（large-v3）會在 `inference-service` 第一次啟動時
自動從 Hugging Face 下載並快取，不需要另外手動處理。

## 3. 啟動

**一鍵啟動**（推薦）：

```powershell
.venv\Scripts\python -m launcher
```

會拉起 audio-service / inference-service / gateway（任何一個崩潰會自動重啟），並開啟
UI；關閉 UI（系統匣 →「結束」）就一起收掉。第一次啟動會自動跳出「環境檢查」，
告訴你 GPU、模型、音訊裝置是否就緒與怎麼修。

**手動分開啟動**（除錯用，四個進程各開一個終端機視窗）：

```powershell
# 1. audio-service — 擷取系統輸出音訊、VAD 切句
.venv\Scripts\python -m audio.service

# 2. inference-service — ASR 轉錄（第一次啟動要等模型載入，約 4~8 秒）
.venv\Scripts\python -m inference.service

# 3. gateway — 字幕廣播
.venv\Scripts\python -m gateway.server

# 4. ui — 浮層 + 系統匣
.venv\Scripts\python -m ui.app
```

啟動順序不是硬性規定（`inference-service` 會自動重試連線直到
`audio-service` 準備好；`ui.app` 也會自動重試連線 `gateway`），
但照這個順序啟動最省事，不用等著看誰先誰後。

啟動後播放任何有聲音的影片/直播，浮層應該會在螢幕下方約 1/4 處顯示字幕。

### 選擇音源（不需要重啟）

系統匣圖示右鍵 →「音源選擇...」，三種來源：

- **單一應用程式**（推薦）：列出目前有視窗的行程，只翻譯選定行程的聲音，
  Discord 語音、通知音效不會混進來。瀏覽器請保持勾選「包含子行程」
- **整個輸出裝置**：某個喇叭/耳機的所有聲音混在一起
- **虛擬音效裝置**：偵測到 VB-CABLE 才能選，行程級擷取用不了的機器的後備

按「套用」立即切換；切換時舊音源說到一半的句子會先收尾。清單是開啟面板當下
掃描的，先開影片再開面板，或按「重新整理清單」。開發/測試也可以用環境變數指定
啟動時的初始來源：`SAS_CAPTURE_TARGET=process:<pid>` 或 `endpoint:<裝置索引>`。

## 4. 操作

| 操作 | 方式 |
|---|---|
| 顯示 / 隱藏字幕 | 熱鍵 `Ctrl+Alt+S`，或系統匣圖示右鍵選單 |
| 拖曳字幕到想要的位置 | 熱鍵 `Ctrl+Alt+E` 進入編輯模式（此時會暫時關閉點擊穿透），拖曳後再按一次退出 |
| 匯出字幕的偏移 ±100ms | 熱鍵 `Ctrl+Alt+,` / `Ctrl+Alt+.`（見下方「匯出字幕」） |
| 音源、語言包、情境、雲端精修、效能監控、字幕歷史、環境檢查 | 系統匣圖示右鍵選單 |
| 結束程式 | 系統匣圖示右鍵 →「結束」 |

**平常（非編輯模式）滑鼠點擊會直接穿透浮層**，可以正常操作底下播放器的控制列，
不會被字幕擋住。

### 匯出字幕（SRT / VTT / 純文字）

系統匣 →「字幕歷史與匯出...」：逐句回看、搜尋、複製，並匯出成 SRT / VTT / TXT
（可選雙語、偏移）。匯出的是**定稿 / 精修後**的字幕，暫定稿不會進檔案。

**時間軸的原點是「開始擷取」的那一刻，不是影片開頭。** 想做成可對應影片的字幕檔：
擷取開始後再從頭播放影片，然後勾「第一句從 0 開始」，或用偏移熱鍵 / 數字欄微調。
（實測：已知播放時刻的音檔，匯出的 SRT 與實際播放時間誤差約 0.2 秒。）

### 情境 Profile

系統匣 →「情境 Profile」：一鍵套用「動畫 / 會議 / 課程 / 直播對外」——決定啟用哪些語言包、
字體大小、結束時是否自動存逐字稿。Profile 只**建議**是否開雲端精修，不會替你開
（那會把內容送往第三方）。自訂 Profile：在 `%APPDATA%\SystemAudioSubtitle\profiles\` 放 yaml，
格式參考 `config/profiles/`。

## 5. 語言設定：語言包

翻譯方向由**語言包**決定（`config/langpacks/`）：內建 英→繁中、日→繁中、泰→繁中、繁中→英。
系統匣 →「語言包設定...」：

- **自動偵測**：可複選多個包，依偵測到的語言自動路由（例如直播混著日文與英文）
- **鎖定單一包**：強制所有語音都當作該語言，關掉自動偵測（語言已知時更準）
- **匯入語言包**：資料夾或 `.zip`。**新增一種語言不需要改任何程式碼**，只要一份 `pack.yaml`
  （可附術語表 `glossary.tsv`、幻覺黑名單 `hallucination.txt`）。範例：
  `docs/examples/langpacks/ko-zhHant/`（韓文 → 繁中）。匯入後勾選「套用」即可，不用重啟
- 匯入的包放在 `%APPDATA%\SystemAudioSubtitle\langpacks\`；語言包是**純資料**，含程式碼或
  可執行檔的包會被拒絕，格式錯誤時對話框會說明哪裡不對

### 雲端精修（選用，預設關閉）與效能監控

系統匣選單：

- **效能監控...**：降級階梯目前級別、各階段延遲 p50/p95/p99、RTF、丟棄/過濾計數。
  跟不上即時時系統會自動逐級降品質（beam↓ → 更新變慢 → 停精修 → 換小模型 → 丟過舊句子），
  這裡看得到現在在第幾級。超過 3 秒沒收到回報會明講（通常是 GPU 被其他程式佔滿）
- **雲端精修...**：把最近幾句連同前文送給 LLM 重譯，回來後靜默替換畫面上的字幕。
  **開啟 = 字幕原文會送往第三方**，勾選時會確認並顯示目的主機。斷網/額度用盡會自動熔斷、
  退回純本地字幕，恢復後自動繼續

雲端服務在啟動 inference-service 前用環境變數設定（OpenAI 相容的 chat completions 端點，
金鑰只存在這裡，不經過 UI）：

```powershell
$env:SAS_LLM_BASE_URL = "https://api.example.com/v1"
$env:SAS_LLM_MODEL    = "your-model"
$env:SAS_LLM_API_KEY  = "..."
```

降級階梯 L4 要用的備援模型需事先下載（不然 L4 停用）：
`.venv/Scripts/python.exe scripts/setup_models.py --degrade-model`

## 6. 已知限制

- **預設擷取整個輸出裝置**：背景音樂、通知音效也會被辨識。要只翻譯單一應用程式
  的聲音，用「音源選擇...」切到行程級擷取。Tier 2 失敗時會自動退回整個輸出裝置並警告
- **行程重開後的自動重建**只依行程名稱比對，同名的多個行程可能選錯（見 ROADMAP.md M4）
- **GPU 被其他程式佔滿時字幕會嚴重延遲**（例如開著 3D 遊戲）：降級階梯只能在解碼「之間」
  調參數，單次解碼被拖到幾十秒它沒辦法。「效能監控」面板會告訴你目前的狀態
- 翻譯品質受 NLLB-600M 限制：短句與語序較不同的語言（日/韓）偶有錯譯；長影片人名一致性
  只有術語表能保證
- 字幕顏色、樣式目前還不能在 UI 調整（字體大小可用 Profile 的倍率）
- 打包成單一發行資料夾（PyInstaller）見 `packaging/`，狀態見 ROADMAP.md M6

## 7. 疑難排解

**`inference-service` 啟動時噴 `cublas64_12.dll is not found`**
→ 通常是還沒跑過 `scripts/probe_gpu.py`，或是虛擬環境本身裝壞了。重新
`pip install -r requirements.txt` 一次。

**huggingface_hub 下載模型時噴 symlink 權限錯誤**
→ 已知 Windows 一般帳號限制，`inference-service`/`scripts/setup_models.py`
內部已經設定 `HF_HUB_DISABLE_SYMLINKS=1` 處理掉了；如果還是遇到，確認
`utils/gpu.py` 有被正確 import。

**浮層完全沒反應、`ui.app` 一直印「gateway 連線中斷」**
→ 確認 `gateway.server` 有先啟動且沒有報錯（預設監聽
`127.0.0.1:8765`，被其他程式佔用會啟動失敗）。

**字幕位置跑到螢幕外面看不到**
→ 目前沒有「重置位置」的功能，刪掉編輯模式時記住的視窗位置最簡單的
方式是直接重啟 `ui.app`（位置目前不會被持久化保存，見上方限制）。

## 8. 開發 / 測試

```powershell
.venv\Scripts\python -m pytest tests\              # 全部測試
.venv\Scripts\python -m pytest tests\ -m "not slow"  # 跳過需要真實 GPU/GUI/子行程的測試
```

M0 的兩個風險驗證 spike（延遲、行程級擷取可行性）：

```powershell
.venv\Scripts\python scripts\bench_latency.py
.venv\Scripts\python scripts\spike_b_process_loopback.py <pid> [seconds]
```
