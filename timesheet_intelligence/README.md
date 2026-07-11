# Timesheet Intelligence Core Engine

A generic, model-agnostic engine that turns a folder of **heterogeneous monthly
timesheets** — PDF, scanned/image PDF, Excel, CSV, DOCX, PNG/JPG — into
**standardized, audited per-employee monthly records** and a calendar UI, with
**no template-specific code**.

Point it at a folder + a month/year. It inspects each file, routes it to the
right extractor, normalizes wildly different layouts into one schema, validates
totals/duplicates/conflicts, and renders a calendar with full source evidence
behind every number.

> Phase 1 scope: **extraction → normalization → validation → calendar display**.
> The architecture is deliberately layered so Phase 2 (approvals, payroll,
> overtime/holiday policy, exports, RBAC, integrations) bolts on without
> touching the core.

---

## Why it's hard (and what this handles)

Real consulting timesheets are chaos. From the included sample month alone:

| Challenge | Example in the data | How the engine copes |
|---|---|---|
| 6+ unrelated layouts | AJACE monthly grid, NPO weekly grid, HCPSS biweekly, Hexaware date/code, Innosoft project matrix, Deloitte timecard | Layout-agnostic strategies + LLM fallback |
| Date ambiguity | `01/04/2026` = **Apr 1** (Hexaware, DMY) vs `4/12/2026` (Brillio, MDY) | Per-file order inference (a component >12 is decisive; else fit to target month) |
| Hours encodings | `7.97`, `8 00` (=8:00), `9:00–5:00` (=8h), `7:30 AM–4:30 PM` | Dedicated hours parser + in/out computation w/ PM crossover |
| Cross-month periods | week `4/26–5/2` | Prorated by in-month fraction |
| Weekly-only data | Saravanan xlsx, Brillio CSV | Kept as weekly totals, prorated, flagged `WEEK_ONLY` |
| Duplicates / conflicts | Brillio rows `39` and `0` for the same week | Dedup by trust, flagged `CONFLICT`/`DUPLICATE` |
| Multiple files / person | 5 weekly HCPSS files for one employee | Merged into one month by identity |
| Image-container DOCX | Word file that is just a pasted screenshot | Embedded media extracted → OCR/vision |
| Scanned & handwritten | ~16 scanned PDFs + 5 images | Local OCR + OpenRouter vision |

On the bundled sample folder (April 2026, 59 files) the **deterministic + local-OCR**
path alone (no API key) cleanly resolves every structured format. Independently
audited against the raw files:

| Employee | Engine | Notes |
|---|---|---|
| Harsha / Siva / Sathis… | 176.00h | full-time monthly grids (22 × 8h) |
| Richard | 80.00h | 4h/day |
| Adam (NPO) | 182.50h | 156 reg **+ 26.5 OT**; an Excel-1900-corrupted week (Apr 20-24) auto-recovered |
| Sean (NPO) | 173.42h | 172 reg + 1.42 OT |
| Elangovan (Deloitte) | 136.00h | labeled-hours timecard, de-duplicated |
| Saravanan | 176.00h | flags a real source inconsistency (declared 168 vs daily 176) |
| Yazheni (Brillio CSV) | 93.85h | duplicate-week rows deduped, cross-month week prorated |

Scanned/handwritten/odd-layout files are routed to the OpenRouter vision/LLM
path (activated by a key) — e.g. project matrices, photographed timesheets,
and image-only DOCX files.

> The engine shipped after an adversarial multi-agent review (23 confirmed
> findings fixed, incl. a decimal-hours parsing bug, an overtime-column
> collision, evidence-endpoint path-traversal hardening, and LLM date-order
> handling) plus a ground-truth numeric audit against the source files.

---

## Architecture

