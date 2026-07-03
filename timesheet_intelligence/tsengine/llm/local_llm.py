"""Local LLM client (Ollama) — the FREE normalizer for the budget flow.

Talks to Ollama's OpenAI-compatible endpoint (http://localhost:11434/v1) so the
same chat-message/JSON-contract prompts used with OpenRouter work unchanged.
Text-only: local models here have no vision, so image/scan normalization always
stays on the cloud vision path.

Fails soft everywhere: if Ollama isn't running or the model isn't pulled,
available() is False and callers fall through to the cloud model.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import httpx

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"checked_at": 0.0, "ok": False, "models": []}
_CHECK_TTL = 60.0          # re-probe Ollama at most once a minute

DEFAULT_BASE = "http://localhost:11434"


def available(base_url: str = DEFAULT_BASE, model: str = "") -> bool:
    """True when Ollama is up (and, if given, the model is pulled). Cached."""
    now = time.time()
    with _LOCK:
        if now - _STATE["checked_at"] > _CHECK_TTL:
            try:
                r = httpx.get(f"{base_url}/api/tags", timeout=3.0)
                tags = [m.get("name", "") for m in (r.json().get("models") or [])]
                _STATE.update(checked_at=now, ok=r.status_code == 200, models=tags)
            except Exception:
                _STATE.update(checked_at=now, ok=False, models=[])
        if not _STATE["ok"]:
            return False
        if model:
            want = model if ":" in model else model + ":latest"
            return any(t == model or t == want or t.startswith(model + ":")
                       for t in _STATE["models"])
        return True


def chat_json(model: str, messages: list[dict[str, Any]], *,
              base_url: str = DEFAULT_BASE, temperature: float = 0.0,
              max_tokens: int = 4096, timeout: float = 240.0,
              num_ctx: int = 16384) -> tuple[Any, int]:
    """Chat via Ollama's NATIVE /api/chat; returns (parsed_json, total_tokens).

    Native endpoint (not /v1) because only it accepts options.num_ctx -- with the
    default 4096 context our ~4K-token prompts silently truncate and the model
    hallucinates dates while still returning valid JSON (observed in bench).
    format="json" enables grammar-constrained JSON decoding (3/3 valid in bench).
    Raises on transport errors or unparseable output — callers treat any raise
    as "local model unavailable" and fall back to the cloud model.
    """
    payload = {
        "model": model,
        "messages": messages,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": max_tokens,
        },
    }
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
        resp = client.post(f"{base_url}/api/chat", json=payload)
    resp.raise_for_status()
    body = resp.json()
    text = (body.get("message") or {}).get("content", "")
    toks = int(body.get("prompt_eval_count") or 0) + int(body.get("eval_count") or 0)
    return _loads_lenient(text), toks


def _loads_lenient(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty local-LLM response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:                          # fenced output
        parts = text.split("```")
        if len(parts) >= 2:
            body = parts[1]
            if body.lstrip().lower().startswith("json"):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            try:
                return json.loads(body.strip())
            except json.JSONDecodeError:
                pass
    for oc, cc in (("{", "}"), ("[", "]")):    # largest JSON span
        i, j = text.find(oc), text.rfind(cc)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"could not parse JSON from local LLM: {text[:160]}")
