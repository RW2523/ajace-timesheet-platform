#!/usr/bin/env python3
"""Score one or more flows against the golden set (tests/golden_set.json).

Usage:
    TSE_OPENROUTER_API_KEY=... python scripts/eval_flows.py direct premium budget
    python scripts/eval_flows.py direct           # just one flow

For each flow it runs every golden file through the full engine and reports:
exact-total rate, within-2h rate, days accuracy, total cost, wall-clock time,
and a per-file diff. This is the acceptance gate: the direct track should ship as
default only if it beats premium on exact-total rate at acceptable cost.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GOLD = json.loads((ROOT / "tests" / "golden_set.json").read_text())


def _find(name: str):
    for root in GOLD["roots"]:
        hits = list(Path(root).rglob(name))
        if hits:
            return hits[0]
    return None


def run_flow(flow: str):
    os.environ["TSE_FLOW"] = flow
    # fresh import so settings pick up the flow env each run
    for m in [k for k in list(sys.modules) if k.startswith("tsengine")]:
        del sys.modules[m]
    from tsengine.pipeline import process_folder

    rows, exact, within2, dayok, cost = [], 0, 0, 0, 0.0
    t0 = time.time()
    for c in GOLD["cases"]:
        p = _find(c["file"])
        if not p:
            rows.append((c["file"], "MISSING", "-", "-")); continue
        tmp = Path(tempfile.mkdtemp()); shutil.copy(p, tmp)
        try:
            rep = process_folder(tmp, c["month"], c["year"])
            d = json.loads(rep.model_dump_json())
            cost += d.get("llm_cost_usd") or 0
            emps = d.get("employees", [])
            got = max((e.get("monthly_total") or 0 for e in emps), default=0)
            gotd = max((e.get("days_worked") or 0 for e in emps), default=0)
            rv = emps[0].get("review_status") if emps else "-"
        except Exception as exc:
            rows.append((c["file"], f"ERR {exc}", "-", "-")); continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        diff = abs(got - c["total"])
        if diff < 0.5: exact += 1
        if diff <= 2.0: within2 += 1
        if c.get("days") and gotd == c["days"]: dayok += 1
        mark = "OK" if diff < 0.5 else ("~" if diff <= 2 else "X")
        rows.append((c["file"][:34], f"{got:g}/{c['total']:g} {mark}", f"{gotd}d", rv))
    n = len(GOLD["cases"])
    return {"flow": flow, "n": n, "exact": exact, "within2": within2, "dayok": dayok,
            "cost": cost, "secs": round(time.time() - t0), "rows": rows}


def main():
    flows = sys.argv[1:] or ["premium"]
    results = [run_flow(f) for f in flows]
    for r in results:
        print(f"\n{'='*64}\nFLOW: {r['flow']}  ({r['n']} files)\n{'='*64}")
        for f, tot, dd, rv in r["rows"]:
            print(f"  {f:34} {tot:16} {dd:5} {rv}")
        print(f"  --> exact {r['exact']}/{r['n']}  within2h {r['within2']}/{r['n']}  "
              f"days {r['dayok']}/{r['n']}  cost ${r['cost']:.3f}  {r['secs']}s")
    print(f"\n{'='*64}\nSUMMARY\n{'='*64}")
    print(f"  {'flow':10} {'exact':>7} {'within2h':>9} {'cost':>8} {'time':>7}")
    for r in results:
        print(f"  {r['flow']:10} {r['exact']}/{r['n']:>5} {r['within2']}/{r['n']:>7} "
              f"${r['cost']:>6.3f} {r['secs']:>5}s")


if __name__ == "__main__":
    main()
