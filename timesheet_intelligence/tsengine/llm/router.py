"""Task-aware model router.

Each *task* (classify / vision / table / normalize / validate) maps to an
ordered list of OpenRouter model candidates. ``run`` tries them in order,
skipping models that have already failed this session, and returns the first
success. This is what makes the system model-agnostic: code asks for a *task*,
never a specific model.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from ..settings import Settings, get_settings
from .client import OpenRouterClient, OpenRouterError, _loads_lenient

log = logging.getLogger("tsengine.llm")


@dataclass
class LLMResult:
    ok: bool
    task: str
    model: Optional[str] = None
    data: Any = None              # parsed JSON (or text)
    text: str = ""
    error: Optional[str] = None
    attempts: list[str] = field(default_factory=list)


class ModelRouter:
    def __init__(self, settings: Optional[Settings] = None,
                 client: Optional[OpenRouterClient] = None):
        self.s = settings or get_settings()
        self.client = client or OpenRouterClient(self.s)
        self._dead: set[str] = set()   # models that errored this session
        self._lock = threading.Lock()  # guards counters when pages run in parallel
        self.calls: int = 0
        # cost / token accounting (populated from OpenRouter usage)
        self.total_cost: float = 0.0
        self.total_tokens: int = 0
        self.cost_by_model: dict[str, float] = {}
        self.tokens_by_model: dict[str, int] = {}
        self.calls_by_model: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.client.available and self.s.llm_policy != "never"

    def _record(self, model: str, usage: Optional[dict]) -> None:
        usage = usage or {}
        toks = int(usage.get("total_tokens") or 0)
        cost = float(usage.get("cost") or 0.0)
        with self._lock:
            self.total_tokens += toks
            self.total_cost += cost
            self.tokens_by_model[model] = self.tokens_by_model.get(model, 0) + toks
            self.cost_by_model[model] = self.cost_by_model.get(model, 0.0) + cost
            self.calls_by_model[model] = self.calls_by_model.get(model, 0) + 1

    def usage_summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost, 6),
            "by_model": {
                m: {"calls": self.calls_by_model.get(m, 0),
                    "tokens": self.tokens_by_model.get(m, 0),
                    "cost_usd": round(self.cost_by_model.get(m, 0.0), 6)}
                for m in self.calls_by_model
            },
        }

    def run(
        self,
        task: str,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = True,
    ) -> LLMResult:
        if not self.enabled:
            return LLMResult(ok=False, task=task, error="llm disabled / no api key")

        params = self.s.task_params(task)
        temperature = float(params.get("temperature", 0.0))
        max_tokens = int(params.get("max_tokens", 4096))
        json_mode = json_mode and bool(params.get("response_format_json", True))

        attempts: list[str] = []
        last_err: Optional[str] = None
        for model in self.s.models_for(task):
            if model in self._dead:
                continue
            attempts.append(model)
            try:
                with self._lock:
                    self.calls += 1
                resp = self.client.chat(
                    model, messages, temperature=temperature, max_tokens=max_tokens,
                    json_mode=json_mode)
                self._record(model, resp.usage)   # capture tokens + cost even before parse
                if json_mode:
                    return LLMResult(ok=True, task=task, model=model,
                                     data=_loads_lenient(resp.text), attempts=attempts)
                return LLMResult(ok=True, task=task, model=model, data=resp.text,
                                 text=resp.text, attempts=attempts)
            except OpenRouterError as exc:
                last_err = str(exc)
                log.warning("model %s failed for task %s: %s", model, task, exc)
                # Blacklist models with persistent per-model failures so we don't
                # retry them on every file: auth, missing route, or capability
                # errors (e.g. a model that rejects response_format json_object).
                low = last_err.lower()
                if any(s in low for s in (
                        "401", "403", "no endpoints", "not a valid model",
                        "response_format", "not supported", "does not support")):
                    self._dead.add(model)
        return LLMResult(ok=False, task=task, error=last_err or "no candidate models",
                         attempts=attempts)
