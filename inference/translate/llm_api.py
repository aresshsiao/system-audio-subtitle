"""雲端 LLM 精修（可選，預設關閉）。見 ARCHITECTURE.md §9。

定稿累積 3~5 句後，連同前文一起送 LLM 重譯，回來後「靜默替換」已顯示的
定稿（state 升為 POLISHED）。使用者看到的是字幕悄悄變好，不是延遲變長——
所以整條路徑都在**背景執行緒**，主迴圈只做「把定稿丟進佇列」與「撈已完成
的結果」兩個不阻塞的動作；雲端再慢、再壞，本地字幕都不受影響。

**隱私**：開啟精修 = 會把原文（與前文、術語表）送到第三方 API。預設關閉；
開啟由 UI 明確確認（見 ui/panel/cloud_polish.py），API 金鑰只從環境變數讀，
絕不經過 ZMQ / UI / log。

**協定**：OpenAI 相容的 `POST {base_url}/chat/completions`（多數雲端服務與
本機推論伺服器都相容這個格式，不綁死單一供應商）。只用標準函式庫
（urllib），不多加相依套件。

**斷網 / 額度用盡**：熔斷器（`CircuitBreaker`）——連續失敗達門檻就暫停送出
（退避時間指數成長），期間定稿完全不進雲端；退避結束後放一個請求試探，成功就
恢復。使用者只損失潤飾，不損失功能（§9「混合路線的全部意義」）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from utils.metrics import Metrics

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "th": "Thai",
    "ko": "Korean",
    "zh-Hant": "Traditional Chinese (Taiwan)",
    "zh-Hans": "Simplified Chinese",
}


class CloudError(Exception):
    """雲端呼叫失敗（網路、HTTP 錯誤、額度、回傳格式不對）。"""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 15.0

    @property
    def host(self) -> str:
        """給 UI 的同意提示用：明確告訴使用者資料會送到哪裡。"""
        return urlparse(self.base_url).netloc or self.base_url

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LlmConfig | None":
        env = env if env is not None else os.environ
        base_url = env.get("SAS_LLM_BASE_URL", "").strip()
        api_key = env.get("SAS_LLM_API_KEY", "").strip()
        model = env.get("SAS_LLM_MODEL", "").strip()
        if not (base_url and model):
            return None
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            timeout_s=float(env.get("SAS_LLM_TIMEOUT_S", "15")),
        )


@dataclass(frozen=True)
class PolishItem:
    """一句待精修的定稿。`revision` 是它目前已發布的 revision，精修結果要用
    比它大的 revision 發布，gateway/UI 才會接受這次替換。"""

    utt_id: str
    revision: int
    span: object  # AudioSpan，這個模組不需要知道內容，原樣帶回給呼叫端
    pack_id: str
    src_lang: str
    tgt_lang: str
    source_text: str
    target_text: str
    glossary: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PolishResult:
    item: PolishItem
    polished_text: str


def build_messages(
    items: list[PolishItem], context: list[tuple[str, str]]
) -> list[dict[str, str]]:
    first = items[0]
    src_name = _LANGUAGE_NAMES.get(first.src_lang, first.src_lang)
    tgt_name = _LANGUAGE_NAMES.get(first.tgt_lang, first.tgt_lang)

    system = (
        f"You are a professional subtitle translator ({src_name} -> {tgt_name}). "
        "You receive consecutive subtitle sentences from ONE continuous video/speech, "
        "each with a rough machine translation. Re-translate each sentence so it is "
        "accurate, natural, and consistent with the surrounding context "
        "(pronouns, omitted subjects, names). Keep subtitle-length brevity. "
        'Reply with ONLY a JSON object: {"translations": ["...", ...]} containing '
        "exactly one translation per input sentence, in the same order."
    )
    if first.glossary:
        terms = "; ".join(f"{src} => {tgt}" for src, tgt in first.glossary.items())
        system += f" Mandatory glossary (always use these renderings): {terms}."

    payload = {
        "previous_context": [{"source": s, "translation": t} for s, t in context],
        "sentences": [
            {"source": it.source_text, "rough_translation": it.target_text} for it in items
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_translations(content: str, expected: int) -> list[str]:
    """從模型回覆取出譯文清單。模型偶爾會在 JSON 外包 markdown 圍欄或多講幾句，
    所以找第一個 `{` 到最後一個 `}` 之間的內容來解析。句數對不上一律視為失敗——
    寧可不替換，也不要把譯文錯位貼到別句上。"""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise CloudError("回覆中找不到 JSON")
    try:
        data = json.loads(content[start : end + 1])
        translations = data["translations"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CloudError(f"回覆格式不符: {exc}") from exc
    if not isinstance(translations, list) or not all(isinstance(t, str) for t in translations):
        raise CloudError("translations 必須是字串清單")
    if len(translations) != expected:
        raise CloudError(f"回傳 {len(translations)} 句，預期 {expected} 句")
    return [t.strip() for t in translations]


class LlmClient:
    """OpenAI 相容 chat completions 的最小客戶端。`opener` 可注入，測試用。"""

    def __init__(self, config: LlmConfig, opener: Callable | None = None) -> None:
        self._config = config
        self._opener = opener or urllib.request.urlopen

    def translate_batch(
        self, items: list[PolishItem], context: list[tuple[str, str]]
    ) -> list[str]:
        body = json.dumps(
            {
                "model": self._config.model,
                "messages": build_messages(items, context),
                "temperature": 0.2,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        request = urllib.request.Request(
            f"{self._config.base_url}/chat/completions", data=body, headers=headers, method="POST"
        )
        try:
            with self._opener(request, timeout=self._config.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 401/402/429 等（金鑰錯、額度用盡、限流）都在這裡；不把回應內文寫進
            # 訊息，避免供應商的錯誤頁帶出敏感資訊。
            raise CloudError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"連線失敗: {exc}") from exc

        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise CloudError(f"回應結構不符: {exc}") from exc
        return parse_translations(content, expected=len(items))


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        base_backoff_s: float = 30.0,
        max_backoff_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._base = base_backoff_s
        self._max = max_backoff_s
        self._clock = clock
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._trips = 0

    def allow(self) -> bool:
        """目前能不能送請求。退避期滿後回傳 True 讓一個請求去試探。"""
        return self._clock() >= self._open_until

    @property
    def is_open(self) -> bool:
        return not self.allow()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._trips = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        # 已經熔斷過、還沒成功恢復（trips > 0）時，退避結束後放行的那個試探
        # 請求如果又失敗，代表網路還沒好——直接再熔斷（退避加倍），不要再
        # 白白累積一輪 threshold 次失敗。
        if self._consecutive_failures >= self._threshold or self._trips > 0:
            backoff = min(self._base * (2**self._trips), self._max)
            self._open_until = self._clock() + backoff
            self._trips += 1
            self._consecutive_failures = 0
            logger.warning("雲端精修連續失敗，暫停 %.0fs（熔斷）", backoff)


class PolishWorker:
    """背景執行緒：收定稿 → 湊批 → 送雲端 → 結果放回佇列。

    主迴圈只呼叫 `submit()`（不阻塞）與 `poll_results()`（不阻塞）。
    `enabled=False` 時 `submit()` 直接忽略——預設關閉，且關閉期間不累積任何東西。
    """

    def __init__(
        self,
        client: LlmClient | None,
        metrics: Metrics,
        *,
        batch_size: int = 4,
        max_wait_s: float = 8.0,
        context_size: int = 3,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._batch_size = batch_size
        self._max_wait_s = max_wait_s
        self._context_size = context_size
        self._breaker = breaker or CircuitBreaker()
        self._clock = clock

        self.enabled = False
        self.allowed_by_degrade = True  # 降級階梯 L3 以上會關閉

        self._inbox: queue.Queue[PolishItem] = queue.Queue()
        self._results: queue.Queue[PolishResult] = queue.Queue()
        self._history: list[tuple[str, str]] = []  # 已定稿（含精修後）的 (原文, 譯文)
        self._pending: list[PolishItem] = []
        self._pending_since = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def breaker_open(self) -> bool:
        return self._breaker.is_open

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="polish-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def submit(self, item: PolishItem) -> None:
        if self.enabled and self.allowed_by_degrade and self.configured:
            self._inbox.put(item)

    def poll_results(self) -> list[PolishResult]:
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(0.1)

    def step(self) -> None:
        """一次迴圈的工作：收件、決定要不要送出。獨立成方法方便測試不開執行緒。"""
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break
            if not self._pending:
                self._pending_since = self._clock()
            self._pending.append(item)

        if not self._pending:
            return
        if not (self.enabled and self.allowed_by_degrade):
            self._pending.clear()
            return

        # 滿一批就送一批（一次 step 可能收進不只一批），不足一批的等 max_wait。
        while len(self._pending) >= self._batch_size:
            batch, self._pending = self._pending[: self._batch_size], self._pending[self._batch_size :]
            self._send(batch)
            self._pending_since = self._clock()
        if self._pending and self._clock() - self._pending_since >= self._max_wait_s:
            batch, self._pending = self._pending, []
            self._send(batch)

    def _send(self, batch: list[PolishItem]) -> None:
        if not self._breaker.allow():
            self._metrics.incr("polish_skipped_circuit_total", len(batch))
            self._remember(batch)
            return
        context = self._history[-self._context_size :]
        started = time.perf_counter()
        try:
            translations = self._client.translate_batch(batch, context)
        except CloudError as exc:
            self._breaker.record_failure()
            self._metrics.incr("polish_fail_total")
            logger.warning("雲端精修失敗（本地字幕不受影響）: %s", exc)
            self._remember(batch)
            return
        self._breaker.record_success()
        self._metrics.observe("polish_ms", (time.perf_counter() - started) * 1000.0)
        self._metrics.incr("polish_ok_total")

        polished_batch = []
        for item, text in zip(batch, translations, strict=True):
            if text and text != item.target_text:
                self._results.put(PolishResult(item=item, polished_text=text))
                polished_batch.append(replace(item, target_text=text))
            else:
                self._metrics.incr("polish_unchanged_total")
                polished_batch.append(item)
        self._remember(polished_batch)

    def _remember(self, items: list[PolishItem]) -> None:
        self._history.extend((it.source_text, it.target_text) for it in items)
        del self._history[: -self._context_size * 2]
