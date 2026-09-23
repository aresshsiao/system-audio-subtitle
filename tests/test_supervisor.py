"""runtime/supervisor.py 的整合測試：真的拉起 OS 子進程並觀察其生命週期。

這些測試比較慢（牽涉真的 subprocess 啟動與 sleep），標記為 slow，
CI 或日常開發可以用 `-m "not slow"` 跳過，M0 驗收時要完整跑一次。
"""

from __future__ import annotations

import os
import time

import pytest

from runtime.supervisor import ManagedProcess, RestartPolicy, Supervisor

pytestmark = pytest.mark.slow


def test_start_all_launches_real_processes() -> None:
    sup = Supervisor(
        [
            ManagedProcess("audio", "runtime._test_stub_service"),
            ManagedProcess("inference", "runtime._test_stub_service"),
            ManagedProcess("gateway", "runtime._test_stub_service"),
        ]
    )
    sup.start_all()
    try:
        time.sleep(2.0)  # 給三個進程時間完成至少一次 heartbeat
        assert sup.is_alive("audio")
        assert sup.is_alive("inference")
        assert sup.is_alive("gateway")
    finally:
        sup.stop_all(grace_period_s=3.0)

    assert not sup.is_alive("audio")
    assert not sup.is_alive("inference")
    assert not sup.is_alive("gateway")


def test_crash_triggers_automatic_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """模擬 CUDA OOM 這類崩潰：進程掛掉後 Supervisor 要在退避時間後拉回來。"""
    monkeypatch.setenv("SAS_STUB_CRASH_AFTER_S", "1")

    restart_delay_s = 0.5
    sup = Supervisor(
        [
            ManagedProcess(
                "flaky",
                "runtime._test_stub_service",
                restart_policy=RestartPolicy(initial_delay_s=restart_delay_s, max_delay_s=2.0),
            )
        ],
        poll_interval_s=0.2,
    )
    sup.start_all()
    try:
        # 第一輪：啟動 → 1s 後崩潰 → 偵測到（restart_count += 1）→ 退避
        # restart_delay_s → 才真的重新 spawn。restart_count 在退避「開始前」
        # 就會 +1（見 supervisor.py _check_one），所以這裡等到 restart_count
        # 增加之後，還要再等過退避時間，新進程才會真的活著。
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and sup.restart_count("flaky") < 1:
            time.sleep(0.1)
        assert sup.restart_count("flaky") >= 1, "崩潰後沒有觸發重啟"

        time.sleep(restart_delay_s + 0.3)  # 讓退避時間確實走完，新進程完成 spawn
        assert sup.is_alive("flaky"), "重啟後的新進程應該還活著（會在下個 1s 後才又崩潰）"
    finally:
        sup.stop_all(grace_period_s=3.0)


def test_stop_all_prevents_pending_restart() -> None:
    """stop_all() 呼叫後，正在退避等待中的重啟不應該又跑出一個新進程。"""
    os.environ["SAS_STUB_CRASH_AFTER_S"] = "0.5"
    try:
        sup = Supervisor(
            [
                ManagedProcess(
                    "flaky",
                    "runtime._test_stub_service",
                    restart_policy=RestartPolicy(initial_delay_s=3.0),
                )
            ],
            poll_interval_s=0.1,
        )
        sup.start_all()
        time.sleep(1.0)  # 讓它崩潰一次，進入 3s 退避等待
        assert sup.restart_count("flaky") >= 1

        sup.stop_all(grace_period_s=1.0)
        count_after_stop = sup.restart_count("flaky")

        time.sleep(2.0)  # 原本 3s 退避會在這段時間內到期
        assert sup.restart_count("flaky") == count_after_stop, "stop_all 後不應該還有新的重啟"
    finally:
        os.environ.pop("SAS_STUB_CRASH_AFTER_S", None)
