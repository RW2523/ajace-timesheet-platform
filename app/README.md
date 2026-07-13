# Ajace Timesheets — product app

A Next.js + Supabase web app that wraps the Python **Timesheet Intelligence**
engine: employees sign up, upload a timesheet in any format, the AI populates a
calendar + details, they review/correct/answer a short questionnaire, validation
catches mismatches, and admins review everything.

## Architecture

```
Browser ──► Next.js (App Router, port 3009)
              │   • Supabase Auth (email/password)
              │   • /api/preview  ─┐  proxy (auth-guarded)
              │   • /api/process  ─┤
              ▼                    ▼
         Supabase            Python engine (FastAPI, port 8078)
         • Auth              • POST /api/preview-upload  (file → page images)
         • Postgres (ts_*)   • POST /api/process-upload  (file → populated calendar)
         • Storage (ts-uploads)
```

The Python engine lives in `../engine`. The web app never does
extraction itself — it calls the engine and persists the results to Supabase.

## Database (Supabase, `ts_` namespace, all RLS-protected)

| Table | Purpose |
|-------|---------|
| `ts_profiles` | Signup details (name, employer, client, manager, role). `role` is `employee`/`admin`. |
| `ts_files` | Uploaded file metadata + Storage path. |
| `ts_timesheets` | The AI-populated record (source of truth), one per user+month. |
| `ts_employee_edits` | Each employee submission (their corrected snapshot). Shown to admins. |
| `ts_admin_edits` | **Separate** table holding admin corrections — the employee's submission is never overwritten. |

RLS: employees see only their own rows; admins (via `ts_is_admin()`) can read all;
`ts_admin_edits` is admin-only. Storage uploads are scoped to `\<uid\>/…`.

## Run it

```bash
# 1) Python engine (from repo root)
cd engine
python3 -m uvicorn tsengine.api.app:app --host 127.0.0.1 --port 8078

# 2) Next.js app
cd app
npm install
npm run dev          # http://localhost:3009
```

`.env.local` holds the Supabase URL + publishable key and `ENGINE_URL` (the engine).

## Test accounts (pre-confirmed)

| Role | Email | Password |
|------|-------|----------|
| Employee | `employee@ajace.com` | `Passw0rd!` |
| Admin | `admin@ajace.com` | `Passw0rd!` |

## Auth flows

- **Signup → instant login.** Timesheet signups send an `app: 'ajace_timesheets'`
  marker in their metadata; a DB trigger (`ts_autoconfirm_timesheet`) auto-confirms
  *those* users so they can sign in immediately, and the signup form then signs them
  in automatically. This is scoped to the timesheet app — the other apps sharing this
  Supabase project keep their normal email-confirmation flow.
- **Forgot password** (`/forgot` → email link → `/auth/callback` → `/reset`):
  `resetPasswordForEmail` sends a recovery link; the callback exchanges the code for a
  session; `/reset` lets the user set a new password. Two Supabase settings are
  required for the emailed link to work:
  1. **SMTP** — the built-in mailer is rate-limited (~2–4 emails/hour) and may land in
     spam. Configure custom SMTP in Supabase → Authentication → Emails → SMTP for
     reliable delivery. (During testing you may see `email rate limit exceeded`.)
  2. **Redirect URLs** — add `http://localhost:3009/auth/callback` (and your production
     `https://…/auth/callback`) under Supabase → Authentication → URL Configuration →
     Redirect URLs, or the reset link falls back to the Site URL.

## Notes

- **Period auto-select**: on/before the 10th of a month the app defaults to the
  *previous* month (grace window); after the 10th, the current month.
- **US federal holidays** are computed in `lib/holidays.js` and surfaced in the
  calendar + questionnaire ("did you work this holiday?").

## Deployment

No hosting provider is wired into the repo — deploy the Next.js app to any host
you like (set the project **root directory to `app/`**).

Two ways to get AI processing:

### 1. Serverless Direct++ — no engine at all (`lib/directpp/`)
The whole-file Direct++ flow (blind read → code-side clip/dedupe/summing →
one-shot arithmetic repair → conditional cross-family verify → review routing)
runs **inside the Next.js API route** — no Python, no OCR, no server. Set:
- `NEXT_PUBLIC_AI_ENABLED=true`
- `OPENROUTER_API_KEY=sk-or-...`

It activates when the admin AI-flow is `direct_serverless`, when
`DIRECT_SERVERLESS=true`, or **automatically on a Vercel deployment with no
`ENGINE_URL`** (pure-Vercel mode). Input handling: PDFs/images go to the model
natively (images lightly enhanced via sharp), `.eml` sends the body text +
attachments, `.docx` falls back to its embedded images, spreadsheets arrive as
extracted CSV text (no LibreOffice render on serverless — the one trade-off).
Offline self-test: `node lib/directpp/selftest.mjs`. Note: Vercel Hobby caps
functions at 60s — a big scan with repair + verify can exceed it; Pro (300s)
removes the worry.

### 2. The full Python engine (all flows: consensus, premium+, …)
The engine shells out to `tesseract` / `LibreOffice` and uses heavy native libs,
which serverless functions don't support — host it separately (any Docker host),
then set on the app deployment:
- `NEXT_PUBLIC_AI_ENABLED=true`
- `ENGINE_URL=https://your-engine-host`

With neither configured, the app still works: auth, signup, the **manual**
timesheet flow, edit, questionnaire, US-holidays, validation, submit, and the
admin console; the "Process with AI" upload is hidden.

Supabase env: the publishable URL + anon key can be baked into
`lib/supabase/config.js` as fallbacks (safe — the anon key is public by design,
protected by RLS), or set `NEXT_PUBLIC_SUPABASE_URL` /
`NEXT_PUBLIC_SUPABASE_ANON_KEY` on the host.