```
folder + month/year
        │
        ▼
┌──────────────┐   detect format + quality (native-text vs scanned, etc.)
│ Orchestrator │── routes each file to a specialized extraction "subagent":
└──────────────┘     excel · csv · pdf_native · pdf_scanned · docx · image
        │  RawExtraction (text + tables + images + source refs)
        ▼
┌──────────────┐   deterministic strategies: daily_grid · weekly_totals ·
│  Normalizer  │   weekday_matrix · labeled-hours timecard
└──────────────┘   ── escalates to ──▶ LLM normalizer (OpenRouter); per file it
        │                              picks the flow:
        │   • native digital text  → text LLM (per page, aggregated)
        │   • scanned/photo grid   → VISION grounded in layout-OCR  ◀ see below
        │   • poor/handwritten     → VISION on the image alone
        │  NormResult(s) per file
        ▼
┌──────────────┐   merge files per employee · resolve duplicates/conflicts by
│   Registry   │   trust · prorate weekly totals · lay out the month calendar
└──────────────┘
        │
        ▼
┌──────────────┐   field consistency · totals cross-check · optional LLM
│  Validator   │   conflict reconciliation
└──────────────┘
        │  ProcessingReport (Pydantic) ──▶ JSON + Calendar UI (FastAPI)
        ▼
```

**All AI calls go through OpenRouter.** A model-routing table
(`config/models.yaml`) maps each *task* — `classify`, `vision`, `table`,
`normalize`, `validate` — to an ordered list of OpenRouter model candidates
with automatic fallback. Code asks for a *task*, never a hardcoded model; swap
models via yaml or `TSE_MODEL_*` env vars. The shipped default routes every task
to `google/gemini-2.5-pro` (max accuracy) with `openai/gpt-4o-mini` as a fast
fallback; flip to all-`gpt-4o-mini` for ~40× lower cost at slightly lower
accuracy on the hardest scans.

Each calendar day is counted **exactly once**: daily data is authoritative for
its dates, and a weekly total contributes only for in-month days not already
covered (by daily data or an earlier weekly total) — so the same period reported
by two sources (e.g. a CSV plus a photo) never double-counts.

**Per-page processing — parallel, with retry.** Multi-page documents (a weekly
grid per page) are read one page at a time and aggregated. Pages run
**concurrently** (the runtime bottleneck), and a page that returns no data is
**retried once** — so model variance on a single hard page can no longer
silently drop that page's whole week (this fixed a 6-page scan that flipped
between 72h and the correct 176h).

