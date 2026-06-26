# Ajace Timesheet Platform

AI-assisted timesheet capture, extraction, and review for a consulting company.
Two projects in one monorepo:

| Folder | What it is |
|--------|-----------|
| [`timesheet_intelligence/`](timesheet_intelligence/) | **Core engine** (Python/FastAPI). Turns a folder of heterogeneous monthly timesheets — native/scanned PDF, Excel, CSV, DOCX, images, `.eml` — into standardized, audited per-employee monthly records. Deterministic-first, escalating to OpenRouter LLM/vision only when needed. Ships a calendar UI with source-evidence drill-down. |
| [`timesheet_app/`](timesheet_app/) | **Product app** (Next.js + Supabase). Email/password auth, employee portal (upload → AI populate → review/edit → questionnaire → submit), and an admin console. Wraps the engine over HTTP and persists to Supabase (auth, Postgres, storage). |

## Architecture

```
Browser ─► Next.js app (timesheet_app, :3009)
             │  • Supabase Auth + Postgres + Storage
             │  • /api/process, /api/preview  (auth-guarded proxy)
             ▼
        Python engine (timesheet_intelligence, FastAPI :8078)
             • multi-format extract → deterministic normalize → LLM/vision (OpenRouter)
```

## Quick start

```bash
# 1) Engine
cd timesheet_intelligence
cp .env.example .env          # add your OpenRouter key
pip install -r requirements.txt
python3 -m uvicorn tsengine.api.app:app --port 8078

# 2) App
cd ../timesheet_app
cp .env.example .env.local    # add your Supabase URL + anon key
npm install
npm run dev                   # http://localhost:3009
```

See each folder's own README for details:
[engine README](timesheet_intelligence/README.md) · [app README](timesheet_app/README.md).

## Not in this repo (by design)

Secrets (`.env`, `.env.local`), real employee timesheet data, processing outputs,
and build artifacts are git-ignored — this is a public repository.
