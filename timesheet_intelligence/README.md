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
