"""一鍵啟動：audio-service、inference-service、gateway 由 Supervisor 拉起並監看
（崩潰自動重啟，見 runtime/supervisor.py），UI 在本進程執行；關閉 UI 就一起收掉全部。

    .venv\Scripts\python -m launcher

打包成 exe 後的內部用法：`sas.exe --module audio.service`（Supervisor 用它啟動各服務，
見 runtime/supervisor.py 的 `command_for_module`）。
"""

from __future__ import annotations

import logging
import runpy
import sys


def _run_module_and_exit(argv: list[str]) -> None:
    runpy.run_module(argv[1], run_name="__main__", alter_sys=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "--module":
        sys.argv = [argv[1], *argv[2:]]
        _run_module_and_exit(argv)
        return 0

    from runtime.supervisor import ManagedProcess, Supervisor
    from utils.logging import setup_logging

    setup_logging("launcher")
    log = logging.getLogger("launcher")

    supervisor = Supervisor(
        [
            ManagedProcess("audio", "audio.service"),
            ManagedProcess("inference", "inference.service"),
            ManagedProcess("gateway", "gateway.server"),
        ]
    )
    supervisor.start_all()
    log.info("服務已啟動（audio / inference / gateway），開啟 UI")
    try:
        from ui.app import main as ui_main

        return ui_main()
    finally:
        log.info("UI 已關閉，停止所有服務")
        supervisor.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
