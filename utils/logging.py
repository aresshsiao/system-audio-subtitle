"""單一日誌設定點。見 ARCHITECTURE.md §4：三個進程都呼叫這支檔案的
`setup_logging()`，不要讓每個進程各自 `logging.basicConfig()`——那樣
格式、等級、輸出目的地會漸漸分裂，除錯時很難比對不同進程的 log。
"""

from __future__ import annotations

import logging
import sys


def setup_logging(process_name: str, *, level: int = logging.INFO) -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 主控台預設非 UTF-8

    logging.basicConfig(
        level=level,
        format=f"%(asctime)s [{process_name}] %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
