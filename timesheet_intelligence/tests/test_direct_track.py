"""Direct track (flow='direct') exercised with a mock model client -- no API key.

Covers: file->model contract mapping, the escalation gate, cross-model
disagreement, month-level sanity checks, and human-review routing.
"""
import datetime as dt
import threading

from tsengine.direct.extractor import DirectExtractor
from tsengine.llm.client import ChatResponse
from tsengine.schema import EmployeeMonth, FileKind, Issue, IssueCode, IssueSeverity
from tsengine.settings import Settings
from tsengine.validate.validator import Validator


def _mega(total_days=17, conf=0.9, matches=True, discs=None, is_ts=True):
    entries = [{"date": f"2026-04-{d:02d}", "regular_hours": 8, "overtime_hours": 0,
                "total_hours": 8} for d in range(1, total_days + 1)]
    return {
        "document_type": "timesheet" if is_ts else "invoice",
        "is_timesheet": is_ts,
        "employee_name": "Elangovan Krishnaswamy", "client": "HCPSS",
        "entries": entries, "weekly_totals": [],
        "stated_total": total_days * 8.0, "confidence": conf,
        "self_check": {"sum_of_daily_totals": total_days * 8.0,
                       "matches_stated_total": matches,
                       "per_day_reg_ot_consistent": True,
                       "discrepancies": discs or []},
        "ambiguities": [], "handwritten_or_faint": False,
    }


class _FakeClient:
    """Returns a scripted mega-contract per model id."""
    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    def file_message(self, text, file_path=None, images=None):
        return {"role": "user", "content": text}

    def chat(self, model, messages, **kw):
        self.calls.append(model)
        import json
        data = self.by_model.get(model, self.by_model.get("*"))
        return ChatResponse(text=json.dumps(data), model=model, raw={},
                            usage={"total_tokens": 100, "cost": 0.001})


class _FakeRouter:
    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._dead = set()
        self.calls = 0

    def _record(self, model, usage):
        pass


def _extractor(by_model, **over):
    s = Settings(flow="direct", **over)
    return DirectExtractor(_FakeRouter(_FakeClient(by_model)), s), s


def test_direct_maps_contract_and_stops_at_primary():
    ext, s = _extractor({"*": _mega(conf=0.9)})
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res and len(res) == 1
    r = res[0]
    assert r.employee_name == "Elangovan Krishnaswamy"
    assert sum(e.total for e in r.entries) == 136.0
    assert r.method == "direct:openai/gpt-5.4-nano"          # never escalated
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]


def test_direct_escalates_on_low_confidence():
    ext, s = _extractor({
        "openai/gpt-5.4-nano": _mega(conf=0.4),
        "openai/gpt-5.4-mini": _mega(conf=0.92),
    })
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res[0].method == "direct:openai/gpt-5.4-mini"     # climbed one rung
    assert ext.router.client.calls[:2] == ["openai/gpt-5.4-nano", "openai/gpt-5.4-mini"]


def test_direct_self_check_mismatch_routes_to_review_not_escalation():
    # confident but the doc's stated total != daily sum -> keep primary, note it
    # (validator's TOTAL_MISMATCH warning routes to needs_review); do NOT escalate
    # and do NOT block on the self-check alone.
    ext, s = _extractor({"*": _mega(conf=0.9, matches=False, discs=["stated 168 != sum 176"])})
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert ext.router.client.calls == ["openai/gpt-5.4-nano"]  # did NOT escalate
    assert res[0].needs_llm is False                           # self-check alone != block
    assert any("self-check" in n for n in res[0].notes)        # but it IS surfaced


def test_direct_rejects_non_timesheet():
    ext, s = _extractor({"*": _mega(is_ts=False)})
    res = ext.extract("invoice.pdf", "invoice.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res is None


def test_direct_flags_cross_model_disagreement():
    ext, s = _extractor({
        "openai/gpt-5.4-nano": _mega(total_days=17, conf=0.5),   # 136h, escalates
        "openai/gpt-5.4-mini": _mega(total_days=20, conf=0.5),   # 160h, escalates
        "openai/gpt-5": _mega(total_days=22, conf=0.5),          # 176h
    })
    res = ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    assert res[0].needs_llm is True
    assert any("disagreed" in n for n in res[0].notes)


# -- validator: month checks + review routing --------------------------------
def _em(total, days, conf, ot=0.0):
    return EmployeeMonth(month=4, year=2026, monthly_total=total, monthly_regular=total - ot,
                         monthly_overtime=ot, days_worked=days, confidence=conf)


def test_month_check_flags_impossible_daycount():
    em = _em(300, 25, 0.9)                    # April 2026 has 22 weekdays
    Validator().validate(em)
    assert any(i.code == IssueCode.INVALID and i.severity == IssueSeverity.ERROR
               for i in em.issues)
    assert em.review_status == "blocked"


def test_month_check_flags_high_total():
    em = _em(240, 20, 0.9)
    Validator().validate(em)
    assert any("unusually high" in i.message for i in em.issues)


def test_review_routing_autoaccept():
    em = _em(136, 17, 0.95)
    Validator().validate(em)
    assert em.review_status == "auto_accepted"


def test_review_routing_needs_review_on_medium_confidence():
    em = _em(136, 17, 0.7)                    # below autoaccept 0.85, no errors
    Validator().validate(em)
    assert em.review_status == "needs_review"


# --- step 4: consensus prompt clauses + blindness guard ------------------- #
def test_direct_prompt_carries_consensus_clauses():
    from tsengine.llm.prompts import direct_extract_system
    p = direct_extract_system(5, 2026)
    # month-clip, portal no-double-count, printed-total/excluded-row, approver
    assert "MONTH BOUNDARY" in p
    assert "NEVER DOUBLE-COUNT A PERIOD" in p
    assert "PRINTED TOTAL vs EXCLUDED ROWS" in p
    assert "APPROVER" in p and "manager_name" in p
    # the boundary clause must steer a crossing period total away from stated_total
    assert "stated_total null" in p or "leave stated_total null" in p


def test_direct_read_is_blind_of_any_candidate():
    # BLINDNESS: the model is never shown a pre-computed/deterministic total, so
    # its read cannot be biased by one. The per-file instruction is a fixed string
    # with no numbers, and the system prompt is a pure function of (month, year).
    captured = {}

    class _Rec(_FakeClient):
        def chat(self, model, messages, **kw):
            captured["messages"] = messages
            return super().chat(model, messages, **kw)

    ext = DirectExtractor(_FakeRouter(_Rec({"*": _mega(conf=0.9)})),
                          Settings(flow="direct"))
    ext.extract("el.pdf", "el.pdf", 4, 2026, None, None, FileKind.PDF_NATIVE)
    msgs = captured["messages"]
    user = [m for m in msgs if m["role"] == "user"][0]
    assert user["content"] == "Extract the full mega-contract JSON for this document now."
    assert "candidate" not in str(msgs).lower()
    # the system prompt depends only on month/year, not on any prior read
    from tsengine.llm.prompts import direct_extract_system
    assert msgs[0]["content"] == direct_extract_system(4, 2026)
