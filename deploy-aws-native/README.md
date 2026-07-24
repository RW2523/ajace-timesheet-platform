# Timesheet (Direct++) — AWS-only (no Supabase)

The timesheet app re-platformed onto **AWS-native services**. Supabase is gone
entirely. The only thing that leaves AWS is the OpenRouter call (Direct++ AI).

```
Browser ──HTTPS──► Caddy ──► Next.js app (:3009, PM2) ──► OpenRouter (Direct++)
                                 │
              ┌──────────────────┼───────────────────┐
              ▼                   ▼                    ▼
     Amazon RDS Postgres   S3 (presigned URLs)   (self-managed auth
     records + auth_users   file bytes            = bcrypt + JWT cookie)
```

### What replaced Supabase

| Supabase piece | Now |
|---|---|
| GoTrue auth | **self-managed** — `auth_users` table + bcrypt + a signed JWT session cookie (`lib/aws/auth.js`, `/api/auth/*`) |
| Postgres + PostgREST | **Amazon RDS** + one scoped endpoint `/api/data` (`lib/aws/data.js`) |
| RLS (DB-enforced) | **app-enforced** ownership in `lib/aws/data.js` (deny-by-default, forces `user_id = you`) |
| Storage | **S3** via presigned URLs (`lib/aws/storage.js`, `/api/storage/*`) |
| the `@supabase/*` client | **drop-in shims** (`lib/supabase/{client,server,middleware}.js`) so existing pages/components didn't change |

The app **builds clean** as an AWS-native app (verified: `next build` → all 23 routes, no Supabase imports).

---

## ✅ What I need from you

| # | Item | Notes |
|---|------|-------|
| 1 | **AWS account** + **EC2** `t4g.small` (2 GB) Ubuntu 24.04, ports 22/80/443 | app only — no Supabase containers, so 2 GB is plenty |
| 2 | **Amazon RDS PostgreSQL** — `db.t4g.micro`, same VPC, reachable from the EC2 box | the database |
| 3 | **S3 bucket** (`ajace-ts-files`, private) + an **IAM role on the EC2 box** for it | file storage |
| 4 | **A domain** — `timesheet.` → the EC2 IP | TLS + cookies |
| 5 | **OpenRouter API key** | Direct++ document AI |
| 6 | **`AUTH_JWT_SECRET`** you generate (`openssl rand -base64 48`) | signs the login session |

## Deploy

```bash
# on the EC2 box
git clone -b aws-native https://github.com/RW2523/ajace-timesheet-platform.git
cd ajace-timesheet-platform
bash deploy-aws-native/scripts/setup.sh                 # node + pm2 + caddy + psql

cp deploy-aws-native/env.production.example app/.env.production
#   ↳ edit: DATABASE_URL (RDS), AUTH_JWT_SECRET, STORAGE_S3_BUCKET/REGION, OPENROUTER_API_KEY, SITE_URL

cd app && npm ci && npm run build && cd ..
deploy-aws-native/scripts/migrate.sh                    # create schema in RDS
pm2 start deploy-aws-native/ecosystem.config.cjs && pm2 save && pm2 startup
sudo cp deploy-aws-native/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

## Verify end-to-end (your live test — I can't run RDS/S3 from here)

1. Open `https://timesheet.ajace.com` → **Sign up** → you land on the dashboard (session cookie set).
2. Upload a timesheet → Direct++ (OpenRouter) reads it → hours appear.
3. Confirm storage + records:
   ```bash
   aws s3 ls s3://ajace-ts-files/ts-uploads/ --recursive
   psql "$DATABASE_URL" -c "select email from auth_users; select count(*) from ts_timesheets;"
   ```
4. **Make yourself admin:** `deploy-aws-native/scripts/make-admin.sh you@ajace.com` → log in again → `/admin` shows all submissions.
5. **Privacy check (the RLS replacement):** as a second, non-admin user, confirm you see only your own timesheets.

## Cost (AWS-only)

| Line | Spec | $/mo |
|---|---|--:|
| EC2 `t4g.small` (app) | on-demand | ~$12 |
| RDS `db.t4g.micro` + 20 GB | single-AZ | ~$14 ($0 first 12 mo) |
| S3 (files) | ~10–20 GB | ~$1 |
| Data transfer / DNS | light | ~$1 |
| **AWS total** | | **~$28** (~$14 first year) |
| OpenRouter | Direct++ usage | ~$1–15 |

RDS can't cheaply "turn off" like EC2, so the always-down ~$2/mo trick doesn't apply here — this is the trade for going fully managed/AWS-native.

---

## ⚠️ Status — read before trusting in production

This is a **complete first implementation that compiles cleanly**, but I could not
run it against a live RDS/S3 from here. Test the checklist above. Known things to
watch on first run:

- **`/api/data` scoping** (`lib/aws/data.js`) is the RLS replacement — exercise every
  screen as a non-admin AND an admin and confirm no cross-user data leaks.
- **Password reset email** isn't wired to a sender yet — `/api/auth/forgot` creates the
  token and logs the link; add **Amazon SES** to email it (marked `TODO` in that route).
- **First admin** must be promoted via `make-admin.sh` (no admin exists at first signup).
