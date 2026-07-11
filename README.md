# Ajace Timesheet Platform

AI-assisted timesheet capture, extraction, and review for a US consulting company.
Upload a folder of wildly different monthly timesheets — native or scanned PDF,
Excel, CSV, DOCX, images, even forwarded `.eml` approvals — and get back
**standardized, audited, per-employee monthly records** with a calendar UI and
full source-evidence behind every number.

## Repository layout

| Path | What it is |
|------|-----------|
| [`engine/`](engine/) | **Core engine** — Python / FastAPI (`tsengine`). Multi-format extract → deterministic normalize → validate → LLM/vision (OpenRouter) only when needed. Exposes `/api/process-upload`, `/api/preview-upload`, `/api/health` on `:8078`. |
| [`app/`](app/) | **Product app** — Next.js + Supabase (`:3009`). Email/password auth, employee portal (upload → AI populate → review/edit → questionnaire → submit), and an admin console. Wraps the engine over HTTP; persists to Supabase (auth, Postgres, storage). |
| [`scripts/`](scripts/) | launchd plists + tunnel script for running the engine durably on a Mac, and `DURABLE_SETUP.md`. |
| [`render.yaml`](render.yaml) | Render Blueprint — deploys the engine as a Docker web service (auto-deploys on push to `main`). |

## Architecture

```
Browser ─► Next.js app  (app/, :3009)
             │  • Supabase Auth + Postgres + Storage
             │  • /api/process, /api/preview  (auth-guarded proxy)
             ▼
        Python engine  (engine/, FastAPI :8078)
             multi-format extract → deterministic normalize
             → validate → OpenRouter LLM/vision only when needed
```

The app never parses files itself — it proxies to the engine over HTTP
(`ENGINE_URL`), so the two deploy and scale independently.

## Processing flows

The admin picks the AI flow per deployment (one setting, no redeploy). All cloud
flows share the same setup and need only an OpenRouter key; **Budget** additionally
needs a local Ollama model.

| Flow | What it does | Cost / 68 files | Deploy |
|------|--------------|-----------------|--------|
| **🎯 Consensus** *(default)* | Two independent derivations — a hardened deterministic read + a blind model read — must agree before a number auto-accepts. Clean sheets exit at **$0**; nothing auto-accepts unless CONFIRMED. Highest accuracy, zero silent-wrongs. | ~$1.10–1.40 | key only |
| **✨ Premium+** | Parse-first + a full-image GPT vision re-read for any scan it under-reads. Best value. | ~$0.43 | key only |
| **⭐ Premium** | Parse-first — `gpt-4o-mini` + selective `gemini-2.5-pro`. | ~$0.48 | key only |
| **⚡ Direct** | Whole file to a vision LLM (`gpt-5.4-nano → mini → gpt-5`), one request per file. No parsing/OCR. | ~$1.11 | key only |
| **💰 Budget** | Free local model (`qwen2.5:7b` via Ollama) first, cloud only as fallback. Near-zero API cost, slower. | ~$0 | + Ollama |

**Models used** (all via OpenRouter, plus one local): `openai/gpt-4o-mini`,
`openai/gpt-5.4-nano`, `openai/gpt-5.4-mini`, `openai/gpt-5`,
`google/gemini-2.5-pro`, and local `qwen2.5:7b-instruct` (Budget only).
The engine **never calls an Anthropic/Claude model** — excluded on cost grounds.

## Quick start (local)

```bash
# 1) Engine
cd engine
cp .env.example .env                 # add TSE_OPENROUTER_API_KEY
pip install -r requirements.txt
python3 -m uvicorn tsengine.api.app:app --port 8078

# 2) App (new terminal)
cd app
cp .env.example .env.local           # add Supabase URL + anon key + ENGINE_URL
npm install
npm run dev                          # http://localhost:3009
```

Run the engine test suite: `cd engine && python -m pytest -q`.

## Deployment

| Piece | Where | Notes |
|-------|-------|-------|
| Engine | **Render** (Docker, `render.yaml`) or a Mac via `scripts/` (launchd + Cloudflare tunnel) | set `TSE_OPENROUTER_API_KEY`; auto-deploys on push to `main` |
| App | **Vercel** (Root Directory = `app`) | set `ENGINE_URL` + Supabase env vars |
| Data / Auth | **Supabase** | `ts_`-prefixed tables; email/password auth |

> After renaming folders, update **Vercel → Root Directory = `app`** and confirm
> **Render → rootDir = `engine`** (already set in `render.yaml`).

See [`engine/README.md`](engine/README.md) and [`app/README.md`](app/README.md)
for the deep dives.

## What's in the repo

Secrets (`.env*`), build artifacts (`node_modules/`, `.next/`, `__pycache__/`),
and generated evaluation reports are git-ignored. The sample timesheet data under
`Timesheet/` and `TimeSheet-May/` is intentionally committed by the owner (it
contains employee PII + invoices) — keep the repository access controlled
accordingly.
