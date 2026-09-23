"""Spike B（ROADMAP.md M0）★ 最高風險項目。

目標只有一個：對指定 PID 呼叫 Windows 的 Process Loopback Capture API
（`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_PARAMS`），
驗證能不能拿到「只有那個行程」的 PCM，而不是整個系統混音。

目前沒有任何 Python 套件包裝這支 API，這裡直接用 ctypes + comtypes
手刻最小可行的 COM 呼叫鏈。見 ARCHITECTURE.md §6 Tier 2。

跑法：.venv/Scripts/python.exe scripts/spike_b_process_loopback.py <pid> [seconds]
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from ctypes import POINTER, byref, c_uint32, c_ulong, c_void_p, c_wchar_p, cast, sizeof
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.hresult import S_OK

# ---------------------------------------------------------------------------
# Win32 常數與型別（見 ARCHITECTURE.md §6，官方文件沒有 Python binding）
# ---------------------------------------------------------------------------

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000  # Tier 2 不支援，見 §6 陷阱 2，這裡故意不用

WAVE_FORMAT_IEEE_FLOAT = 3
VT_BLOB = 65  # PROPVARIANT.vt


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", c_uint32),
        ("ProcessLoopbackMode", c_uint32),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", c_uint32),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
        # C 原文是 union，但這個 spike 只用得到 ProcessLoopbackParams 這個成員，
        # 用 struct 而非 union 一樣能正確初始化（只是多浪費幾個 byte，無影響）。
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    """最小化的 PROPVARIANT，只支援我們需要的 VT_BLOB 用途。

    真正的 PROPVARIANT 是一個很大的 union（要能塞 LARGE_INTEGER 等），
    這裡只保證 blob 這個成員對齊正確、且結構體夠大不會被 OS 寫爆。
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("blob", BLOB),
        ("_padding", ctypes.c_uint64 * 2),  # 確保結構體大小 >= 真正的 PROPVARIANT
    ]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


# ---------------------------------------------------------------------------
# COM 介面定義（comtypes 標準寫法：沒有現成 typelib，手動宣告 vtable）
# ---------------------------------------------------------------------------

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IActivateAudioInterfaceCompletionHandler = GUID(
    "{41D949AB-9862-444A-80F6-C261334DA5EB}"
)
IID_IActivateAudioInterfaceAsyncOperation = GUID(
    "{72A22D78-CDE4-431D-B8CC-843A71199B6D}"
)
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")


class IAgileObject(IUnknown):
    """標記介面，沒有額外方法。宣告物件支援跨 apartment 直接呼叫，
    不需要 COM 走標準 marshaling/proxy 那一套——mmdevapi 內部用背景執行緒
    呼叫我們的完成回呼，沒有這個介面時，QueryInterface(IID_IAgileObject)
    會失敗，導致 ActivateAudioInterfaceAsync 直接以 E_UNEXPECTED 收場
    （實測結果，見 ARCHITECTURE.md §16 spike 記錄）。
    """

    _case_insensitive_ = True
    _iid_ = IID_IAgileObject
    _methods_ = []


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IActivateAudioInterfaceAsyncOperation
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetActivateResult",
            (["out"], POINTER(ctypes.c_long), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IActivateAudioInterfaceCompletionHandler
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "ActivateCompleted",
            (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
        ),
    ]


class IAudioClient(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Initialize",
            (["in"], c_uint32, "ShareMode"),
            (["in"], c_uint32, "StreamFlags"),
            (["in"], ctypes.c_int64, "hnsBufferDuration"),
            (["in"], ctypes.c_int64, "hnsPeriodicity"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["in"], c_void_p, "AudioSessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_int64), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD(
            [],
            HRESULT,
            "IsFormatSupported",
            (["in"], c_uint32, "ShareMode"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetMixFormat",
            (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat"),
        ),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_int64), "a"), (["out"], POINTER(ctypes.c_int64), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "eventHandle")),
        COMMETHOD(
            [],
            HRESULT,
            "GetService",
            (["in"], POINTER(GUID), "riid"),
            (["out"], POINTER(c_void_p), "ppv"),
        ),
    ]


