"""Tier 1：WASAPI Loopback（整個輸出端點）。見 ARCHITECTURE.md §6。

用 `PyAudioWPatch` 開啟 `*.loopback` 裝置——擷取整個輸出端點的混音，
不分行程。這是 M1 打通全管線用的第一層，之後 Tier 2（行程級）在 M4
補上時，上層完全不需要改，因為都遵守同一個 `CaptureBackend` 介面。
"""

from __future__ import annotations

import pyaudiowpatch as pyaudio

from audio.capture.base import AudioFormat
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget

import numpy as np

# WASAPI loopback 的緩衝週期。§6 陷阱 2 是講 Tier 2（行程級）不支援事件驅動、
# 只能輪詢；Tier 1 端點級 loopback 其實支援事件回呼，但這裡統一用固定週期
# 輪詢阻塞讀取——介面要跟 Tier 2 一致，且 10ms 週期已經在延遲預算內
# （見 ARCHITECTURE.md §10：擷取緩衝預算 20-30ms）。
_CHUNK_MS = 10.0


class WasapiLoopbackCapture:
    """`CaptureBackend` 的 Tier 1 實作。"""

    def __init__(self) -> None:
        self._pa = pyaudio.PyAudio()
        self._stream: pyaudio.Stream | None = None
        self._format: AudioFormat | None = None
        self._frames_per_buffer: int = 0

    def open(self, target: CaptureTarget) -> None:
        if target.kind != CaptureTargetKind.ENDPOINT:
            raise ValueError(
                f"WasapiLoopbackCapture 只接受 CaptureTargetKind.ENDPOINT，收到 {target.kind}"
            )

        device_info = self._resolve_device(target.device_id)
        sample_rate = int(device_info["defaultSampleRate"])
        channels = int(device_info["maxInputChannels"])
        self._frames_per_buffer = max(1, round(sample_rate * _CHUNK_MS / 1000))

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_info["index"],
            frames_per_buffer=self._frames_per_buffer,
        )
        self._format = AudioFormat(sample_rate=sample_rate, channels=channels)

    def _resolve_device(self, device_id: str | None) -> dict:
        if device_id is None or device_id == "default":
            try:
                return self._pa.get_default_wasapi_loopback()
            except OSError as e:
                raise RuntimeError(
                    "找不到預設 WASAPI loopback 裝置——目前系統可能沒有預設播放裝置，"
                    "或該裝置不支援 loopback"
                ) from e

        try:
            index = int(device_id)
        except ValueError:
            raise ValueError(f"device_id 應該是裝置索引數字或 'default'，收到: {device_id!r}") from None

        info = self._pa.get_device_info_by_index(index)
        if not info.get("isLoopbackDevice"):
            raise ValueError(f"裝置索引 {index}（{info.get('name')}）不是 loopback 裝置")
        return info

    def read(self) -> np.ndarray | None:
        if self._stream is None:
            raise RuntimeError("read() 前必須先呼叫 open()")

        available = self._stream.get_read_available()
        if available <= 0:
            return None

        n = min(available, self._frames_per_buffer * 4)  # 避免一次讀進過大的量
        raw = self._stream.read(n, exception_on_overflow=False)
        data = np.frombuffer(raw, dtype=np.float32)

        channels = self._format.channels
        if channels > 1:
            data = data.reshape(-1, channels)
        return data

    @property
    def format(self) -> AudioFormat:
        if self._format is None:
            raise RuntimeError("format 只在 open() 之後才有效")
        return self._format

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        self._pa.terminate()
