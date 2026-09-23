"""GPU / CUDA DLL 路徑注入。

ctranslate2 的 Windows wheel 不會自動找到 pip 安裝的 nvidia-cublas-cu12 /
nvidia-cudnn-cu12 套件（它們把 DLL 放在 <package>/bin/ 下，不在系統 PATH 上）。
必須在 `import ctranslate2`（或任何依賴它的模組，如 faster_whisper）**之前**
呼叫 `ensure_cuda_dll_path()`，否則會直接載入失敗或悄悄退回 CPU。

用法（放在 inference-service 進程入口最前面）：

    from utils.gpu import ensure_cuda_dll_path
    ensure_cuda_dll_path()
    import ctranslate2  # 現在才 import
"""

from __future__ import annotations

import os
import sys


def ensure_cuda_dll_path() -> bool:
    """把 nvidia-cublas-cu12 / nvidia-cudnn-cu12 的 bin/ 目錄加進 PATH。

    回傳 True 表示兩個套件都找到了；False 表示至少一個缺失
    （呼叫端應該預期 CUDA 不可用並走 CPU 後備路徑）。
    """
    if sys.platform != "win32":
        # Linux 上 pip 套件是用 RPATH/LD_LIBRARY_PATH 機制，不需要這段
        return True

    ok = True
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            mod = __import__(pkg_name, fromlist=["_"])
        except ImportError:
            ok = False
            continue
        bin_dir = os.path.join(os.path.dirname(mod.__file__), "bin")
        if not os.path.isdir(bin_dir):
            ok = False
            continue
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        # 只改 PATH 不夠：Python 3.8+ 在 Windows 上預設用「安全 DLL 搜尋模式」，
        # ctypes/原生擴充（ctranslate2 底層就是）載入相依 DLL 時不一定會走
        # PATH，而是需要明確用 os.add_dll_directory() 註冊搜尋路徑，否則
        # 即使 PATH 有加、`ctranslate2.get_cuda_device_count()` 這類只碰
        # CUDA driver API 的呼叫會成功，但真正跑矩陣運算需要的
        # cublas64_12.dll 卻載入失敗（這是實測踩到的坑，不是理論推測）。
        os.add_dll_directory(bin_dir)
    return ok
