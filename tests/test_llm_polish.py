"""雲端精修：LlmClient（對本機假 HTTP 伺服器）、熔斷器、PolishWorker、控制請求。

不需要真的雲端金鑰——伺服器端行為（成功、格式錯誤、HTTP 錯誤、逾時、連不上）
都由本機假伺服器模擬。**真實供應商相容性沒有在這裡驗證**，見 ROADMAP.md M5。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from contracts.messages import AudioSpan, SetCloudPolish
from inference.translate.llm_api import (
    CircuitBreaker,
    CloudError,
    LlmClient,
    LlmConfig,
    PolishItem,
    PolishWorker,
    build_messages,
    parse_translations,
)
from utils.metrics import Metrics


def item(n: int, target: str = "粗譯") -> PolishItem:
    return PolishItem(
        utt_id=f"u{n}",
        revision=2,
        span=AudioSpan(0, 16000),
        pack_id="ja-zhHant",
        src_lang="ja",
        tgt_lang="zh-Hant",
        source_text=f"原文{n}",
        target_text=f"{target}{n}",
        glossary={"アリス": "愛麗絲"},
    )


class FakeServer:
    """記錄收到的請求，回應內容由測試設定。"""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | http500 | http429 | garbage | wrongcount | slow
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # 安靜
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
                )
                if outer.mode in ("http500", "http429"):
                    self.send_response(int(outer.mode[4:]))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.mode == "slow":
                    threading.Event().wait(2.0)
                user = json.loads(body["messages"][1]["content"])
                n = len(user["sentences"])
                if outer.mode == "wrongcount":
                    n += 1
                if outer.mode == "garbage":
                    content = "not json"
                else:
                    fenced = json.dumps({"translations": [f"精修{i}" for i in range(n)]})
                    content = "```json\n" + fenced + "\n```"
                payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def client_for(server: FakeServer, timeout_s: float = 3.0) -> LlmClient:
    cfg = LlmConfig(base_url=server.url, api_key="secret-key", model="m", timeout_s=timeout_s)
    return LlmClient(cfg)


def test_client_success_sends_expected_request(server) -> None:
    out = client_for(server).translate_batch([item(1), item(2)], context=[("前文", "上文")])

    assert out == ["精修0", "精修1"]
    req = server.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer secret-key"
    system = req["body"]["messages"][0]["content"]
    assert "Japanese" in system and "Traditional Chinese" in system
    assert "アリス => 愛麗絲" in system  # 術語表寫進 system prompt
    user = json.loads(req["body"]["messages"][1]["content"])
    assert user["previous_context"] == [{"source": "前文", "translation": "上文"}]
    assert [s["source"] for s in user["sentences"]] == ["原文1", "原文2"]


@pytest.mark.parametrize("mode", ["http500", "http429", "garbage", "wrongcount"])
def test_client_failure_modes_raise_cloud_error(server, mode) -> None:
    server.mode = mode
    with pytest.raises(CloudError):
        client_for(server).translate_batch([item(1), item(2)], context=[])


def test_client_timeout_raises_cloud_error(server) -> None:
    server.mode = "slow"
    with pytest.raises(CloudError):
        client_for(server, timeout_s=0.3).translate_batch([item(1)], context=[])


def test_client_connection_refused_raises_cloud_error() -> None:
    """模擬「拔網路線」：連不上。"""
    cfg = LlmConfig(base_url="http://127.0.0.1:9/v1", api_key="k", model="m", timeout_s=1.0)
    with pytest.raises(CloudError):
        LlmClient(cfg).translate_batch([item(1)], context=[])


def test_api_key_never_appears_in_error_messages(server) -> None:
    server.mode = "http500"
    with pytest.raises(CloudError) as exc:
        client_for(server).translate_batch([item(1)], context=[])
    assert "secret-key" not in str(exc.value)


def test_parse_translations_tolerates_fences_rejects_mismatch() -> None:
    assert parse_translations('前言 {"translations": ["a", "b"]} 結語', 2) == ["a", "b"]
    with pytest.raises(CloudError):
        parse_translations('{"translations": ["a"]}', 2)
    with pytest.raises(CloudError):
        parse_translations('{"translations": [1, 2]}', 2)


def test_config_from_env_requires_url_and_model() -> None:
    assert LlmConfig.from_env({}) is None
    assert LlmConfig.from_env({"SAS_LLM_BASE_URL": "https://x/v1"}) is None
    cfg = LlmConfig.from_env(
        {"SAS_LLM_BASE_URL": "https://api.example.com/v1/", "SAS_LLM_MODEL": "m"}
    )
    assert cfg.base_url == "https://api.example.com/v1" and cfg.host == "api.example.com"


def test_glossary_absent_means_no_glossary_clause() -> None:
    plain = PolishItem("u", 0, None, "p", "en", "zh-Hant", "a", "b")
    assert "glossary" not in build_messages([plain], [])[0]["content"].lower()


# --- CircuitBreaker ---


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_threshold_and_backs_off_exponentially() -> None:
    clock = Clock()
    b = CircuitBreaker(failure_threshold=3, base_backoff_s=30, max_backoff_s=100, clock=clock)
    for _ in range(3):
        b.record_failure()
    assert b.is_open
    clock.now = 31
    assert b.allow()  # 退避結束，放行試探
    b.record_failure()  # 試探失敗 → 立刻再熔斷，退避加倍
    assert b.is_open
    clock.now = 31 + 59
    assert b.is_open
    clock.now = 31 + 61
    assert b.allow()
    b.record_success()
    assert b.allow()
    for _ in range(3):
        b.record_failure()
    clock.now += 31  # 成功後退避重置回 30s
    assert b.allow()


# --- PolishWorker ---


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[list[PolishItem], list]] = []

    def translate_batch(self, items, context):
        self.calls.append((items, list(context)))
        if self.fail:
            raise CloudError("down")
        return [f"精修-{it.utt_id}" for it in items]


def make_worker(client, **kw) -> tuple[PolishWorker, Metrics, Clock]:
    clock = Clock()
    m = Metrics()
    w = PolishWorker(client, m, batch_size=3, max_wait_s=10.0, clock=clock, **kw)
    return w, m, clock


def test_disabled_by_default_accepts_nothing() -> None:
    client = FakeClient()
    w, _m, _c = make_worker(client)
    assert w.enabled is False
    for i in range(5):
        w.submit(item(i))
    w.step()
    assert client.calls == [] and w.poll_results() == []


def test_batches_by_size_and_returns_results_with_original_items() -> None:
    client = FakeClient()
    w, m, _c = make_worker(client)
    w.enabled = True
    for i in range(3):
        w.submit(item(i))
    w.step()

    assert len(client.calls) == 1 and len(client.calls[0][0]) == 3
    results = w.poll_results()
    assert [r.polished_text for r in results] == ["精修-u0", "精修-u1", "精修-u2"]
    assert results[0].item.revision == 2  # 呼叫端用 revision+1 發布
    assert m.snapshot()["counters"]["polish_ok_total"] == 1


def test_partial_batch_flushes_after_max_wait() -> None:
    client = FakeClient()
    w, _m, clock = make_worker(client)
    w.enabled = True
    w.submit(item(0))
    w.step()
    assert client.calls == []
    clock.now = 11
    w.step()
    assert len(client.calls) == 1


def test_later_batches_get_previous_sentences_as_context() -> None:
    client = FakeClient()
    w, _m, _c = make_worker(client)
    w.enabled = True
    for i in range(6):
        w.submit(item(i))
    w.step()
    assert client.calls[0][1] == []
    w.step()
    assert client.calls[1][1] != []
    assert client.calls[1][1][-1][1] == "精修-u2"  # 用精修後的譯文當前文


def test_cloud_failure_keeps_local_subtitles_and_trips_breaker() -> None:
    client = FakeClient(fail=True)
    w, m, _c = make_worker(client)
    w.enabled = True
    for round_ in range(3):
        for i in range(3):
            w.submit(item(round_ * 10 + i))
        w.step()
    assert w.poll_results() == []  # 沒有任何精修結果，但也沒有例外
    assert w.breaker_open
    calls_before = len(client.calls)

    for i in range(3):
        w.submit(item(100 + i))
    w.step()
    assert len(client.calls) == calls_before  # 熔斷期間不再送出
    counters = m.snapshot()["counters"]
    assert counters["polish_fail_total"] == 3 and counters["polish_skipped_circuit_total"] == 3


def test_degrade_l3_blocks_polish() -> None:
    client = FakeClient()
    w, _m, _c = make_worker(client)
    w.enabled = True
    w.allowed_by_degrade = False
    for i in range(3):
        w.submit(item(i))
    w.step()
    assert client.calls == []


def test_unchanged_text_is_not_republished() -> None:
    class Echo:
        def translate_batch(self, items, context):
            return [it.target_text for it in items]

    w, m, _c = make_worker(Echo())
    w.enabled = True
    for i in range(3):
        w.submit(item(i))
    w.step()
    assert w.poll_results() == []
    assert m.snapshot()["counters"]["polish_unchanged_total"] == 3


def test_not_configured_ignores_submit_even_when_enabled() -> None:
    w, _m, _c = make_worker(None)
    w.enabled = True
    w.submit(item(0))
    w.step()
    assert not w.configured and w.poll_results() == []


# --- service 控制請求 ---


def test_control_request_query_enable_disable_and_unconfigured() -> None:
    from inference.service import handle_cloud_polish_request

    class RT:  # 只需要 handle_cloud_polish_request 用到的欄位
        pass

    rt = RT()
    rt.llm_config = LlmConfig("https://api.example.com/v1", "k", "m")
    rt.polish, _m, _c = make_worker(FakeClient())

    status = handle_cloud_polish_request(SetCloudPolish(enabled=None), rt)
    assert status.enabled is False and status.configured
    assert status.endpoint_host == "api.example.com"

    assert handle_cloud_polish_request(SetCloudPolish(enabled=True), rt).enabled is True
    assert handle_cloud_polish_request(SetCloudPolish(enabled=False), rt).enabled is False

    rt.llm_config = None
    rt.polish, _m, _c = make_worker(None)
    status = handle_cloud_polish_request(SetCloudPolish(enabled=True), rt)
    assert not status.success and "SAS_LLM_BASE_URL" in status.error and status.enabled is False
