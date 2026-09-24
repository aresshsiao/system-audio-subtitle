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

## 3. 啟動（四個進程，各開一個終端機視窗，照順序啟動）

```powershell
# 1. audio-service — 擷取系統輸出音訊、VAD 切句
.venv\Scripts\python -m audio.service

# 2. inference-service — ASR 轉錄（第一次啟動要等模型載入，約 4~8 秒）
$env:SAS_ASR_LANGUAGE = "en"   # 見下方「語言設定」，留空 = 自動偵測
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

## 4. 操作

| 操作 | 方式 |
|---|---|
| 顯示 / 隱藏字幕 | 熱鍵 `Ctrl+Alt+S`，或系統匣圖示右鍵選單 |
| 拖曳字幕到想要的位置 | 熱鍵 `Ctrl+Alt+E` 進入編輯模式（此時會暫時關閉點擊穿透），拖曳後再按一次退出 |
| 結束程式 | 系統匣圖示右鍵 →「結束」 |

**平常（非編輯模式）滑鼠點擊會直接穿透浮層**，可以正常操作底下播放器的控制列，
不會被字幕擋住。

## 5. 語言設定

`inference-service` 用環境變數控制辨識語言：

```powershell
$env:SAS_ASR_LANGUAGE = "en"   # 英文；也可以 "ja"（日文）、"th"（泰文）
$env:SAS_ASR_LANGUAGE = ""     # 留空或不設 = Whisper 自動偵測語言
```

還有兩個進階環境變數（通常不需要動）：

```powershell
$env:SAS_STABILIZER_GRANULARITY = "char"  # 日/泰文建議用 char，英文用預設的 word
$env:SAS_HALLUCINATION_BLACKLIST = "config\langpacks\ja-zhHant\hallucination.txt"
```

> 語言包（`config/langpacks/`）目前只是資料檔案，`inference-service`
> 還沒有真正讀它們做自動路由——這是 M3 要接上的部分，見 ROADMAP.md。

## 6. 已知限制（現在這個階段）

- **沒有翻譯**：顯示的是原文，不是中文
- **只有 Tier 1 擷取**：抓的是整個系統輸出端點的混音，不是單一應用程式的聲音
  （例如背景音樂、通知音效也會被抓進去、可能被辨識出來——這是已知的 Tier 1
  限制，Tier 2 行程級擷取排在 M4）
- **沒有安裝精靈**：要照上面手動啟動四個進程
- **沒有設定面板**：字型大小、顏色目前只能改程式碼

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
