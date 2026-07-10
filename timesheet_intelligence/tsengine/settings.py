"""Configuration for the engine.

All knobs come from environment variables (prefix ``TSE_``) or a ``.env`` file,
plus the ``config/models.yaml`` routing table. Nothing about the provider or
model is hardcoded -- swap models by editing yaml or setting ``TSE_MODEL_*``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_YAML = PROJECT_ROOT / "config" / "models.yaml"

# per-task model overrides are read straight from the environment so we don't
# create pydantic fields named ``model_*`` (a protected namespace).
_TASKS = ("classify", "vision", "table", "normalize", "validate")

# module-level cache for the parsed routing yaml, keyed by absolute path
_ROUTING_CACHE: dict[str, dict[str, Any]] = {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TSE_",
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    # ---- access control ----
    # When set, every /api/* call (except /api/health) must send a matching
    # X-API-Key header. Lets the engine be exposed over a public tunnel safely.
    api_key: str = ""

    # ---- OpenRouter ----
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_title: str = "Timesheet Intelligence"
    openrouter_referer: str = "https://timesheet-intelligence.local"

    routing_yaml: str = str(DEFAULT_MODELS_YAML)

    # ---- flow ----
    # "premium": deterministic -> gpt-4o-mini -> selective gemini second opinion.
    # "budget":  deterministic -> FREE local LLM (Ollama) for text -> gpt-4o-mini
    #            fallback + vision; gemini hard-off. Near-zero cost, slower.
    # "direct":  send the WHOLE file to a vision LLM with one exhaustive prompt,
    #            per file, escalating nano -> mini -> gpt-5 on low confidence /
    #            self-check failure. No deterministic parsing.
    # "premium_plus": premium, PLUS the Claude-baseline technique -- when a
    #            scan/photo is under-read, do a full-image vision re-read with the
    #            exhaustive prompt on the existing gemini model and keep the better
    #            read. Same models as premium; recovers OCR under-reads (e.g. Rajani).
    flow: str = "premium"                    # TSE_FLOW

    # premium_plus: full-image vision re-read ladder (cheap gpt vision first,
    # escalate if it still under-reads) + trigger for under-read scans.
    premium_plus_vision_models: str = "openai/gpt-4o-mini,openai/gpt-5.4-mini"
    premium_plus_min_hours: float = 60.0      # below this on a scan -> vision re-read
    premium_plus_min_days: int = 6

    @property
    def premium_plus_vision_ladder(self) -> list[str]:
        return [m.strip() for m in self.premium_plus_vision_models.split(",") if m.strip()]

    # ---- direct track (flow="direct") model ladder + gates ----
    direct_primary_model: str = "openai/gpt-5.4-nano"
    direct_fallback1_model: str = "openai/gpt-5.4-mini"
    direct_fallback2_model: str = "openai/gpt-5"
    direct_min_confidence: float = 0.75       # below -> escalate to next model
    direct_autoaccept_confidence: float = 0.85  # at/above + no errors -> auto-accept
    direct_agreement_tolerance: float = 2.0   # cross-model monthly-total agreement (h)
    direct_max_pages: int = 20                # page cap per request

    use_local_llm: str = "auto"              # auto (= on in budget flow) | on | off
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "qwen2.5:7b-instruct"
    local_llm_timeout_seconds: float = 240.0  # bench worst case 165s + headroom
    local_llm_num_ctx: int = 16384            # full engine prompt fits (bench-proven)
    agent_trace: bool = True                  # per-file internal sub-agent traces

    # ---- behaviour ----
    llm_policy: str = "on_low_confidence"   # never | on_low_confidence | always
    llm_confidence_threshold: float = 0.6
    # selective second opinion: re-run only HARD files (low-confidence / flagged /
    # implausible vision reads) on this stronger model, keeping the better result.
    # "" disables it; most files stay on the cheap primary model.
    escalation_model: str = "google/gemini-2.5-pro"
    escalation_min_confidence: float = 0.6

    @property
    def is_budget(self) -> bool:
        return self.flow.strip().lower() == "budget"

    @property
    def is_direct(self) -> bool:
        return self.flow.strip().lower() == "direct"

    @property
    def is_premium_plus(self) -> bool:
        return self.flow.strip().lower() == "premium_plus"

    @property
    def direct_ladder(self) -> list[str]:
        """Ordered model ladder for the direct track (dedup, drop blanks)."""
        seen, out = set(), []
        for m in (self.direct_primary_model, self.direct_fallback1_model,
                  self.direct_fallback2_model):
            m = (m or "").strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @property
    def local_llm_enabled(self) -> bool:
        v = self.use_local_llm.strip().lower()
        if v in ("on", "true", "1"):
            return True
        if v in ("off", "false", "0"):
            return False
        return self.is_budget                 # "auto": budget=on, premium=off

    @property
    def second_opinion_model(self) -> str:
        """Gemini tier is hard-off in the budget flow."""
        return "" if self.is_budget else self.escalation_model
    use_local_ocr: bool = True
    # OCR engine for scanned/image timesheets: "auto" prefers PaddleOCR when it's
    # installed (far better on faint/light-text grids: ~98 vs ~75 confidence),
    # falling back to tesseract; "tesseract" or "paddle" force one.
    ocr_engine: str = "auto"
    # IBM Docling (TableFormer TSR) as a fallback PDF table extractor, used only
    # when pdfplumber finds NO tables. OPT-IN (default off): A/B testing showed it
    # extracts materially better grids, but enabling it by default can short-circuit
    # to a wrong partial summary and suppress the LLM that would resolve it
    # correctly. Flip on (TSE_USE_DOCLING=1) once the normalizer's table-selection
    # + confidence-gating is tuned and validated against live LLM. See README.
    use_docling: bool = False
    tesseract_cmd: str = "tesseract"
    ocr_dpi: int = 220
    # mean tesseract word confidence (0-100) above which the OCR-layout text is
    # used to GROUND the vision model (exact cell values); below it the model
    # reads the image freely (poor OCR would otherwise suppress real reads).
    # Bench on 6 real scans: tesseract's worst CONTENT failures scored 65-75 conf
    # (never escalated at the old 55 threshold) while its only good read scored
    # 88 -- clean separation at ~80. Below this, PaddleOCR takes over.
    ocr_ground_min_confidence: float = 80.0
    max_pdf_pages: int = 60          # cap rasterization/OCR per file (DoS guard)
    max_excel_cols: int = 64         # cap columns materialized per sheet
    llm_timeout_seconds: float = 120.0   # gemini-2.5-pro reasoning can run long
    llm_max_retries: int = 3

    # ---- calendar / display policy ----
    holiday_region: str = "US"
    extra_holidays: str = ""
    weekend_days: str = "5,6"               # 0=Mon .. 6=Sun
    max_hours_per_day: float = 16.0

    # ---- paths ----
    output_dir: str = "output"

    # -- derived helpers ------------------------------------------------------
    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key.strip()) and self.llm_policy != "never"

    @property
    def weekend_set(self) -> set[int]:
        out: set[int] = set()
        for tok in str(self.weekend_days).split(","):
            tok = tok.strip()
            if tok.isdigit():
                out.add(int(tok))
        return out or {5, 6}

    @property
    def extra_holiday_list(self) -> list[str]:
        return [d.strip() for d in str(self.extra_holidays).split(",") if d.strip()]

    @property
    def output_path(self) -> Path:
        p = Path(self.output_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -- model routing table --------------------------------------------------
    def _routing_table(self) -> dict[str, Any]:
        path = Path(self.routing_yaml)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        key = str(path)
        if key in _ROUTING_CACHE:
            return _ROUTING_CACHE[key]
        if not path.exists():
            data: dict[str, Any] = {"defaults": {}, "tasks": {}}
        else:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except (yaml.YAMLError, OSError):
                # a malformed config must not crash health checks / processing
                data = {"defaults": {}, "tasks": {}}
        _ROUTING_CACHE[key] = data
        return data

    def models_for(self, task: str) -> list[str]:
        """Ordered list of candidate model ids for a task (env override first)."""
        override = os.environ.get(f"TSE_MODEL_{task.upper()}", "").strip()
        table = self._routing_table()
        candidates = list(((table.get("tasks") or {}).get(task) or {}).get("candidates") or [])
        if override:
            return [override] + [c for c in candidates if c != override]
        return candidates

    def task_params(self, task: str) -> dict[str, Any]:
        table = self._routing_table()
        params = dict(table.get("defaults") or {})
        params.update({k: v for k, v in (((table.get("tasks") or {}).get(task)) or {}).items()
                       if k != "candidates"})
        return params


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings(**overrides: Any) -> Settings:
    """Mostly for tests: build a fresh Settings with explicit overrides."""
    get_settings.cache_clear()
    _ROUTING_CACHE.clear()
    if overrides:
        for k, v in overrides.items():
            os.environ[f"TSE_{k.upper()}"] = str(v)
    return get_settings()
