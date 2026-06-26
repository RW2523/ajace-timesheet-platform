"""Normalization: raw extractions -> canonical EmployeeMonth records.

Deterministic strategies handle the structured majority (daily grids, weekly
totals, weekday matrices, time-in/out). The LLM normalizer handles the rest
(scanned/handwritten/odd layouts) when OpenRouter is configured.
"""
from .normalizer import Normalizer

__all__ = ["Normalizer"]
