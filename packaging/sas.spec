# PyInstaller spec：one-dir 打包（不是 one-file——one-file 每次啟動都要解壓數 GB 到暫存目錄）。
#
# 打包的是「程式」，**不含模型**：
#   - models/            VAD 與 NLLB（跑 scripts/setup_models.py 產生，放在 exe 旁邊）
#   - Whisper large-v3   第一次啟動由 huggingface_hub 下載到使用者的 HF 快取
# 這樣安裝檔不會多出 3GB+，模型升級也不用重新打包。
#
# 建置：packaging/build.ps1（或 .venv\Scripts\pyinstaller packaging\sas.spec --noconfirm）

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], []

# 四個服務是用 `exe --module <name>` 由 runpy 動態載入的，靜態分析看不到，要明列
for pkg in ("audio", "inference", "gateway", "ui", "runtime", "contracts", "utils"):
    hiddenimports += collect_submodules(pkg)

# 需要連同原生 DLL / 資料檔一起打包的第三方套件
for pkg in (
    "ctranslate2",
    "faster_whisper",
    "onnxruntime",
    "sentencepiece",
    "tokenizers",
    "comtypes",
    "pyaudiowpatch",
    "zmq",
    "nvidia.cublas",  # utils/gpu.py 靠 nvidia.cublas / nvidia.cudnn 的 bin/ 找 CUDA DLL
    "nvidia.cudnn",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# uvicorn 的協定/事件迴圈實作是字串動態載入
hiddenimports += collect_submodules("uvicorn") + collect_submodules("websockets")
hiddenimports += ["yaml", "ulid", "fastapi", "starlette", "anyio"]

# 內建資料：語言包、Profile
datas += [
    (str(ROOT / "config"), "config"),
]

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SystemAudioSubtitle",
    console=True,  # 先保留主控台：第一次出問題時才看得到錯誤；穩定後再改 False
)
coll = COLLECT(exe, a.binaries, a.datas, name="SystemAudioSubtitle")
