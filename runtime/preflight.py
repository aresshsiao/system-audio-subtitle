"""環境自檢。見 ROADMAP.md M6「首次啟動精靈」。

一台新機器上最常見的失敗都是「環境沒準備好」而不是程式壞了：沒有 NVIDIA GPU 或
驅動太舊、模型沒下載、沒有音效輸出裝置、連接埠被上一次沒關乾淨的實例佔著。這裡把
這些檢查集中成一份清單，每一項都給「怎麼修」，不只是報錯。

每個檢查都是獨立的純函式，外部依賴（nvidia-smi、檔案系統、PyAudio、socket）都可注入，
所以能在沒有 GPU 的機器上測試判斷邏輯。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from contracts.topics import BUS_ENDPOINTS, GATEWAY_HTTP_HOST, GATEWAY_HTTP_PORT
from utils.paths import models_dir

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = models_dir()

MIN_VRAM_MB = 6000  # large-v3 int8 + NLLB 約 2.5~3GB，加上系統與其他程式的餘裕
MIN_FREE_VRAM_MB = 3000


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix: str = ""  # 怎麼修；OK 時為空


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name, Status.OK, detail)


# --- 各項檢查 ---


def check_python(version: tuple[int, int] | None = None, platform: str | None = None) -> CheckResult:
    version = version or sys.version_info[:2]
    platform = platform or sys.platform
    if platform != "win32":
        return CheckResult("作業系統", Status.FAIL, f"目前是 {platform}", "本工具只支援 Windows（WASAPI loopback）")
    if version < (3, 11):
        return CheckResult("Python", Status.FAIL, f"{version[0]}.{version[1]}", "需要 Python 3.11 以上（建議 3.13）")
    return _ok("Python / 作業系統", f"Python {version[0]}.{version[1]} on Windows")


def _run_nvidia_smi() -> str:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "nvidia-smi 失敗")
    return out.stdout


def check_gpu(run_smi: Callable[[], str] = _run_nvidia_smi) -> list[CheckResult]:
    try:
        line = run_smi().strip().splitlines()[0]
        name, total, free, driver = (x.strip() for x in line.split(","))
        total_mb, free_mb = int(total), int(free)
    except (OSError, RuntimeError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
        return [
            CheckResult(
                "NVIDIA GPU",
                Status.FAIL,
                f"偵測不到（{e}）",
                "需要 NVIDIA GPU 與驅動（安裝最新 Game Ready / Studio 驅動）；沒有 GPU 時 Whisper large-v3 無法即時運作",
            )
        ]

    results = [_ok("NVIDIA GPU", f"{name}（驅動 {driver}）")]
    if total_mb < MIN_VRAM_MB:
        results.append(
            CheckResult("顯示記憶體", Status.WARN, f"總共 {total_mb} MB", f"建議 {MIN_VRAM_MB} MB 以上；不夠時降級階梯會更常介入")
        )
    elif free_mb < MIN_FREE_VRAM_MB:
        results.append(
            CheckResult(
                "顯示記憶體",
                Status.WARN,
                f"目前只剩 {free_mb} MB 可用（總共 {total_mb} MB）",
                "關掉其他吃 GPU 的程式（遊戲、瀏覽器硬體加速的大分頁），否則模型可能載不進去、或解碼被拖慢",
            )
        )
    else:
        results.append(_ok("顯示記憶體", f"可用 {free_mb} MB / 總共 {total_mb} MB"))
    return results


def _hf_cache_root() -> Path:
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def check_models(models_dir: Path = MODELS_DIR, hf_cache: Path | None = None) -> list[CheckResult]:
    hf_cache = hf_cache if hf_cache is not None else _hf_cache_root()
    results = []

    def need(name: str, present: bool, detail: str, fix: str) -> None:
        results.append(_ok(name, detail) if present else CheckResult(name, Status.FAIL, "找不到", fix))

    fix_all = "跑 .venv/Scripts/python.exe scripts/setup_models.py"
    need("VAD 模型", (models_dir / "silero_vad.onnx").is_file(), "silero_vad.onnx", fix_all)
    need("翻譯模型 (NLLB)", any(models_dir.glob("nllb*/model.bin")), "NLLB-200 (CTranslate2)", fix_all)
    whisper = list(hf_cache.glob("models--Systran--faster-whisper-large-v3/snapshots/*/model.bin"))
    need(
        "語音辨識模型 (Whisper large-v3)",
        bool(whisper),
        "已快取",
        "第一次啟動 inference-service 會自動下載（約 3GB，需要網路，請耐心等待）",
    )
    if not whisper:  # 沒快取只是「第一次會慢」，不是壞掉
        results[-1] = CheckResult(results[-1].name, Status.WARN, "尚未下載", results[-1].fix)

    turbo = list(hf_cache.glob("models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/*/model.bin"))
    results.append(
        _ok("降級備援模型 (L4)", "large-v3-turbo 已快取")
        if turbo
        else CheckResult(
            "降級備援模型 (L4)", Status.WARN, "未下載（降級階梯會跳過 L4）", "選用：scripts/setup_models.py --degrade-model"
        )
    )
    return results


def _list_loopback() -> int:
    from audio.capture.enumerate import list_loopback_devices

    return len(list_loopback_devices())


def check_audio(count_devices: Callable[[], int] = _list_loopback) -> CheckResult:
    try:
        n = count_devices()
    except Exception as e:  # noqa: BLE001 — PyAudio 初始化失敗也是「不能擷取」
        return CheckResult("音訊擷取", Status.FAIL, f"無法列舉音訊裝置（{e}）", "確認有啟用的音效輸出裝置（喇叭/耳機）")
    if n == 0:
        return CheckResult("音訊擷取", Status.FAIL, "沒有可擷取的輸出裝置", "插上耳機或啟用喇叭；系統音效設定裡要有啟用的播放裝置")
    return _ok("音訊擷取", f"{n} 個 WASAPI loopback 裝置")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _our_ports() -> list[int]:
    ports = {int(ep.rsplit(":", 1)[1]) for ep in BUS_ENDPOINTS.values()}
    ports.add(GATEWAY_HTTP_PORT)
    return sorted(ports)


def check_ports(port_free: Callable[[int], bool] = _port_free, ports: list[int] | None = None) -> CheckResult:
    ports = ports if ports is not None else _our_ports()
    busy = [p for p in ports if not port_free(p)]
    if busy:
        return CheckResult(
            "連接埠",
            Status.WARN,
            f"被佔用：{busy}",
            "可能是上一次沒關乾淨的實例仍在執行（工作管理員結束 python.exe），或有其他程式佔用；若你已經在跑本工具，這是正常的",
        )
    return _ok("連接埠", f"{len(ports)} 個本機連接埠皆可用（{GATEWAY_HTTP_HOST}）")


def check_langpacks() -> CheckResult:
    from inference.langpack import LangPackRegistry

    registry = LangPackRegistry()
    registry.reload()
    n = len(registry.all_packs())
    if n == 0:
        return CheckResult("語言包", Status.FAIL, "沒有任何可用的語言包", "檢查 config/langpacks/，或用「語言包設定 → 匯入」")
    return _ok("語言包", f"{n} 個可用")


def run_preflight() -> list[CheckResult]:
    results = [check_python(), *check_gpu(), *check_models(), check_audio(), check_ports(), check_langpacks()]
    for r in results:
        logger.info("preflight [%s] %s: %s", r.status.value, r.name, r.detail)
    return results


def summarize(results: list[CheckResult]) -> Status:
    if any(r.status == Status.FAIL for r in results):
        return Status.FAIL
    if any(r.status == Status.WARN for r in results):
        return Status.WARN
    return Status.OK
