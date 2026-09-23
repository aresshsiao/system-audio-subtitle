"""進程生命週期管理：拉起、監看、崩潰後自動重啟。

見 ARCHITECTURE.md §2「崩潰隔離」。這是三進程架構的安全網：inference-service
被 CUDA OOM 或驅動 TDR 殺掉時，Supervisor 要偵測到、記錄、按退避策略重啟，
而不是讓整個系統跟著死掉或無聲卡住。

設計取捨：
  - 用 `subprocess.Popen` 啟動真正獨立的 OS 進程，不是 `multiprocessing`——
    要的就是完全獨立的記憶體空間與崩潰隔離，`multiprocessing` 在 spawn
    模式下也接近，但 Popen 語意更直接、之後要換成不同 Python 直譯器
    （例如某個服務想釘死在特定版本）也不用改程式碼。
  - 每個進程是一個獨立的 `python -m <module>` 呼叫，模組本身要能被
    `python -m` 執行（有 `if __name__ == "__main__"`）。
  - 退避重啟：崩潰後不是立刻重啟，而是指數退避（見 RestartPolicy），
    避免「啟動即崩潰」的進程把 CPU 耗在無限重啟迴圈上。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestartPolicy:
    """指數退避重啟策略。"""

    initial_delay_s: float = 1.0
    max_delay_s: float = 30.0
    backoff_factor: float = 2.0
    # 進程穩定運行超過這個秒數後，退避計時器歸零——避免「跑了三小時才崩潰一次」
    # 的正常情況被誤判成連續崩潰，導致下次重啟被硬套用最大延遲。
    reset_after_stable_s: float = 60.0


@dataclass
class ManagedProcess:
    """一個受監控的服務定義。"""

    name: str
    module: str  # 用 `python -m <module>` 啟動
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)

    # --- 執行期狀態，Supervisor 內部維護，呼叫端不要手動改 ---
    _popen: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _current_delay_s: float = field(default=0.0, init=False, repr=False)
    _started_at: float = field(default=0.0, init=False, repr=False)
    _restart_count: int = field(default=0, init=False, repr=False)


class Supervisor:
    """啟動並持續監看一組 ManagedProcess，崩潰時依退避策略重啟。

    用法：
        sup = Supervisor([
            ManagedProcess("audio", "audio.service"),
            ManagedProcess("inference", "inference.service"),
            ManagedProcess("gateway", "gateway.server"),
        ])
        sup.start_all()
        ...
        sup.stop_all()
    """

    def __init__(self, processes: list[ManagedProcess], *, poll_interval_s: float = 0.5) -> None:
        self._processes = {p.name: p for p in processes}
        self._poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start_all(self) -> None:
        for proc in self._processes.values():
            self._spawn(proc)
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_all(self, *, grace_period_s: float = 5.0) -> None:
        """優雅關閉：先 terminate() 給進程機會清理，逾時才 kill()。"""
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=self._poll_interval_s * 2)

        for proc in self._processes.values():
            if proc._popen is None or proc._popen.poll() is not None:
                continue
            logger.info("terminating %s (pid=%s)", proc.name, proc._popen.pid)
            proc._popen.terminate()

        deadline = time.monotonic() + grace_period_s
        for proc in self._processes.values():
            if proc._popen is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc._popen.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning("%s 沒有在 %.1fs 內結束，強制 kill", proc.name, grace_period_s)
                proc._popen.kill()
                proc._popen.wait()

    def is_alive(self, name: str) -> bool:
        proc = self._processes[name]
        return proc._popen is not None and proc._popen.poll() is None

    def restart_count(self, name: str) -> int:
        return self._processes[name]._restart_count

    def _spawn(self, proc: ManagedProcess) -> None:
        logger.info("starting %s (%s)", proc.name, proc.module)
        proc._popen = subprocess.Popen([sys.executable, "-m", proc.module])
        proc._started_at = time.monotonic()

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            for proc in self._processes.values():
                self._check_one(proc)
            self._stop_event.wait(self._poll_interval_s)

    def _check_one(self, proc: ManagedProcess) -> None:
        if proc._popen is None:
            return
        returncode = proc._popen.poll()
        if returncode is None:
            uptime = time.monotonic() - proc._started_at
            if uptime >= proc.restart_policy.reset_after_stable_s:
                proc._current_delay_s = 0.0
            return

        logger.warning("%s 結束，returncode=%s，準備重啟", proc.name, returncode)
        proc._restart_count += 1

        policy = proc.restart_policy
        proc._current_delay_s = (
            policy.initial_delay_s
            if proc._current_delay_s == 0.0
            else min(proc._current_delay_s * policy.backoff_factor, policy.max_delay_s)
        )
        delay = proc._current_delay_s
        logger.info("%s 將在 %.1fs 後重啟（第 %d 次）", proc.name, delay, proc._restart_count)

        if self._stop_event.wait(delay):
            return  # stop_all() 被呼叫，不要再重啟
        self._spawn(proc)