**Layout-grounded vision.** For scanned grids, a vision model alone tends to
*hallucinate* values into blank cells (e.g. reading 8h on weekends that are
actually 0). The engine runs **layout-aware OCR** first (tesseract word boxes →
reconstruct the real grid rows), then feeds that text to the vision model *with*
the page image as grounding ("use these exact cell values; don't invent hours
for blank/0 cells"). When OCR confidence is too low to trust (handwriting), the
grounding is dropped and the model reads the image freely. This fixed the
worst residual errors (e.g. a weekend over-read of 240h → the correct 176h).

### Module map (`tsengine/`)
| Path | Responsibility |
|---|---|
| `schema.py` | Canonical models (`EmployeeMonth`, `DayRecord`, `Issue`, `SourceRef`, `RawExtraction`) |
| `settings.py` | Env/`.env`-driven config + model routing |
| `orchestrator.py` | Format detection → extractor dispatch |
| `ingest/` | `detect`, `excel`, `csv_ingest`, `pdf_native`, `pdf_scanned`, `docx_ingest`, `image`, `ocr` |
| `normalize/` | `dates`, `hours`, `normalizer` (deterministic), `llm_normalizer` (OpenRouter) |
| `aggregate/` | `calendar` (weekends/holidays), `registry` (merge/dedupe/rollup) |
| `validate/` | `validator` (consistency + LLM reconciliation) |
| `llm/` | `client` (OpenRouter), `router` (task→model), `prompts` |
| `pipeline.py` | `process_folder(folder, month, year) → ProcessingReport` |
| `api/` + `ui/` | FastAPI endpoints + self-contained calendar UI |

---

## Install

```bash
cd timesheet_intelligence
python -m pip install -r requirements.txt
# Local OCR fallback (optional but recommended): tesseract
#   macOS:  brew install tesseract     Ubuntu: apt-get install tesseract-ocr
```

Python 3.10+ recommended.

## Configure (optional — runs without any key)

```bash
cp .env.example .env
# set TSE_OPENROUTER_API_KEY=...  to unlock vision/LLM for scanned & odd layouts
```

Key settings (all `TSE_`-prefixed; see `.env.example`):
`OPENROUTER_API_KEY`, `LLM_POLICY` (`never|on_low_confidence|always`),
`LLM_CONFIDENCE_THRESHOLD`, `USE_LOCAL_OCR`, `OCR_DPI`, `HOLIDAY_REGION`,
`WEEKEND_DAYS`, `MAX_HOURS_PER_DAY`. Per-task model overrides:
`TSE_MODEL_VISION`, `TSE_MODEL_NORMALIZE`, etc.

## Run

**CLI** — process a folder and print a summary + write JSON:
```bash
python -m tsengine.cli --folder "/path/to/Timesheet" --month 4 --year 2026
python -m tsengine.cli --folder "/path/to/Timesheet" --month 4 --year 2026 --serve
```
Output: `output/report_YYYY_MM.json` (+ `output/latest_report.json`). The CLI
summary also prints **OpenRouter usage and actual cost** (tokens + USD, broken
down per model — captured live from OpenRouter's `usage.cost`), and the same
fields are persisted on the report (`llm_calls`, `llm_tokens`, `llm_cost_usd`,
`llm_usage_by_model`).

**API + Calendar UI**:
```bash
python -m uvicorn tsengine.api.app:app --port 8000
# open http://127.0.0.1:8000/
```
- `POST /api/process` `{folder, month, year}` → runs the pipeline, returns the report
- `GET  /api/report` → latest report
- `GET  /api/evidence?file=&page=` → source evidence (rendered page image or original file)
- `GET  /api/health` → config + active model per task

The UI shows, per employee: the full month calendar (regular/OT/total per day),
weekends/holidays/missing/overtime/flagged distinctions, monthly summary,
client/project breakdown, a data-quality issue list, and **click-through to the
source evidence** behind any day.

## Test
```bash
python -m pytest -q
```
Unit tests cover date inference, hours parsing, every normalization strategy,
aggregation (conflict/duplicate/weekly proration/merge), the LLM contract (with
a fake router, so no key needed), plus an end-to-end run over the sample folder.

---

## The canonical record

Every employee-month is one `EmployeeMonth`: identity (name/id, clients,
projects), 30/31 `DayRecord`s (date, weekday, weekend/holiday flags,
regular/overtime/total hours, project, **source refs**, per-day issues), weekly
totals, monthly rollups, client/project breakdown, an issue list, extraction
methods used, and a confidence score. Issue codes: `MISSING`, `INVALID`,
`UNCLEAR`, `DUPLICATE`, `CONFLICT`, `OUT_OF_RANGE`, `CROSS_MONTH`, `WEEK_ONLY`,
`TOTAL_MISMATCH`, `NEEDS_LLM`, `UNATTRIBUTED`, `OCR_LOW_QUALITY`, `PARSE_ERROR`.

Every extracted number carries `SourceRef`s (file / sheet / page / row / cell /
image region / extractor) so the whole report is auditable.

## Evaluation (20-file blind sample, all formats)

A stratified-random 20-file sample (5 Excel, 1 CSV, 6 native PDF, 4 scanned PDF,
3 image, 1 DOCX) was run end-to-end with `gpt-4o-mini` and graded against
ground truth established by independently re-reading every source file
(openpyxl / pdfplumber / PyMuPDF / tesseract OCR), with adversarial verification
of each flagged error.

| Stage | Accuracy (±10% of true April hours) |
|---|---|
| Initial (all `gpt-4o-mini`) | 50% (9/18) |
| After extraction fixes (all `gpt-4o-mini`) | 78% (14/18) |
| **+ `vision` → `gemini-2.5-pro` (hybrid)** | **100% (18/18)** |

What the eval drove (all fixed):
- **Multi-page aggregation** — stacked weekly grids (5 weeks across pages) were
  read only for week 1. Native timecards (`date <hrs> PayCode`) now parse
  deterministically; scanned/image docs use **per-page vision aggregation**.
- **Image-only fallbacks** — scanned PDFs with no text layer / unreadable
  Type3-font text now route to OCR+vision (e.g. Emmanuel 0h → 176h).
- **Name extraction** — no longer grabs a company address or the *approver*;
  picks the worker/contractor (e.g. an approver name → the real contractor).
- **Wrong-month rejection** — a May timesheet is no longer counted in April.

Structured (Excel/CSV) and clean native-PDF files score ~100% on `gpt-4o-mini`
alone. The hard multi-week scanned/handwritten/image documents are where model
strength matters: with `gpt-4o-mini` they under-read by a week or a few cells
(78%); routing just the `vision` task to `gemini-2.5-pro` reads every week and
takes the sample to **100%** (shipped default in `config/models.yaml`). Every
remaining low-confidence record is flagged for human review either way.

### Full-folder re-evaluation (all 59 files)

A second, exhaustive ground-truth pass over **every** file (59 files / 49
employees) drove a further round of fixes — multi-page native aggregation
(per-page text), native PDFs whose grid is an embedded image, merge-collision
splitting (one company template shared by several people), and a hard per-call
timeout. File-level accuracy went **68% → 86%** (≈90% adjusting for two
biweekly forms mis-labelled in ground truth). The residual misses are project
matrices needing cross-project summing, occasional vision weekend over-reads,
and genuinely ambiguous double-submissions — all flagged with low confidence.

**Cost** (full 59-file run, hybrid models, captured from OpenRouter `usage.cost`):
**$1.84** total — `gemini-2.5-pro` $1.82 (49 vision calls) + `gpt-4o-mini`
$0.02 (28 calls), 465K tokens. ~96% of cost is vision; an all-`gpt-4o-mini` run
costs ~$0.05 but at lower accuracy on hard scans. Saved to
`results/openrouter_cost.json`.

## Design notes & honest limits

- **Deterministic-first, never guesses.** When a value is ambiguous it is left
  `null` and flagged rather than fabricated — wrong data is worse than a flag.
- **No key?** Scanned/handwritten/unusual-layout files are processed as far as
  local OCR allows and flagged `NEEDS_LLM`; add an OpenRouter key to complete them.
- **Holiday/overtime *policy*** is intentionally minimal here (display only). A
  configurable `HolidayProvider` and policy hooks are the Phase-2 seam.
- Confidence is heuristic; treat low-confidence employees as review candidates.

## Extending (Phase 2 seams)
Approvals/payroll/exports consume `ProcessingReport` read-only. Overtime &
holiday rules slot into `aggregate/calendar.py` + a future `policy/` module.
New formats = one `ingest/*` module returning a `RawExtraction`. New models =
edit `config/models.yaml`. Nothing in the core is provider- or template-specific.

## Resilience & accuracy notes (May-2026 hardening)

A full run over a heterogeneous May folder (70 files, 6 clients) drove these:

- **Graceful low-credit degradation.** When OpenRouter returns `402` ("requested
  N tokens, can only afford M"), the client now **clamps `max_tokens` to M and
  retries** instead of dropping the document. Salvages a run as credit runs low;
  only a hard `403` monthly-limit is terminal. (`llm/client.py`)
- **Invoice rejection.** Billing/invoice PDFs that list employee hours are
  classified and skipped (`unprocessed`, reason "invoice") rather than fabricated
  into records — verified on 5 May invoices.
- **Name guard.** Filenames like `May_timesheet_2026.xlsx` (no name inside) no
  longer yield the employee **"May"**: month names and generic timesheet words can
  never stand in as a person; falls back to the unique filename. (`ingest/excel.py`)
- **Docling (IBM TableFormer) — opt-in.** `ingest/docling_ingest.py` adds
  TableFormer table-structure recognition as a **fallback** PDF table extractor
  (used only when pdfplumber finds none). Default **off** (`TSE_USE_DOCLING=1` to
  enable): A/B testing showed it extracts materially better grids, but enabling it
  by default can short-circuit to a wrong partial summary and suppress the LLM that
  would resolve a file correctly. Flip on once `normalize/` table-selection +
  confidence-gating is tuned and validated against live LLM. Install: `pip install docling`.

### Month-boundary clipping & period resolution (all flows, all months)

Two generic, deterministic (`$0`, no model) fixes for the biggest cross-month
error class — weeks that straddle a month boundary and files filed under the
wrong month. Both are month-agnostic: they take the target `(month, year)` and
work for any period.

- **Workday-weighted weekly clip.** A lump weekly total that straddles the
  boundary is now attributed by **workday**, not calendar day. A 40h week
  spanning `Apr 27–May 3` contributes **8h** to May (only May 1, a weekday), not
  the old `40 × 3/7 = 17.1h`. If the hours can't fit in the week's workdays
  (`>max_hours_per_day` each → weekend work), it falls back to a 7-day spread so a
  genuine 6/7-day week is never under-counted. Honors the configurable
  `weekend_days`. (`normalize/dates.py:clip_weekly_to_month`, applied in
  `aggregate/registry.py:_rollup`.)
- **2-of-3 period resolver.** Three signals vote on the period a file really
  covers — the requested month, the **filename** month (`TS May-2026`,
  `4.30.26`, `…20260529`), and the **dominant in-document date**. Majority wins;
  a lone dissenter is noted; a genuine disagreement raises a `PERIOD_MISMATCH`
  warning → **needs_review** (never a silent wrong-month total). When filename
  **and** content agree on a different month than requested (e.g. an April sheet
  dropped into the May run), it's surfaced as a remap candidate.
  (`normalize/dates.py:resolve_target_period`, wired in `pipeline.py`.)

### Portal-export period parsing & dedupe (scanned biweekly exports)

Biweekly/weekly portal exports (Beeline, Jira/Timesheets-for-Jira, Unanet,
Time@IBM, Clarity) print **one pay period per page** as a date range header —
`04/19/2026 to 05/02/2026`, `Apr 26, 2026 - May 2, 2026`, `Apr 25 - May 01,
2026` — but the OCR reads the total row two or three times per page, and pages
also carry per-project subtotal rows. Summing every verified row double- or
triple-counted the month to **280–448h**. The new `portal_periods` strategy
(`normalize/normalizer.py`) anchors each verified total to the **period** it
sits under, takes the **max per period** (collapsing OCR repeats *and* project
subtotals to the one real weekly total), **dedupes by date range** — never by
hours, so five genuinely-identical 40h weeks all survive — and emits one weekly
total per period that the month-clip above then trims to the target month. It
steps over noise ranges (a >1yr assignment "Date Range" line), and returns
`None` on a plain `WK1..WK5 Total` scan so that falls through to the existing
sum-verified OCR path. Verified on the real scans: Saurabh 448→**152**, Jude
344→**160** (both exact truth), Arunkumar 384→**128** (plausible; the remainder
is OCR dropping a page).

### Vote-validity: a lone read may not silently decide a month

The "single-cell collapse" class shipped a tiny, confident number for a full
month — a legacy `.xls` whose only parsed value is one summary cell (Justin
**8h** vs a 170h month), a `.docx` where a single `8 Hours` label was read
(Hemachandra **8h** vs 168h). A read may now only *decide* the month if it
carries **month-scale evidence**: ≥6 worked day rows, weekly ranges covering ≥10
in-month weekdays, or a self-verifying method (sum-verified OCR / deduped portal
period). A read below **60h with ≤2 worked days and no full grid** is demoted —
`needs_llm` + confidence capped at 0.25 — so the pipeline escalates (premium
re-reads and recovers the real number) or, with no LLM, it routes to review
instead of silently shipping. A genuine part-time month backed by a real grid is
evidence-valid and untouched. (`normalize/normalizer.py:_apply_vote_validity`,
applied at the single `_attach_identity` chokepoint on every return path.)

## Two processing flows (v1.1)

Set `TSE_FLOW=budget|premium` (default `premium`). Both share the same
deterministic-first parser stack — the flow only changes what happens when a
file needs an LLM:

| Stage | budget (near-zero cost) | premium (max accuracy) |
|---|---|---|
| Deterministic parsers | identical (benchmark-validated layering) | identical |
| Text normalization | **FREE local LLM** (Ollama `qwen2.5:7b-instruct`), plausibility-gated, gpt-4o-mini fallback | gpt-4o-mini |
| Vision (scans/photos) | gpt-4o-mini (local models have no vision) | gpt-4o-mini |
| Hard-file second opinion | off | **gemini-2.5-pro** (selective) |
| PaddleOCR faint-scan escalation | on | on |

Budget-flow local LLM needs [Ollama](https://ollama.com) + `ollama pull
qwen2.5:7b-instruct` (~4.7 GB); without it the budget flow transparently uses
gpt-4o-mini. Local calls run ~40–165 s/file (free but slow) and are
plausibility-gated: an implausible local read (fabricated days, >300h) is
rejected and retried on the cloud model automatically.

### Parser choices are benchmark-driven (real-dataset A/B, June 2026)

Measured on the actual April+May folders, deterministic-resolution rate:
pdfplumber beat Docling and pymupdf4llm on native PDFs (7/16 vs 4 and 3, and the
alternatives corrupted files the baseline got right); the current openpyxl
ingest beat MarkItDown and Docling on office files (9/13); PaddleOCR beat
tesseract on all 6 hard scans (conf 96–100 vs 40–88) → it now takes over
whenever tesseract confidence < 80. Legacy `.xls` converts via a pandas/xlrd
fast-path (0.02 s) before falling back to LibreOffice.

### Internal sub-agents (per-file audit trace)

Every processed file gets a `FileTrace` in `report.agent_traces`: which agent
(Classifier → Parser → OCR → Normalizer / Normalizer:Local / Normalizer:Cloud /
VisionReader → Reconciler) acted, what it decided, and which model it used.
`GET /api/health` reports the active flow + local-LLM availability.

## Third flow: "direct" (v1.2) — whole-file to a vision model

Set `TSE_FLOW=direct`. Instead of parsing, the WHOLE file is sent to a vision LLM
with one exhaustive prompt (`prompts.DIRECT_MEGA_CONTRACT`) that asks for every
field the app needs — identity, period, per-day hours (regular/OT/sick/vacation/
holiday), weekly rows, stated totals, questionnaire prefills, per-field
confidence, provenance, and a mandatory arithmetic self-check. One request per
file; office files are converted to PDF first (the same `to_pdf` used by preview).

Model ladder (config, not hardcode — `TSE_DIRECT_PRIMARY/_FALLBACK1/_FALLBACK2`):

```
openai/gpt-5.4-nano   (every file, cheap, 400k context)
  ↓ escalate on: no data | confidence < 0.75 | implausible read (>23 days, >300h)
openai/gpt-5.4-mini
  ↓ escalate if still failing
openai/gpt-5          (final read; best-so-far kept if it fails)
```

A self-check mismatch does NOT escalate (a genuine stated-vs-sum document
discrepancy won't be fixed by a bigger model) — it routes the record to human
review instead. When ≥2 models read a file and disagree on the month total by
>2h, the record is auto-flagged CONFLICT.

### Human-review routing (all flows)

Every `EmployeeMonth` now carries `review_status`, computed by the validator:

| status | when | admin action |
|---|---|---|
| `auto_accepted` | 0 errors, 0 conflicts, confidence ≥ 0.85 | none — shown as clean |
| `needs_review` | any warning / confidence 0.6–0.85 | review queue |
| `blocked` | errors, cross-model conflict, or confidence < 0.6 | must resolve |

The admin console has a triage filter (All / Blocked / Needs review / Clean).

### Extra "after extraction" checks (all flows)

Beyond the existing per-day/-month checks, the validator now also flags: month
total > 230h (over-read), `days_worked` > weekdays-in-month (impossible → error),
and any overtime present (confirm approval).

### Accuracy harness

`scripts/eval_flows.py` scores any flow(s) against `tests/golden_set.json`
(invoice-confirmed + audited April/May totals): exact-total rate, within-2h rate,
days accuracy, cost, and time. The direct track ships as default only if it beats
premium on exact-total rate at acceptable cost:

```
TSE_OPENROUTER_API_KEY=... python scripts/eval_flows.py direct premium budget
```
