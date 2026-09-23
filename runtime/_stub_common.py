"""M0 骨架階段的共用進程主迴圈，供 audio/service.py、inference/service.py、
gateway/server.py 這三個目前還是空殼的進程入口共用。

**這支檔案的內容會在對應里程碑實作時被真正的邏輯取代**（M1 換掉 audio 與
inference、M2 換掉 gateway），存在的唯一目的是讓 M0 能驗證
runtime/supervisor.py 真的能拉起、監看、重啟三個獨立的 OS 進程。

`SAS_STUB_CRASH_AFTER_S` 環境變數讓測試可以注入「跑幾秒後崩潰」，
藉此驗證 Supervisor 的重啟與退避邏輯，不需要另外寫一份會崩潰的假進程。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

logger = logging.getLogger(__name__)


def run_stub_heartbeat(service_name: str, *, interval_s: float = 1.0) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{service_name}] %(message)s",
        stream=sys.stdout,
    )

    crash_after_s_raw = os.environ.get("SAS_STUB_CRASH_AFTER_S")
    crash_after_s = float(crash_after_s_raw) if crash_after_s_raw else None

    stop = False

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    # Windows 上 SIGTERM 可以被註冊 handler，但 Popen.terminate() 實際呼叫
    # TerminateProcess()，不會觸發它——這裡註冊只是為了 Linux/測試環境下的
    # 一致行為，Windows 正式關閉流程走 supervisor 的 grace period + kill。
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, AttributeError, OSError):
        pass

    started_at = time.monotonic()
    logger.info("stub service started (pid=%d)", os.getpid())

    # 崩潰時機檢查用細粒度 tick（獨立於 heartbeat log 的輸出頻率），
    # 否則 SAS_STUB_CRASH_AFTER_S 的精確度會被 interval_s 綁死——
    # 例如 interval_s=1.0 時，設定 0.5s 崩潰實際上要等到 ~1.0s 才觸發，
    # 這在測試 Supervisor 的退避時機時會造成看似隨機的誤判。
    tick_s = min(0.05, interval_s)
    next_heartbeat_at = started_at + interval_s

    while not stop:
        now = time.monotonic()
        elapsed = now - started_at
        if crash_after_s is not None and elapsed >= crash_after_s:
            logger.warning("SAS_STUB_CRASH_AFTER_S=%.2f 到期，模擬崩潰退出", crash_after_s)
            sys.exit(1)
        if now >= next_heartbeat_at:
            logger.info("heartbeat, uptime=%.1fs", elapsed)
            next_heartbeat_at += interval_s
        time.sleep(tick_s)

    logger.info("received stop signal, exiting cleanly")
