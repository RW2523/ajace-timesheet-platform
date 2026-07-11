"""Consensus flow (flow='consensus'): $0 printed-total exit + the two-key gate.

The gate and the $0 exit are pure decision logic -- tested directly, no model.
A mock-stubbed _process_consensus checks the orchestration wiring end to end.
"""
import datetime as dt

from tsengine.normalize.normalizer import DayEntry, NormResult
from tsengine.pipeline import TimesheetPipeline
from tsengine.schema import (ExtractionQuality, FileKind, RawExtraction,
                             SourceRef)
from tsengine.settings import Settings


def _pipe():
    return TimesheetPipeline(Settings(flow="consensus", llm_policy="never"))


def _grid(total_per_day=8.0, days=20, method="daily_grid", stated=None, conf=0.9):
    ents = [DayEntry(date=dt.date(2026, 5, d), total=total_per_day,
                     regular=total_per_day, overtime=0.0) for d in range(1, days + 1)]
    return NormResult(file="f", method=method, quality=ExtractionQuality.NATIVE,
                      entries=ents, stated_total=stated, confidence=conf)


def _stated_only(total, method="summary_total", conf=0.6):
    return NormResult(file="f", method=method, quality=ExtractionQuality.NATIVE,
                      stated_total=total, confidence=conf)


_noop = lambda *a, **k: None


# --- S2: $0 printed-total exit -------------------------------------------- #
def test_printed_total_exit_confirms_matching_sum():
    p = _pipe()
    r = _grid(total_per_day=8.0, days=20, stated=160.0)   # sum 160 == stated 160
    assert p._printed_total_confirms(r, 5, 2026)


def test_printed_total_exit_rejects_mismatch():
    p = _pipe()
    r = _grid(total_per_day=8.0, days=20, stated=176.0)   # sum 160 != stated 176
    assert not p._printed_total_confirms(r, 5, 2026)


def test_printed_total_exit_needs_daily_evidence():
    p = _pipe()
    # a stated total with NO daily grid is not an independent second derivation
    assert not p._printed_total_confirms(_stated_only(160.0), 5, 2026)


# --- S4: consensus gate --------------------------------------------------- #
def test_gate_confirms_when_two_keys_agree():
    p = _pipe()
    a = _grid(days=20)                 # 160h deterministic
    b = _grid(days=20, method="direct:openai/gpt-5.4-nano")  # 160h model
    out = p._consensus_gate(a, b, 5, 2026, _noop)
    assert round(sum(e.total for e in out.entries), 1) == 160.0
    assert out.confidence >= 0.9 and not out.needs_llm
    assert any("CONFIRMED" in n for n in out.notes)


def test_gate_ratio_veto_keeps_higher_on_disagreement():
    p = _pipe()
    a = _grid(days=20)                                  # 160h
    b = _grid(days=5, method="direct:openai/gpt-5.4-nano")  # 40h (< 60% of 160)
    out = p._consensus_gate(a, b, 5, 2026, _noop)
    assert round(sum(e.total for e in out.entries), 1) == 160.0   # higher wins
    assert out.confidence <= 0.65
    assert any("DISAGREEMENT" in n for n in out.notes)


def test_gate_low_total_lock_needs_both_day_level():
    p = _pipe()
    # both say 40h but Key B is stated-only -> below 60h without two day-level
    # derivations -> not confirmed
    a = _grid(total_per_day=8.0, days=5)               # 40h grid
    b = _stated_only(40.0, method="direct:openai/gpt-5.4-nano")
    out = p._consensus_gate(a, b, 5, 2026, _noop)
    assert out.confidence <= 0.65                       # review, not auto-accept
    assert not any("CONFIRMED" in n for n in out.notes)


def test_gate_ceiling_blocks_absurd_agreement():
    p = _pipe()
    a = _grid(total_per_day=12.0, days=21)             # 252h
    b = _grid(total_per_day=12.0, days=21, method="direct:openai/gpt-5.4-nano")
    out = p._consensus_gate(a, b, 5, 2026, _noop)
    assert out.confidence <= 0.65                       # >230h never auto-accepts
    assert not any("CONFIRMED" in n for n in out.notes)


def test_gate_single_derivation_is_unconfirmed():
    p = _pipe()
    out = p._consensus_gate(_grid(days=20), None, 5, 2026, _noop)
    assert out.confidence <= 0.7
    assert any("only one derivation" in n for n in out.notes)


def test_gate_none_when_no_data():
    p = _pipe()
    assert p._consensus_gate(None, None, 5, 2026, _noop) is None


# --- orchestration: _process_consensus wiring (mock-stubbed) -------------- #
def test_process_consensus_dollar_zero_exit(monkeypatch):
    p = _pipe()
    raw = RawExtraction(file="f.xlsx", kind=FileKind.EXCEL)
    monkeypatch.setattr(p.orch, "extract", lambda path, det: raw)
    monkeypatch.setattr(p.normalizer, "normalize",
                        lambda raw, m, y, ch: [_grid(days=20, stated=160.0)])
    called = {"direct": False}

    class _Det:
        kind = FileKind.EXCEL
    # if the model were called this would flip -> assert it stays False
    monkeypatch.setattr(p, "_direct", type("D", (), {
        "extract": lambda *a, **k: called.__setitem__("direct", True) or None})())
    report = type("R", (), {"unprocessed": []})()
    out = p._process_consensus("f.xlsx", "f.xlsx", 5, 2026, None, _Det(),
                               report, _noop, _noop)
    assert out and round(sum(e.total for e in out[0].entries), 1) == 160.0
    assert called["direct"] is False          # printed-total match -> no model call
    assert out[0].confidence >= 0.9
