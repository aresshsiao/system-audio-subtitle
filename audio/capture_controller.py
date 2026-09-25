"""擷取來源的建立與執行期切換（含 Tier 2 → Tier 1 自動退回）。見
ARCHITECTURE.md §6、§16。

從 `audio/service.py` 抽出來的原因：切換的決策邏輯（先開新的、成功才關舊的、
Tier 2 失敗要退回 Tier 1 並且明講）需要用假的 backend 測，塞在有真實擷取迴圈
的 `main()` 裡沒辦法單元測試。這裡只管「開哪個 backend、失敗怎麼辦」，
重採樣/VAD/segmenter 的重置在 service.py（那些跟音訊流連續性有關，不是
擷取來源的事）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from audio.capture.base import CaptureBackend
from contracts.enums import CaptureTargetKind
from contracts.messages import CaptureTarget, SetCaptureTargetAck

logger = logging.getLogger(__name__)

BackendFactory = Callable[[CaptureTargetKind], CaptureBackend]

_DEFAULT_ENDPOINT = CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id="default")


def default_backend_factory(kind: CaptureTargetKind) -> CaptureBackend:
    # 延遲 import：Tier 2 的 COM 模組 import 時會動到 COM apartment
    # （見 _win32_process_loopback_com.ensure_mta），沒人要用行程級擷取的
    # 時候不該碰。
    if kind == CaptureTargetKind.PROCESS:
        from audio.capture.process_loopback import ProcessLoopbackCapture

        return ProcessLoopbackCapture()

    from audio.capture.wasapi_loopback import WasapiLoopbackCapture

    return WasapiLoopbackCapture()


def default_cable_finder() -> str | None:
    from audio.capture.virtual_cable import find_virtual_cable_device

    device = find_virtual_cable_device()
    return device.device_id if device else None


def describe_target(target: CaptureTarget) -> str:
    if target.kind == CaptureTargetKind.PROCESS:
        from audio.capture.enumerate import get_process_name

        name = get_process_name(target.pid) if target.pid is not None else None
        return f"行程 {name or '?'} (PID {target.pid})"
    if target.kind == CaptureTargetKind.VIRTUAL_CABLE:
        return f"虛擬音效裝置 (#{target.device_id})"
    if target.device_id in (None, "default"):
        return "預設輸出裝置（整個系統的聲音）"
    return f"輸出裝置 #{target.device_id}（整個裝置的聲音）"


def parse_target_spec(spec: str | None) -> CaptureTarget:
    """解析 `SAS_CAPTURE_TARGET` 環境變數（啟動時的初始來源，之後可由 UI 切換）。

        未設定 / "endpoint:default"  → Tier 1，預設 WASAPI loopback 端點
        "endpoint:<device_id>"       → Tier 1，指定裝置索引
        "process:<pid>"              → Tier 2，含子行程樹
        "process:<pid>:notree"       → Tier 2，只抓該 PID 自己（不含子行程）
    """
    spec = spec or "endpoint:default"
    parts = spec.split(":")

    if parts[0] == "process":
        if len(parts) < 2:
            raise ValueError(f"SAS_CAPTURE_TARGET 格式錯誤，process 要帶 pid: {spec!r}")
        include_tree = not (len(parts) > 2 and parts[2] == "notree")
        return CaptureTarget(
            kind=CaptureTargetKind.PROCESS,
            pid=int(parts[1]),
            include_process_tree=include_tree,
        )
    if parts[0] == "endpoint":
        return CaptureTarget(
            kind=CaptureTargetKind.ENDPOINT,
            device_id=parts[1] if len(parts) > 1 else "default",
        )
    raise ValueError(f"SAS_CAPTURE_TARGET 無法識別: {spec!r}")


class CaptureController:
    """持有目前的 `CaptureBackend`，處理切換。

    `switch()` 的順序刻意是「先開新的、成功了才關舊的」：新來源開不起來時，
    舊的仍在擷取、字幕不中斷；反過來（先關舊的）的話，開失敗就會留下一個
    沒有任何擷取的空窗，還得再花力氣把舊的開回來。代價是短暫同時持有兩個
    擷取物件，loopback 是共享模式，這沒有問題。
    """

    def __init__(
        self,
        backend_factory: BackendFactory = default_backend_factory,
        cable_finder: Callable[[], str | None] = default_cable_finder,
    ) -> None:
        self._factory = backend_factory
        self._cable_finder = cable_finder
        self.backend: CaptureBackend | None = None
        self.target: CaptureTarget | None = None

    def open_initial(self, target: CaptureTarget) -> SetCaptureTargetAck:
        ack = self.switch(target)
        if not ack.success:
            raise RuntimeError(ack.error or "無法開啟初始擷取來源")
        return ack

    def switch(self, requested: CaptureTarget) -> SetCaptureTargetAck:
        try:
            resolved = self._resolve(requested)
        except ValueError as exc:
            return SetCaptureTargetAck(success=False, error=str(exc))

        warning: str | None = None
        opened: tuple[CaptureBackend, CaptureTarget] | None = None
        try:
            opened = self._open(resolved)
        except Exception as exc:  # noqa: BLE001 - 任何開啟失敗都要回報，不能讓主迴圈崩
            logger.warning("open %s failed: %s", describe_target(resolved), exc)
            if resolved.kind == CaptureTargetKind.PROCESS:
                # Tier 2 是「最好的」但不是每台機器/每個目標都成功（見 §16）。
                # 退回 Tier 1 是使用者當下最想要的結果（至少有字幕），但一定要
                # 明講——行程級隔離沒了，Discord 語音等其他聲音會混進來。
                try:
                    opened = self._open(_DEFAULT_ENDPOINT)
                    warning = (
                        f"行程級擷取失敗（{exc}），已退回整個輸出裝置的擷取——"
                        "其他應用程式的聲音也會被翻譯"
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    logger.warning("fallback to default endpoint failed: %s", fallback_exc)
            if opened is None:
                return SetCaptureTargetAck(
                    success=False,
                    error=f"無法開啟 {describe_target(resolved)}：{exc}",
                )

        backend, active_target = opened
        old = self.backend
        self.backend, self.target = backend, active_target
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                logger.exception("closing previous capture backend failed")

        return SetCaptureTargetAck(
            success=True,
            active_kind=active_target.kind,
            description=describe_target(active_target),
            warning=warning,
        )

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
            self.backend = None

    def _resolve(self, target: CaptureTarget) -> CaptureTarget:
        """VIRTUAL_CABLE 只是「幫使用者找出 VB-CABLE 的裝置 id」，擷取本身
        就是 Tier 1 端點擷取（見 virtual_cable.py）。"""
        if target.kind != CaptureTargetKind.VIRTUAL_CABLE:
            return target
        device_id = target.device_id or self._cable_finder()
        if device_id is None:
            from audio.capture.virtual_cable import guidance_text

            raise ValueError(guidance_text())
        return CaptureTarget(kind=CaptureTargetKind.VIRTUAL_CABLE, device_id=device_id)

    def _open(self, target: CaptureTarget) -> tuple[CaptureBackend, CaptureTarget]:
        backend = self._factory(target.kind)
        try:
            # backend 只認 ENDPOINT/PROCESS；虛擬音效裝置對它而言就是端點
            backend_target = (
                CaptureTarget(kind=CaptureTargetKind.ENDPOINT, device_id=target.device_id)
                if target.kind == CaptureTargetKind.VIRTUAL_CABLE
                else target
            )
            backend.open(backend_target)
        except Exception:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        return backend, target
