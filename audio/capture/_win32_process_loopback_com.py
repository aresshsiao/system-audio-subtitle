"""Process Loopback Capture 的底層 Win32/COM 機制。見 ARCHITECTURE.md §6 Tier 2。

這支檔案是 M0 Spike B（`scripts/spike_b_process_loopback.py`）驗證過的
COM 呼叫鏈本體，抽出來給正式的 `process_loopback.py` 與 spike 腳本共用，
避免兩邊各自維護一份、日後改一邊忘了改另一邊。目前沒有任何 Python 套件
包裝 `ActivateAudioInterfaceAsync` 這支 API，這裡直接用 ctypes + comtypes
手刻 vtable。

三個實測踩過、容易再犯的坑（詳見 ARCHITECTURE.md §6、§16 的記錄）：
  1. 必須用 MTA，不能用 STA——STA 下 `ActivateAudioInterfaceAsync` 同步
     呼叫就直接收到 `E_UNEXPECTED`。
  2. 完成回呼物件必須實作 `IAgileObject`，否則背景執行緒呼叫回來時失敗，
     錯誤訊息完全看不出是 marshaling 問題。
  3. comtypes 對 HRESULT 回傳型別的方法有特殊呼叫慣例：`[out]` 參數是
     回傳值，不是用 byref 填的參數；`operation` 這類介面指標直接呼叫
     方法即可，不需要 `.cast()` 再 `.contents`。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import POINTER, byref, c_uint32, c_ulong, c_void_p, c_wchar_p, cast, sizeof

# comtypes 的官方機制：import 時依 `sys.coinit_flags` 初始化 COM，沒設就是 STA。
# 這裡一開始就要 MTA（見 ensure_mta 的說明），所以要在 import comtypes 之前設。
# 只在這支模組是「第一個 import comtypes 的」時有效——audio-service 的 main()
# 會在碰任何 PyAudio 之前先 import 它。
sys.coinit_flags = 0x0  # COINIT_MULTITHREADED

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.hresult import S_OK

# ---------------------------------------------------------------------------
# Win32 常數與型別（官方文件沒有 Python binding）
# ---------------------------------------------------------------------------

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000  # Tier 2 不支援，見 §6 陷阱 2，故意不用
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_IEEE_FLOAT = 3
VT_BLOB = 65  # PROPVARIANT.vt

REFTIMES_PER_SEC = 10_000_000


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", c_uint32),
        ("ProcessLoopbackMode", c_uint32),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", c_uint32),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
        # C 原文是 union，這裡只用得到 ProcessLoopbackParams 這個成員，
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


# ---------------------------------------------------------------------------
# COM 介面定義（comtypes 標準寫法：沒有現成 typelib，手動宣告 vtable）
# ---------------------------------------------------------------------------

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IActivateAudioInterfaceCompletionHandler = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
IID_IActivateAudioInterfaceAsyncOperation = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")


class IAgileObject(IUnknown):
    """標記介面，沒有額外方法。宣告物件支援跨 apartment 直接呼叫，不需要
    COM 走標準 marshaling/proxy 那一套——mmdevapi 內部用背景執行緒呼叫
    我們的完成回呼，沒有這個介面時，QueryInterface(IID_IAgileObject) 會
    失敗，導致 ActivateAudioInterfaceAsync 直接以 E_UNEXPECTED 收場。
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
            [], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevicePeriod",
            (["out"], POINTER(ctypes.c_int64), "a"),
            (["out"], POINTER(ctypes.c_int64), "b"),
        ),
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
        self.audio_client = None

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, operation):
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


def activate_process_loopback_audio_client(pid: int, *, include_process_tree: bool = True, timeout_s: float = 5.0):
    """對指定 PID 啟用 process loopback，回傳一個已初始化 apartment 內的
    `IAudioClient` 介面指標。呼叫端要自己負責 `Initialize`/`Start`/`Stop`。

    呼叫前必須已經用 `ensure_mta()` 把目前執行緒切到 MTA（見下方），
    這支函式不會自己切，因為 apartment 是 per-thread 的狀態，切換的時機
    要由呼叫端根據自己的執行緒模型決定。
    """
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

    # MTA 下不需要幫 COM 幫浦訊息迴圈——mmdevapi 背景執行緒會直接透過
    # vtable 呼叫我們的 IAgileObject 完成回呼，不像 STA 需要
    # GetMessage/DispatchMessage 才能收到 COM 呼叫。
    deadline = time.monotonic() + timeout_s
    while not handler.done:
        if time.monotonic() > deadline:
            raise TimeoutError("ActivateAudioInterfaceAsync 完成回呼逾時")
        time.sleep(0.01)

    if handler.hresult != S_OK or handler.audio_client is None:
        raise OSError(f"Activate 完成但結果失敗，hresult=0x{(handler.hresult or 0) & 0xFFFFFFFF:08X}")

    return handler.audio_client


