"""Thin OpenRouter client (OpenAI-compatible Chat Completions).

Supports text and vision messages, JSON-mode requests, retries with backoff,
and graceful failure. The client is provider-agnostic in spirit -- it only
assumes the OpenAI chat schema that OpenRouter exposes -- but every default
points at OpenRouter as required.
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import mimetypes
import random
import re
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from ..settings import Settings, get_settings

# Bounded pool used to enforce a HARD wall-clock deadline per HTTP request.
# httpx's read timeout can fail to fire if a slow/hung upstream trickles bytes
# (observed with reasoning models), so we cap each attempt out-of-band.
_HTTP_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                   thread_name_prefix="orq-http")

# Cap concurrent in-flight TLS requests. Many simultaneous handshakes to the
# same host were corrupting the SSL stream (SSLV3_ALERT_BAD_RECORD_MAC under the
# vision fan-out); a small gate keeps throughput high while staying clear of it.
_REQUEST_GATE = threading.Semaphore(3)

# One shared, pre-built SSL context. Building a fresh context per request is slow
# and was part of the concurrency problem; a single context reused across clients
# is the supported, thread-safe pattern.
try:
    _SSL_CTX = httpx.create_ssl_context()
except Exception:           # pragma: no cover - fall back to httpx default
    _SSL_CTX = True


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter so concurrent retries don't re-collide."""
    return min(2 ** attempt, 8) + random.uniform(0, 0.75)


# OpenRouter 402 bodies read: "...can only afford 3486. To increase..." -- when
# the request's max_tokens exceeds the remaining budget we clamp to what's
# affordable and retry, instead of throwing the whole extraction away.
_AFFORD_RE = re.compile(r"can only afford\s+(\d+)")


def _parse_affordable_tokens(body: str) -> Optional[int]:
    m = _AFFORD_RE.search(body or "")
    return int(m.group(1)) if m else None


class OpenRouterError(RuntimeError):
    pass


@dataclass
class ChatResponse:
    text: str
    model: str
    raw: dict[str, Any]
    usage: dict[str, Any] = None  # OpenRouter usage accounting (tokens + cost)


def image_to_data_url(path: str | Path) -> str:
    p = Path(path)
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class OpenRouterClient:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self.s.openrouter_api_key.strip())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.s.openrouter_api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution (optional but recommended)
            "HTTP-Referer": self.s.openrouter_referer,
            "X-Title": self.s.openrouter_app_title,
        }

    # -- message construction helpers ----------------------------------------
    @staticmethod
    def text_message(role: str, content: str) -> dict[str, Any]:
        return {"role": role, "content": content}

    @staticmethod
    def vision_message(text: str, image_paths: list[str | Path]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for ip in image_paths:
            parts.append({
                "type": "image_url",
                "image_url": {"url": image_to_data_url(ip)},
            })
        return {"role": "user", "content": parts}

    # -- core call ------------------------------------------------------------
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> ChatResponse:
        if not self.available:
            raise OpenRouterError("No OpenRouter API key configured")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # ask OpenRouter to report token usage + actual USD cost
            "usage": {"include": True},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = self.s.openrouter_base_url.rstrip("/") + "/chat/completions"
        read_to = self.s.llm_timeout_seconds
        hard_to = read_to + 20          # hard wall-clock cap > httpx read timeout
        last_err: Optional[Exception] = None
        for attempt in range(self.s.llm_max_retries):
            try:
                with _REQUEST_GATE:          # cap concurrent TLS handshakes
                    resp = _HTTP_POOL.submit(
                        self._post_once, url, payload, read_to).result(timeout=hard_to)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except (ValueError, json.JSONDecodeError) as exc:
                        # 200 with a non-JSON body (proxy/CDN page, SSE fragment)
                        last_err = OpenRouterError(f"non-JSON 200 body: {exc}")
                        time.sleep(_backoff(attempt))
                        continue
                    choice = (data.get("choices") or [{}])[0]
                    finish = choice.get("finish_reason")
                    text = (choice.get("message", {}).get("content", "")) or ""
                    if finish == "length":
                        # truncated at max_tokens -> surface so the caller can
                        # see the cause rather than a generic parse failure
                        raise OpenRouterError(
                            f"response truncated (finish_reason=length, "
                            f"max_tokens={max_tokens})")
                    if not text.strip():
                        raise OpenRouterError(
                            f"empty content (finish_reason={finish})")
                    return ChatResponse(text=text, model=model, raw=data,
                                        usage=data.get("usage") or {})
                # 402 = request bigger than remaining credit. If the body tells us
                # what we can still afford, clamp max_tokens and retry rather than
                # losing the whole document (salvages runs as credit runs low).
                if resp.status_code == 402:
                    afford = _parse_affordable_tokens(resp.text)
                    cur = payload.get("max_tokens", max_tokens)
                    if afford and afford >= 512 and afford < cur:
                        payload["max_tokens"] = max(512, afford - 128)
                        last_err = OpenRouterError(
                            f"402 budget: retrying at max_tokens={payload['max_tokens']}")
                        continue                 # immediate retry with a smaller ask
                    raise OpenRouterError(f"402 insufficient credit: {resp.text[:200]}")
                # Retry transient/server/rate-limit errors; fail fast on 4xx auth
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = OpenRouterError(f"{resp.status_code}: {resp.text[:200]}")
                    time.sleep(_backoff(attempt))
                    continue
                raise OpenRouterError(f"{resp.status_code}: {resp.text[:300]}")
            except concurrent.futures.TimeoutError:
                # hung/trickling upstream -> abandon this attempt, move on
                last_err = OpenRouterError(f"hard timeout after {hard_to}s")
                time.sleep(_backoff(attempt))
            except (httpx.TransportError, httpx.TimeoutException, ssl.SSLError) as exc:
                # transient transport/TLS error (incl. SSLV3_ALERT_BAD_RECORD_MAC
                # under concurrency) -> back off with jitter and retry
                last_err = exc
                time.sleep(_backoff(attempt))
        raise OpenRouterError(f"chat failed after retries: {last_err}")

    def _post_once(self, url: str, payload: dict, read_timeout: float):
        timeout = httpx.Timeout(read_timeout, connect=15.0)
        # HTTP/1.1 + one shared SSL context: avoids h2 multiplexing surprises and
        # per-request context churn that triggered TLS stream corruption.
        with httpx.Client(timeout=timeout, verify=_SSL_CTX, http2=False) as client:
            return client.post(url, headers=self._headers(), json=payload)

    # -- convenience: JSON-returning call ------------------------------------
    def chat_json(self, model: str, messages: list[dict[str, Any]], **kw: Any) -> Any:
        kw.setdefault("json_mode", True)
        resp = self.chat(model, messages, **kw)
        return _loads_lenient(resp.text)


def _loads_lenient(text: str) -> Any:
    """Parse JSON that may be wrapped in prose or ```json fences."""
    text = (text or "").strip()
    if not text:
        raise OpenRouterError("empty LLM response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # strip code fences
    if "```" in text:
        inner = text.split("```", 2)
        if len(inner) >= 2:
            body = inner[1]
            if body.lstrip().lower().startswith("json"):
                body = body.split("\n", 1)[1] if "\n" in body else body
            try:
                return json.loads(body.strip())
            except json.JSONDecodeError:
                pass
    # grab the largest {...} or [...] span
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise OpenRouterError(f"could not parse JSON from response: {text[:200]}")