AUDCLNT_BUFFERFLAGS_SILENT = 0x2


class IAudioCaptureClient(IUnknown):
    _case_insensitive_ = True
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(c_uint32), "pdwFlags"),
            (["out"], POINTER(ctypes.c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(ctypes.c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")
        ),
    ]


# ---------------------------------------------------------------------------
# 完成回呼（COM 物件實作）
# ---------------------------------------------------------------------------


class ActivateCompletionHandler(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = False
        self.hresult: int | None = None
        self.audio_client: "POINTER(IAudioClient) | None" = None

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, operation):
        # 注意：comtypes 對「回傳型別是 HRESULT 的介面方法」有特殊處理——
        # [out] 參數不是用 byref 傳進去，而是直接變成 Python 呼叫的回傳值
        # （HRESULT 本身失敗時 comtypes 會自動丟 COMError，不需要另外檢查）。
        # `operation` 也不需要 `.cast` 再 `.contents`，它本身就是可直接呼叫
        # 方法的 comtypes 介面指標——這兩點都是實測踩過的坑（見 ARCHITECTURE.md §16）。
        try:
            hr_result, punk = operation.GetActivateResult()
        except comtypes.COMError as e:
            self.hresult = e.hresult
            self.done = True
            return S_OK

        self.hresult = hr_result
        if hr_result == S_OK and punk:
            self.audio_client = punk.QueryInterface(IAudioClient)
        self.done = True
        return S_OK


# ---------------------------------------------------------------------------
# mmdevapi.dll 匯出的 ActivateAudioInterfaceAsync
# ---------------------------------------------------------------------------

_mmdevapi = ctypes.WinDLL("mmdevapi.dll")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    c_wchar_p,
    POINTER(GUID),
    POINTER(PROPVARIANT),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]


def activate_process_loopback_audio_client(
    pid: int, *, include_process_tree: bool = True, timeout_s: float = 5.0
) -> "POINTER(IAudioClient)":
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        ActivationType=AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        ProcessLoopbackParams=AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(
            TargetProcessId=pid,
            ProcessLoopbackMode=(
                PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                if include_process_tree
                else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
            ),
        ),
    )

    prop = PROPVARIANT()
    prop.vt = VT_BLOB
    prop.blob.cbSize = sizeof(params)
    prop.blob.pBlobData = cast(byref(params), c_void_p)

    handler = ActivateCompletionHandler()
    handler_iface = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)

    op_ptr = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = _ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        byref(IID_IAudioClient),
        byref(prop),
        handler_iface,
        byref(op_ptr),
    )
    if hr != S_OK:
        raise OSError(f"ActivateAudioInterfaceAsync 失敗，hr=0x{hr & 0xFFFFFFFF:08X}")

    # MTA（多執行緒 apartment）下不需要幫 COM 幫浦訊息迴圈——mmdevapi 背景
    # 執行緒會直接透過 vtable 呼叫我們的 IAgileObject 完成回呼，不像 STA
    # 需要 GetMessage/DispatchMessage 才能收到 COM 呼叫。
    deadline = time.monotonic() + timeout_s
    while not handler.done:
        if time.monotonic() > deadline:
            raise TimeoutError("ActivateAudioInterfaceAsync 完成回呼逾時")
        time.sleep(0.01)

    if handler.hresult != S_OK or handler.audio_client is None:
        raise OSError(f"Activate 完成但結果失敗，hresult=0x{(handler.hresult or 0) & 0xFFFFFFFF:08X}")

    return handler.audio_client


