"""audio/ringbuffer.py 的單元測試。

重點測三件事：基本讀寫正確、環繞（wraparound）正確、覆寫（overrun）偵測正確。
最後補一個真正跨進程的整合測試，因為 shared_memory 的重點就是跨進程，
只在單一進程內測邏輯不夠——之前 `ActivateAudioInterfaceAsync` 的教訓是
「單元測試過不代表跨邊界真的通」。
"""

from __future__ import annotations

import multiprocessing
import uuid

import numpy as np
import pytest

from audio.ringbuffer import RingBufferReader, RingBufferWriter


def unique_name() -> str:
    # Windows 的 shared_memory 名稱不能太長也不能有某些字元，用短 uuid hex。
    return f"sas-test-{uuid.uuid4().hex[:16]}"


@pytest.fixture
def rb_name() -> str:
    return unique_name()


def test_write_then_read_basic(rb_name: str) -> None:
    writer = RingBufferWriter(rb_name, capacity_samples=100, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=100)
        try:
            data = np.arange(10, dtype=np.float32)
            writer.write(data)
            out, overrun = reader.read()

            assert overrun is None
            np.testing.assert_array_equal(out, data)
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_read_with_nothing_written_returns_empty(rb_name: str) -> None:
    writer = RingBufferWriter(rb_name, capacity_samples=50, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=50)
        try:
            out, overrun = reader.read()
            assert len(out) == 0
            assert overrun is None
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_read_max_samples_leaves_remainder_for_next_read(rb_name: str) -> None:
    writer = RingBufferWriter(rb_name, capacity_samples=100, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=100)
        try:
            writer.write(np.arange(20, dtype=np.float32))

            first, _ = reader.read(max_samples=8)
            second, _ = reader.read(max_samples=100)

            np.testing.assert_array_equal(first, np.arange(8, dtype=np.float32))
            np.testing.assert_array_equal(second, np.arange(8, 20, dtype=np.float32))
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_wraparound_read_is_contiguous(rb_name: str) -> None:
    """寫入的資料跨過緩衝區尾端繞回開頭時，讀出來仍要是正確順序。"""
    capacity = 10
    writer = RingBufferWriter(rb_name, capacity_samples=capacity, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=capacity)
        try:
            # 先寫 7 個並讀走，把 read_total/write_total 都推到 7，
            # 接下來寫 6 個就會跨過緩衝尾端繞回開頭（7+6=13 > capacity=10）。
            writer.write(np.arange(7, dtype=np.float32))
            reader.read()

            wrap_data = np.arange(100, 106, dtype=np.float32)  # 6 個樣本
            writer.write(wrap_data)
            out, overrun = reader.read()

            assert overrun is None
            np.testing.assert_array_equal(out, wrap_data)
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_overrun_detected_when_reader_falls_behind(rb_name: str) -> None:
    """讀取端太慢、寫入端已經繞了一整圈以上：偵測到 overrun，並追到最舊的有效資料。"""
    capacity = 10
    writer = RingBufferWriter(rb_name, capacity_samples=capacity, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=capacity)
        try:
            # 寫 25 個樣本進一個容量只有 10 的緩衝，讀取端完全沒讀過。
            writer.write(np.arange(25, dtype=np.float32))

            out, overrun = reader.read()

            assert overrun is not None
            assert overrun.lost_samples == 25 - capacity  # 15 個被覆蓋掉
            # 讀取端應該追到「最舊仍然有效」的 10 個樣本，也就是 15..24
            np.testing.assert_array_equal(out, np.arange(15, 25, dtype=np.float32))
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_write_larger_than_capacity_keeps_only_tail(rb_name: str) -> None:
    capacity = 5
    writer = RingBufferWriter(rb_name, capacity_samples=capacity, create=True)
    try:
        reader = RingBufferReader(rb_name, capacity_samples=capacity)
        try:
            writer.write(np.arange(12, dtype=np.float32))  # 一次寫超過容量
            out, overrun = reader.read()

            np.testing.assert_array_equal(out, np.arange(7, 12, dtype=np.float32))
        finally:
            reader.close()
    finally:
        writer.close()
        writer.unlink()


def test_writer_never_blocks_regardless_of_reader_speed(rb_name: str) -> None:
    """鐵則驗證（ARCHITECTURE.md §11）：讀取端完全不讀，寫入端仍要能持續寫入不拋例外。"""
    writer = RingBufferWriter(rb_name, capacity_samples=16, create=True)
    try:
        for _ in range(1000):
            writer.write(np.ones(4, dtype=np.float32))
        assert writer.write_total == 4000
    finally:
        writer.close()
        writer.unlink()


# --- 跨進程整合測試 ---


def _cross_process_writer(name: str, capacity: int, n_batches: int) -> None:
    writer = RingBufferWriter(name, capacity_samples=capacity, create=False)
    try:
        for i in range(n_batches):
            writer.write(np.full(10, float(i), dtype=np.float32))
    finally:
        writer.close()


def test_cross_process_write_and_read(rb_name: str) -> None:
    capacity = 200
    n_batches = 20
    # 建立共享記憶體區塊（create=True）。
    writer_owner = RingBufferWriter(rb_name, capacity_samples=capacity, create=True)
    try:
        # Reader 要在子進程開始寫之前就 attach——ringbuffer 的設計是「從現在
        # 開始讀，不補歷史資料」（見 RingBufferReader docstring），這對應
        # 真實系統裡 inference-service 持續讀 audio-service 產生的即時資料，
        # 不是拿去讀一段早就結束的錄音。子進程寫完才建立 reader 會讀到空的，
        # 那是測試沒配合這個語意，不是 ringbuffer 的 bug。
        reader = RingBufferReader(rb_name, capacity_samples=capacity)
        try:
            proc = multiprocessing.Process(
                target=_cross_process_writer, args=(rb_name, capacity, n_batches)
            )
            proc.start()
            proc.join(timeout=10)
            assert proc.exitcode == 0

            out, overrun = reader.read()
            assert overrun is None
            assert len(out) == n_batches * 10
            # 最後一批寫的是全部填 19.0
            np.testing.assert_array_equal(out[-10:], np.full(10, 19.0, dtype=np.float32))
        finally:
            reader.close()
    finally:
        writer_owner.close()
        writer_owner.unlink()
