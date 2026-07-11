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
            "Ignore any selected-day 'Total' side panel -- it is not the month total."
            "\nCRITICAL: Saturday and Sunday are NOT working days. If a weekend "
            "cell is blank, empty, or 0, output 0 (or null) for it -- NEVER a "
            "guessed value like 8. Only report weekend hours when the source shows "
            "an explicit number written for that exact date."
            "\nIf the document shows an explicit month/period total (e.g. 'Total "
            "Hours: 176', 'Approved: 168'), also set \"stated_total\" to that number.")


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


# --------------------------------------------------------------------------- #
# DIRECT TRACK: the whole file is sent to a vision model with this single
# exhaustive contract. It is a SUPERSET of CANONICAL_CONTRACT (same
# employee_name / entries / weekly_totals / stated_total / confidence keys, so
# the existing _from_contract mapper works unchanged) plus every extra field the
# app needs -- period, identity, questionnaire prefills, per-field confidence,
# provenance, a document classification, and a mandatory self-check.
# --------------------------------------------------------------------------- #
DIRECT_MEGA_CONTRACT = """
Return ONLY a single JSON object with EXACTLY this shape. Fill EVERY field; use
null / [] when a datum is genuinely absent -- never omit a key, never guess.

{
  "document_type": "timesheet" | "invoice" | "approval_email" | "cover_sheet" | "other",
  "is_timesheet": boolean,
  "multiple_employees": boolean,          // true if the file holds >1 person's time

  "employee_name": string | null,         // the WORKER/contractor (never the approver,
                                           // manager, company, address, city, or project)
  "employee_id": string | null,
  "employer": string | null,              // the vendor/staffing company employing them
  "client": string | null,                // the client/company the work was for
  "project": string | null,               // project or task name/id
  "manager_name": string | null,          // approver / engagement manager, if shown
  "approval_status": string | null,       // e.g. "approved", "signed", "pending", null

  "period_start": "YYYY-MM-DD" | null,     // period start AS PRINTED
  "period_end": "YYYY-MM-DD" | null,       // period end AS PRINTED

  "entries": [                             // one object PER CALENDAR DATE that is shown
    {
      "date": "YYYY-MM-DD",
      "regular_hours": number | null,
      "overtime_hours": number | null,
      "sick_hours": number | null,
      "vacation_hours": number | null,     // PTO/vacation
      "holiday_hours": number | null,
      "total_hours": number | null,        // if a single number is given, put it here
      "time_in": string | null,            // "09:00" if shown
      "time_out": string | null,
      "project": string | null,
      "note": string | null,
      "raw": string | null,                // the literal text you read this from
      "confidence": number                 // 0..1 for THIS day
    }
  ],
  "weekly_totals": [                        // ONLY when the source is weekly, not daily
    { "week_start": "YYYY-MM-DD", "week_end": "YYYY-MM-DD",
      "regular_hours": number | null, "overtime_hours": number | null,
      "total_hours": number | null }
  ],

  "stated_regular": number | null,          // a MONTH regular total printed on the doc
  "stated_overtime": number | null,
  "stated_total": number | null,            // the MONTH total printed on the doc (a
                                            // "Total Hours" box / "Approved 168h")

  "questionnaire": {
    "worked_weekends": "yes" | "no" | null, // from the data: any Sat/Sun hours?
    "holidays_worked": [ "YYYY-MM-DD" ],    // holiday dates that show hours worked
    "pto_days": number | null,              // count of PTO/vacation days taken
    "holidays_taken": number | null
  },

  "provenance": {
    "pages_read": number,                   // how many pages you actually read
    "value_pages": { "monthly_total": number | null }  // page # a key value came from
  },
  "ambiguities": [ string ],                // e.g. "May 14 cell smudged; read as 8?"
  "handwritten_or_faint": boolean,
  "confidence": number,                     // 0..1 OVERALL

  "self_check": {                           // you MUST compute this before answering
    "sum_of_daily_totals": number,          // add up every entry.total_hours
    "matches_stated_total": boolean,        // == stated_total (within 0.5h)?
    "per_day_reg_ot_consistent": boolean,   // every day: regular+overtime == total?
    "discrepancies": [ string ]             // describe any mismatch you found
  }
}

RULES (follow ALL -- these mirror how a careful human reads a timesheet):

IDENTITY
- employee_name is the PERSON whose time this is (a human name). NEVER a company,
  client, address, city, PROJECT, or a SOFTWARE/TOOL name (e.g. "Clarity PPM",
  "Workday", "SAP"), and NEVER the APPROVER/MANAGER. Look near "Employee", "Name",
  "Consultant", "Contractor", or a signature. If truly none, null.
- "Approved by X", "Manager: X", "Timesheet Approver: X", "Hiring Manager: X",
  "Locked by X", or a lone signature is the APPROVER -- NOT the worker. Put that
  name in manager_name, never employee_name. If the worker's own name is not
  clearly labelled, prefer the name in the FILENAME over any approver name.

DO NOT WRONGLY REJECT (important)
- A faint, low-contrast, handwritten, or hard-to-read SCAN/PHOTO is STILL a
  timesheet. Read whatever hours you can see. Only set is_timesheet=false for a
  genuine INVOICE/BILL (line items, $ amounts, "Invoice #", "Bill To") or a plain
  email/cover page with NO hours grid at all. When unsure but you can see any
  dated hours or an hours grid -> is_timesheet=true and extract.
- WEEKLY-RANGE grids ARE timesheets: rows like "Week 1 | 01-May | 07-May | 40"
  (a start date, an end date, and that week's hours). Put each in weekly_totals
  (or expand to days if daily hours are shown). Do NOT reject these as "not a
  timesheet" just because there is no day-by-day grid.
- An APPROVAL email/page that states a month total (e.g. "Approved 160 hours for
  May") with no grid: set stated_total to that number and is_timesheet=true.

READING
- READ EVERY WEEK / EVERY PAGE / THE WHOLE CALENDAR. Timesheets often stack
  several weekly grids (one per page). Emit an entry for EVERY dated day with
  hours across ALL of them; do not stop after the first week.
- Use the EXACT dates printed. Include a date ONLY if it is actually shown with
  hours. Do NOT invent, extrapolate, or fill days/weeks that are not present.
- Blank/empty/illegible cell = null. 0 = 0. NEVER a guessed 8. Weekends are NOT
  assumed worked -- a blank Sat/Sun is null/0.
- PROJECT MATRICES (rows = projects, columns = weekdays): each date's total is
  the SUM across ALL project rows for that date. Go through every row.

COUNT DAYS ONCE (avoid over-reads)
- Each calendar date appears EXACTLY ONCE in entries. Never emit the same date
  twice, even if it shows on two pages. A month has ~20-23 weekdays; if your
  days_worked exceeds 23 or your total exceeds ~230h, you almost certainly
  DOUBLE-COUNTED a week/page -- re-check and de-duplicate before answering.

MONTH BOUNDARY (the figures are for the target month ONLY)
- Emit a per-day entry for EVERY dated day you see, using its REAL printed date,
  even days in the previous or next month -- the engine keeps only the days that
  fall inside the target month. Give real per-day hours; NEVER prorate or split a
  week by a fraction.
- stated_total is ONLY for a total the document prints FOR THE TARGET MONTH. If
  the only printed total is a WEEKLY or BI-WEEKLY period total that crosses the
  month boundary (e.g. a period "04/19-05/02" totalling 80h), do NOT put it in
  stated_total -- leave stated_total null and rely on the per-day entries so the
  month is computed from in-month days only.

NEVER DOUBLE-COUNT A PERIOD (portal exports: Beeline, Jira, Unanet, Time@IBM, Clarity)
- These repeat each pay period's total row TWO or THREE times per page, and also
  show per-project subtotal rows PLUS a combined day-total row. Count each pay
  period EXACTLY ONCE: take the single day-total row, never the sum of the project
  rows, and never add the same period again because it reappears on another page.
- If the file is several weekly/bi-weekly period pages, emit each period's days
  once (or one weekly_totals row per DISTINCT date range). Two different weeks that
  happen to both total 40h are BOTH real -- keep both; only collapse exact repeats
  of the SAME date range.

PRINTED TOTAL vs EXCLUDED ROWS
- If the sheet prints its own month/period total, copy it EXACTLY into stated_total.
  If your summed daily hours EXCEED that printed total, the extra is almost always
  a stray row the sheet itself excludes -- a row with hours but NO in/out times, or
  a Holiday/Leave row. Keep such rows in their proper fields, but record the gap in
  self_check.discrepancies and do not silently inflate past the sheet's own total.

HOURS TYPES
- If the sheet separates REGULAR/BILLABLE from SICK/VACATION/HOLIDAY, put each in
  its field; total_hours = the sum actually worked+paid for that day. Report the
  full monthly total; if a "billable only" number also exists, note it in
  ambiguities. Separate overtime from regular ONLY when the source explicitly
  distinguishes them; otherwise put everything in total_hours.

SELF-CHECK
- COMPUTE self_check for real: add the daily totals, compare to any printed
  total, verify each day's regular+overtime==total, and REPORT every discrepancy
  in self_check.discrepancies (do not silently "fix" the document).

RECONCILE (the number the app trusts)
- The authoritative monthly figure is the SUM of your in-month daily entries, so
  make the entries correct and COMPLETE -- read every page/week before you answer.
  stated_total is the sheet's OWN printed number, kept only for cross-check: if it
  differs from your daily sum, keep BOTH and explain the gap in
  self_check.discrepancies. NEVER pad, invent, or drop days to force a match, and
  never copy a printed total into the daily entries.
"""


