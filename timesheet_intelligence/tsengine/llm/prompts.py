"""Prompt templates for each LLM task.

The contract the model must follow is identical regardless of input modality
(text, table dump, or image) so that downstream parsing is uniform. All prompts
ask for strict JSON matching the canonical day-record shape.
"""
from __future__ import annotations

import calendar
import json
from typing import Any

# Canonical JSON contract the model must emit for extraction/normalization.
CANONICAL_CONTRACT = """
Return ONLY a JSON object with this exact shape:

{
  "employee_name": string | null,
  "employee_id": string | null,
  "client": string | null,            // client / company the work was for
  "project": string | null,           // project name or id if present
  "entries": [
    {
      "date": "YYYY-MM-DD",           // a specific calendar date
      "regular_hours": number | null,
      "overtime_hours": number | null,
      "total_hours": number | null,   // if only one number is given, put it here
      "project": string | null,
      "note": string | null,
      "raw": string | null            // the literal text you read this from
    }
  ],
  "weekly_totals": [                   // ONLY if the source gives weekly (not daily) numbers
    {
      "week_start": "YYYY-MM-DD",
      "week_end": "YYYY-MM-DD",
      "regular_hours": number | null,
      "overtime_hours": number | null,
      "total_hours": number | null
    }
  ],
  "stated_total": number | null,       // a MONTH total stated in prose with NO
                                        // daily/weekly breakdown (e.g. an approval
                                        // email "Approved 24 hours for April")
  "notes": [string],                   // anything ambiguous, unclear, or worth auditing
  "confidence": number                 // 0..1 your overall confidence
}

Rules:
- employee_name is the PERSON whose time this is -- the worker / contractor /
  consultant (often near "Employee", "Name", "Contractor", or a signature line).
  NEVER use a company name, a street address (e.g. "123 Main Street, Anytown"),
  a city, a project, or the APPROVER / MANAGER / "Engagement Manager" name. If no
  person name is visible, use null.
- READ EVERY WEEK / EVERY PAGE / THE WHOLE CALENDAR. Many timesheets stack
  several weekly grids (one per week, often one per page) or a full month grid.
  Output an entry for EVERY day that has hours across ALL of them -- do not stop
  after the first week and do not return only a single weekly subtotal.
- Use the EXACT dates printed in the document (including any week-ending date).
  If the document's dates are for a DIFFERENT month than requested, return an
  empty entries list -- never shift dates into the requested month.
- Use 24h-correct decimal hours (e.g. 7:30am-4:30pm with 1h lunch = 8.0).
- "8 00" or "8:00" in a single hours cell usually means 8 hours, not 8 minutes.
- Times like In=09:00 Out=05:00 almost always mean 9am-5pm = 8 hours.
- PROJECT MATRICES: if the sheet lists several projects/tasks as rows with
  weekday columns (Sun..Sat) of hours, then for each calendar date the day's
  total_hours is the SUM of that date's hours across EVERY project/task row --
  go row by row through ALL of them (there may be 10+); do not stop early and do
  not report only the first project's hours. Numbers under weekday headers are
  HOURS for that project that day (a "1" means 1 hour, a "5" means 5 hours) --
  never a count. Map weekday columns to real dates using the period/week-start
  shown on the sheet. Double-check each day's sum before emitting it.
- One entry PER CALENDAR DATE (already aggregated across projects), not one per
  project row. Put the main project/client in 'project'.
- Only include entries whose date falls in the requested month/year.
- CRITICAL: include a date ONLY if that exact date is actually shown in the
  source with hours. Do NOT extrapolate, fill, or assume hours for days/weeks
  that are not present (e.g. if only weeks 11-17, 18-24, 25-30 are shown, do
  NOT invent days 1-10). Blank/empty cells are 0 or null, never a guessed 8.
- If a value is blank/illegible, use null and add a note. Never invent numbers.
- If only weekly totals exist, fill weekly_totals and leave entries empty.
- Separate overtime from regular only when the source explicitly distinguishes
  them; otherwise put everything in total_hours.
"""


def _month_context(month: int, year: int) -> str:
    name = calendar.month_name[month]
    ndays = calendar.monthrange(year, month)[1]
    return (f"Target period: {name} {year} (month={month:02d}, {ndays} days, "
            f"{year}-{month:02d}-01 .. {year}-{month:02d}-{ndays:02d}). "
            f"Dates may be written D/M/Y or M/D/Y; infer the order from context so "
            f"that the dates land in {name} {year}.")


# --------------------------------------------------------------------------- #
def classify_messages(text_sample: str) -> list[dict[str, Any]]:
    sys = ("You classify timesheet documents. Reply with JSON: "
           '{"layout": one of '
           '["daily_grid","weekly_grid","weekly_totals_only","timecard_entries",'
           '"project_matrix","time_in_out","unknown"], '
           '"has_overtime_column": bool, "date_format_hint": "DMY"|"MDY"|"unknown", '
           '"employee_name": string|null, "client": string|null, '
           '"notes": string}.')
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Document sample:\n```\n{text_sample[:4000]}\n```"},
    ]


def normalize_text_messages(text: str, tables_dump: str, month: int, year: int) -> list[dict[str, Any]]:
    sys = ("You are a meticulous timesheet normalizer for a consulting company. "
           "You convert one employee's timesheet (any layout/client format) into a "
           "standard structure. " + _month_context(month, year) + CANONICAL_CONTRACT)
    user = "RAW TEXT:\n```\n" + text[:9000] + "\n```\n"
    if tables_dump:
        user += "\nDETECTED TABLES:\n```\n" + tables_dump[:9000] + "\n```\n"
    user += "\nProduce the JSON now."
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def vision_normalize_prompt(month: int, year: int) -> str:
    return ("You are reading ONE page of a scanned/photographed timesheet image "
            "(it may be handwritten, a calendar grid, or a screenshot). Extract "
            "EVERY day's hours visible on this page. If it is a weekly grid, "
            "output all its days; if it is a month calendar, sum each day's "
            "cell(s). Do not return only a weekly/period subtotal. "
            + _month_context(month, year) + CANONICAL_CONTRACT +
            "\nRead carefully; if a cell is unreadable use null and add a note. "
            "Ignore any selected-day 'Total' side panel -- it is not the month total.")


def reconcile_messages(employee: str, conflict_blob: str, month: int, year: int) -> list[dict[str, Any]]:
    sys = ("You resolve conflicts between multiple timesheet sources for the same "
           "employee and month. " + _month_context(month, year) +
           ' Return JSON: {"resolutions":[{"date":"YYYY-MM-DD",'
           '"regular_hours":number|null,"overtime_hours":number|null,'
           '"total_hours":number|null,"reason":string,"chosen_source":string}],'
           '"notes":[string]}. Prefer the more authoritative/specific source; '
           "never average away a real conflict without explaining it.")
    user = f"Employee: {employee}\nConflicting data:\n```\n{conflict_blob[:8000]}\n```"
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def dump_tables_for_prompt(tables: list[Any]) -> str:
    """Render RawTable list compactly for a text prompt."""
    out = []
    for t in tables[:12]:
        title = getattr(t, "title", None) or ""
        headers = getattr(t, "headers", []) or []
        rows = getattr(t, "rows", []) or []
        out.append(f"# table {title}".rstrip())
        if headers:
            out.append(" | ".join(str(h) for h in headers))
        for r in rows[:60]:
            out.append(" | ".join("" if c is None else str(c) for c in r))
        out.append("")
    return "\n".join(out)
