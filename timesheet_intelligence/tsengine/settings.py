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

    # ---- behaviour ----
    llm_policy: str = "on_low_confidence"   # never | on_low_confidence | always
    llm_confidence_threshold: float = 0.6
    # selective second opinion: re-run only HARD files (low-confidence / flagged /
    # implausible vision reads) on this stronger model, keeping the better result.
    # "" disables it; most files stay on the cheap primary model.
    escalation_model: str = "google/gemini-2.5-pro"
    escalation_min_confidence: float = 0.6
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
    ocr_ground_min_confidence: float = 55.0
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