# --------------------------------------------------------------------------- #
# A second, BLIND verification prompt (direct flow self-check). It re-reads the
# whole file from scratch and returns ONLY the monthly total -- it never sees the
# first read's number, so agreement is a genuine independent corroboration.
# --------------------------------------------------------------------------- #
def direct_verify_system(month: int, year: int) -> str:
    name = calendar.month_name[month]
    ndays = calendar.monthrange(year, month)[1]
    return (
        f"You are double-checking one employee's timesheet for {name} {year} "
        f"(1-{ndays} {name}). Read the ENTIRE document -- every page and every "
        f"week. Count ONLY hours dated within {name} {year}: a week that crosses "
        f"the month boundary contributes just its in-{name} days, and a period "
        f"total shown more than once (portal exports repeat the same total 2-3x "
        f"per page, and list per-project subtotals plus a day-total) is counted "
        f"ONCE. Ignore any approver/manager signature. Reply with ONLY this JSON, "
        f"nothing else:\n"
        f'{{"monthly_total": <number, hours worked in {name} {year}>, '
        f'"days_worked": <integer>, "confidence": <0..1>}}')


def direct_extract_system(month: int, year: int) -> str:
    return ("You are a meticulous timesheet data-extraction engine for a US "
            "consulting company. You are given ONE employee's timesheet document "
            "(PDF pages or an image) and must extract EVERY field a payroll app "
            "needs, reading the whole document carefully. "
            + _month_context(month, year) + DIRECT_MEGA_CONTRACT)
