"""shared_memory 環形緩衝：single-producer single-consumer，寫入端永不阻塞。

見 ARCHITECTURE.md §2、§11：audio-service 是唯一寫入端，寫入絕對不能被
inference-service 的讀取速度拖慢——緩衝滿了就直接覆蓋最舊的樣本，
沒有鎖、沒有阻塞、也絕不讓佇列無限增長。

跨進程共享用 `multiprocessing.shared_memory.SharedMemory`：一個進程建立
（create=True），另一個進程用同樣的名字 attach（create=False）。

記憶體佈局：

    [ 8 bytes: write_total (uint64, little-endian) ][ capacity 個 float32 樣本 ]

`write_total` 是「從建立以來總共寫入過幾個樣本」的單調遞增計數器，不是緩衝區
內的位置——實際位置永遠是 `write_total % capacity`。讀取端自己在本地追蹤
`read_total`（不需要共享，因為 SPSC 只有一個讀取端），用
`write_total - read_total` 算出「有多少新樣本可讀」；超過 capacity 代表資料
已經被覆寫過，讀取端要偵測到這種 overrun 並往前追到還有效的最舊樣本。

執行緒/跨進程安全性說明：`write_total` 用單一 uint64、8-byte 對齊寫入，在
x86-64（本專案的目標平台，見 ARCHITECTURE.md）上是原子的，讀取端讀到的值
要嘛是寫入前、要嘛是寫入後，不會讀到「寫一半」的撕裂值。寫入端永遠先寫
樣本資料、再更新 `write_total`（release 順序），讀取端永遠先讀
`write_total`、再讀樣本資料（acquire 順序），這樣即使兩端完全沒有鎖，
讀取端也不會讀到「counter 已更新但資料還沒寫完」的樣本。
"""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np

_HEADER_BYTES = 8  # 一個 uint64


def _shm_size(capacity_samples: int) -> int:
    return _HEADER_BYTES + capacity_samples * 4  # float32 = 4 bytes


@dataclass(frozen=True)
class RingBufferOverrun:
    """讀取時偵測到的覆寫事件：這段樣本已經被寫入端蓋掉，永久遺失。"""

    lost_samples: int
    read_total_before: int
    read_total_after: int


class RingBufferWriter:
    """唯一寫入端。`write()` 永遠立即返回，緩衝滿了就覆蓋最舊樣本。"""

    def __init__(self, name: str, capacity_samples: int, *, create: bool = True) -> None:
        self.capacity = capacity_samples
        if create:
            self._shm = shared_memory.SharedMemory(
                name=name, create=True, size=_shm_size(capacity_samples)
            )
        else:
            self._shm = shared_memory.SharedMemory(name=name)
        self._header = np.ndarray((1,), dtype=np.uint64, buffer=self._shm.buf[:_HEADER_BYTES])
        self._data = np.ndarray(
            (capacity_samples,), dtype=np.float32, buffer=self._shm.buf[_HEADER_BYTES:]
        )
        if create:
            self._header[0] = 0
            self._data[:] = 0.0

    @property
    def name(self) -> str:
        return self._shm.name

    @property
    def write_total(self) -> int:
        return int(self._header[0])

    def write(self, samples: np.ndarray) -> None:
        """寫入一批 float32 樣本。永不阻塞，永不因為緩衝滿了而丟棄新資料——
        丟的永遠是最舊的資料（覆蓋），這是刻意的設計取捨（見模組 docstring）。

        `write_total` 永遠推進「這批樣本的完整長度」，即使一次寫入的量本身就
        超過緩衝容量、只有尾端真的進得了緩衝——這樣不管呼叫端是一次寫 25
        個樣本、還是分 25 次各寫 1 個，讀取端算出來的 overrun 結果要一致，
        不能因為批次大小不同就漏算或多算（之前的版本沒做到，見對應測試）。
        """
        n_total = len(samples)
        if n_total == 0:
            return

        write_total = self.write_total
        if n_total > self.capacity:
            samples = samples[-self.capacity :]
        n_kept = len(samples)

        # 這批樣本「應該」落在緩衝裡的位置，是以它們原本的邏輯寫入順序算的，
        # 不是從 write_total 原本的位置開始接——被截掉的前段等於進來就立刻
        # 被蓋掉，尾端要接在「彷彿整批都逐一寫入」後該在的位置。
        start = (write_total + n_total - n_kept) % self.capacity
        end = start + n_kept
        if end <= self.capacity:
            self._data[start:end] = samples
        else:
            first_part = self.capacity - start
            self._data[start:] = samples[:first_part]
            self._data[: end - self.capacity] = samples[first_part:]

        # 資料先寫完，counter 最後更新（release）——見模組 docstring 的順序說明。
        self._header[0] = write_total + n_total

    def close(self) -> None:
        self._shm.close()

    def unlink(self) -> None:
        """只有建立者（create=True 的那一端）該呼叫，釋放共享記憶體區塊。"""
        self._shm.unlink()

    def __enter__(self) -> "RingBufferWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RingBufferReader:
    """唯一讀取端。`read()` 立即返回目前可讀的資料，不足 `max_samples` 也沒關係
    （呼叫端要能處理讀到 0 或任意長度的情況，這不是阻塞式 API）。
    """

    def __init__(self, name: str, capacity_samples: int) -> None:
        self.capacity = capacity_samples
        self._shm = shared_memory.SharedMemory(name=name)
        self._header = np.ndarray((1,), dtype=np.uint64, buffer=self._shm.buf[:_HEADER_BYTES])
        self._data = np.ndarray(
            (capacity_samples,), dtype=np.float32, buffer=self._shm.buf[_HEADER_BYTES:]
        )
        self._read_total = int(self._header[0])  # 從「現在」開始讀，不補歷史資料
        self.total_overrun_samples = 0

    def read(self, max_samples: int | None = None) -> tuple[np.ndarray, RingBufferOverrun | None]:
        write_total = int(self._header[0])  # acquire：先讀 counter
        available = write_total - self._read_total

        overrun: RingBufferOverrun | None = None
        if available > self.capacity:
            lost = available - self.capacity
            new_read_total = write_total - self.capacity
            overrun = RingBufferOverrun(
                lost_samples=lost,
                read_total_before=self._read_total,
                read_total_after=new_read_total,
            )
            self.total_overrun_samples += lost
            self._read_total = new_read_total
            available = self.capacity

        if available <= 0:
            return np.empty(0, dtype=np.float32), overrun

        n = available if max_samples is None else min(max_samples, available)
        start = self._read_total % self.capacity
        end = start + n
        if end <= self.capacity:
            out = self._data[start:end].copy()
        else:
            first_part = self.capacity - start
            out = np.concatenate([self._data[start:], self._data[: end - self.capacity]])

        self._read_total += n
        return out, overrun

    @property
    def read_total(self) -> int:
        return self._read_total

    def close(self) -> None:
        self._shm.close()

    def __enter__(self) -> "RingBufferReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