_APTTYPE_MTA = 1
_ole32 = ctypes.windll.ole32


def _current_apartment_is_mta() -> bool:
    apt_type = ctypes.c_int(0)
    qualifier = ctypes.c_int(0)
    hr = _ole32.CoGetApartmentType(byref(apt_type), byref(qualifier))
    return hr == 0 and apt_type.value == _APTTYPE_MTA


def ensure_mta() -> None:
    """確保目前執行緒的 COM apartment 是 MTA（多執行緒 apartment）。

    `ActivateAudioInterfaceAsync` 在 STA 下呼叫會直接同步失敗，回傳
    `E_UNEXPECTED`（實測結果）。

    **正確的用法是「進程最開頭、任何 PyAudio 之前」呼叫一次**：這支模組
    import 時已經設了 `sys.coinit_flags`，讓 comtypes 一開始就以 MTA 初始化，
    這裡只是確認。之後 PyAudio（PortAudio）自己 init/terminate COM 時，在 MTA
    執行緒上是平衡的，不會影響我們這一份初始化。

    **為什麼不能「等到要用 Tier 2 才切」**（M4 UI 即時切換音源時實測踩到）：
    audio-service 通常先以 Tier 1 啟動，PortAudio 已經在這個執行緒上初始化
    過 COM（STA），這時再 `CoUninitialize()`（只抵消一層引用計數）→
    `CoInitializeEx(MTA)` 會得到 `RPC_E_CHANGED_MODE`；重試又多做一次
    `CoUninitialize()` 把整個 apartment 拆掉，之後 PortAudio 釋放時就是
    `CO_E_NOTINITIALIZED`。所以現在偵測到已經是 MTA 就直接返回，只有在
    「不是 MTA」（例如別的程式碼先用 STA import 了 comtypes）時才走舊的
    切換路徑，作為最後手段。
    """
    if _current_apartment_is_mta():
        return
    comtypes.CoUninitialize()
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)


def read_capture_packet(capture_client, fmt: WAVEFORMATEX) -> tuple[bytes | None, int, bool]:
    """輪詢一次（§6 陷阱 2：Process Loopback 不支援事件驅動模式，只能輪詢）。

    回傳 `(raw_bytes, num_frames, is_silent)`。`raw_bytes` 為 None 代表
    這次輪詢沒有新資料（`GetNextPacketSize()` 回 0），呼叫端該短暫等待
    再重試，不是錯誤。
    """
    packet_frames = capture_client.GetNextPacketSize()
    if packet_frames == 0:
        return None, 0, False

    data_ptr, num_frames, flags, _dev_pos, _qpc_pos = capture_client.GetBuffer()
    is_silent = bool(flags & AUDCLNT_BUFFERFLAGS_SILENT)
    raw = ctypes.string_at(data_ptr, num_frames * fmt.nBlockAlign) if num_frames > 0 else b""
    capture_client.ReleaseBuffer(num_frames)
    return raw, num_frames, is_silent


def bytes_to_frames(raw: bytes, num_frames: int, channels: int) -> np.ndarray:
    """把 GetBuffer 拿到的 interleaved float32 原始位元組轉成
    shape=(num_frames, channels) 的 numpy 陣列——刻意**不在這裡做混音**，
    維持跟 `WasapiLoopbackCapture.read()` 一樣的回傳形狀，混成單聲道是
    `audio/resample.py` 的 `to_mono()` 統一負責的事，不要在兩個
    `CaptureBackend` 實作裡各自重複一份混音邏輯。
    """
    flat = np.frombuffer(raw, dtype=np.float32)
    return flat.reshape(num_frames, channels)
