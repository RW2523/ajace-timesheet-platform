"""Command-line entrypoint.

    python -m tsengine.cli --folder "<path>" --month 4 --year 2026
    python -m tsengine.cli --folder "<path>" --month 4 --year 2026 --serve

Writes a JSON report to the output dir and prints a summary. ``--serve`` also
launches the API + calendar UI pointed at the generated report.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import process_folder
from .settings import get_settings


def _print_summary(report) -> None:
    s = report.stats()
    print("\n" + "=" * 64)
    print(f" Timesheet report — {report.year}-{report.month:02d}")
    print("=" * 64)
    print(f" folder           : {report.folder}")
    print(f" files seen/proc  : {s['files_seen']} / {s['files_processed']}")
    print(f" employees        : {s['employees']}")
    print(f" total hours      : {s['total_hours']}")
    print(f" issues flagged   : {s['issues']}")
    print(f" unprocessed      : {s['unprocessed']}")
    print(f" LLM used         : {s['llm_used']}")
    if report.llm_calls:
        print(f" LLM calls        : {report.llm_calls}")
        print(f" LLM tokens       : {report.llm_tokens:,}")
        print(f" OpenRouter cost  : ${report.llm_cost_usd:.4f}")
        for m, u in report.llm_usage_by_model.items():
            print(f"    - {m:28} {u['calls']:>3} calls  {u['tokens']:>8,} tok  ${u['cost_usd']:.4f}")
    print("-" * 64)
    for em in report.employees:
        flags = len(em.all_issues)
        client = ", ".join(em.clients) or "—"
        print(f"  {(em.employee_name or 'UNKNOWN'):28} "
              f"{em.monthly_total:7.2f}h  (R {em.monthly_regular:.1f} / "
              f"OT {em.monthly_overtime:.1f})  [{client}]  "
              f"flags={flags} conf={em.confidence}")
    if report.unprocessed:
        print("-" * 64)
        print(" Unprocessed:")
        for u in report.unprocessed:
            print(f"   - {u.file}: {u.reason}")
    print("=" * 64 + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Timesheet Intelligence Core Engine")
    ap.add_argument("--folder", required=True, help="folder of timesheets")
    ap.add_argument("--month", type=int, required=True, help="target month (1-12)")
    ap.add_argument("--year", type=int, required=True, help="target year")
    ap.add_argument("--out", default=None, help="output JSON path")
    ap.add_argument("--serve", action="store_true", help="launch UI after processing")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    # pdfminer is extremely chatty about malformed font descriptors
    for noisy in ("pdfminer", "pdfplumber", "PIL", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    folder = Path(args.folder)
    if not folder.exists():
        print(f"error: folder not found: {folder}", file=sys.stderr)
        return 2
    if not (1 <= args.month <= 12):
        print("error: --month must be 1-12", file=sys.stderr)
        return 2

    settings = get_settings()
    print(f"Processing {folder} for {args.year}-{args.month:02d} "
          f"(LLM {'enabled' if settings.llm_enabled else 'disabled — deterministic only'}) ...")
    report = process_folder(folder, args.month, args.year, settings)

    out_path = Path(args.out) if args.out else (
        settings.output_path / f"report_{args.year}_{args.month:02d}.json")
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    # also write a stable 'latest' the UI loads by default
    (settings.output_path / "latest_report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8")
    _print_summary(report)
    print(f"Report written to {out_path}")

    if args.serve:
        from .api.app import serve
        serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