def make_float_stereo_format(sample_rate: int = 48000, channels: int = 2) -> WAVEFORMATEX:
    bits = 32
    block_align = channels * bits // 8
    return WAVEFORMATEX(
        wFormatTag=WAVE_FORMAT_IEEE_FLOAT,
        nChannels=channels,
        nSamplesPerSec=sample_rate,
        nAvgBytesPerSec=sample_rate * block_align,
        nBlockAlign=block_align,
        wBitsPerSample=bits,
        cbSize=0,
    )


def capture_seconds(pid: int, seconds: float, *, include_process_tree: bool = True) -> list[float]:
    """擷取 `seconds` 秒的 PCM，回傳每個取樣的 float32 值（已轉單聲道，取左右聲道平均）。"""
    # 用 MTA 而非預設的 STA：ActivateAudioInterfaceAsync 本身在 STA 下
    # 呼叫會直接收到 E_UNEXPECTED（實測結果，見 ARCHITECTURE.md §16 spike 記錄）。
    # `import comtypes` 本身就已經在目前執行緒自動以 STA 初始化 COM
    # （comtypes 的已知行為），必須先 CoUninitialize() 解除，才能重新用
    # COINIT_MULTITHREADED 初始化，否則 CoInitializeEx 會回
    # RPC_E_CHANGED_MODE（不能在同一執行緒切換 apartment 模式）。
    comtypes.CoUninitialize()
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    try:
        audio_client = activate_process_loopback_audio_client(
            pid, include_process_tree=include_process_tree
        )

        fmt = make_float_stereo_format()
        REFTIMES_PER_SEC = 10_000_000
        buffer_duration = REFTIMES_PER_SEC  # 1 秒緩衝

        # 注意：comtypes 對 HRESULT 回傳型別的介面方法一律自動檢查失敗
        # （失敗會直接丟 comtypes.COMError，不用自己比對回傳值），[out] 參數
        # 也不是用 byref 傳入，而是直接變成 Python 呼叫的回傳值（見上面
        # ActivateCompleted 的註解、以及 ARCHITECTURE.md §16 的 spike 記錄）。
        # `Initialize` 全部是 [in] 參數，所以呼叫風格不受影響。
        audio_client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            buffer_duration,
            0,
            byref(fmt),
            None,
        )

        capture_ptr = audio_client.GetService(byref(IID_IAudioCaptureClient))
        capture_client = cast(capture_ptr, POINTER(IAudioCaptureClient))

        audio_client.Start()

        samples: list[float] = []
        deadline = time.monotonic() + seconds
        # §6 陷阱 2：Process Loopback 不支援事件驅動模式，只能輪詢。
        while time.monotonic() < deadline:
            packet_frames = capture_client.GetNextPacketSize()
            if packet_frames == 0:
                time.sleep(0.005)
                continue

            data_ptr, num_frames, flags, _dev_pos, _qpc_pos = capture_client.GetBuffer()

            n = num_frames
            if n > 0 and not (flags & AUDCLNT_BUFFERFLAGS_SILENT):
                raw = ctypes.string_at(data_ptr, n * fmt.nBlockAlign)
                floats = struct.unpack(f"<{n * fmt.nChannels}f", raw)
                for i in range(n):
                    left = floats[i * fmt.nChannels]
                    right = floats[i * fmt.nChannels + 1]
                    samples.append((left + right) / 2.0)
            elif n > 0:
                samples.extend([0.0] * n)

            capture_client.ReleaseBuffer(n)

        audio_client.Stop()
        return samples
    finally:
        comtypes.CoUninitialize()


def rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: spike_b_process_loopback.py <pid> [seconds=3]")
        return 2
    pid = int(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    print(f"對 PID {pid} 做 process loopback 擷取，{seconds}s ...")
    try:
        samples = capture_seconds(pid, seconds)
    except Exception as e:
        print(f"失敗: {type(e).__name__}: {e}")
        return 1

    print(f"擷取到 {len(samples)} 個樣本，RMS energy = {rms(samples):.6f}")
    print("(RMS 明顯大於 0 代表真的收到該行程的音訊；若目標沒出聲，RMS≈0 是正常的)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
