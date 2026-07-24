# Deploy AJACE Timesheet (Direct++) to AWS — one box

Runs the **Timesheet app only** with **Direct++** AI extraction (OpenRouter), a
**self-hosted Supabase** (auth + Postgres + storage) on a single EC2 box, and
**S3** for uploaded files. No Python engine, no HR, no procurement.

Everything runs **on AWS**; the *only* thing that leaves AWS is the OpenRouter call.

```
Browser ──HTTPS──► Caddy ──► Timesheet (Next.js :3009) ──► OpenRouter (Direct++)
                     │
                     └──► Kong :8000 ──► GoTrue · PostgREST · Storage · Postgres
                                                              │            │
                                                        records=EBS   files=S3
```

---

## ✅ What I need from you (the whole list)

Nothing here is done for you automatically — these are yours to create/provide:

| # | Item | Why | Where |
|---|------|-----|-------|
| 1 | **AWS account** + ability to launch EC2 | the box | console |
| 2 | **EC2 instance**: `t4g.medium` (4 GB, ARM) Ubuntu 24.04, 20–30 GB gp3, ports 22/80/443 open | runs the stack | §2 |
| 3 | **IAM role on the instance** with access to your S3 bucket | files + backups, no static keys | §2 |
| 4 | **S3 bucket** (e.g. `ajace-ts-files`, private) | file storage | §2 |
| 5 | **A domain / subdomains** you control — `timesheet.` and `api.` → the box IP | TLS + auth cookies | §3 |
| 6 | **OpenRouter API key** (`sk-or-…`) | Direct++ document AI | §4 |
| 7 | **Four secrets** you generate: `POSTGRES_PASSWORD`, `JWT_SECRET`, `ANON_KEY`, `SERVICE_ROLE_KEY` | Supabase auth/DB | §4 |
| 8 | *(optional)* your current Supabase connection string | migrate old data | §7 |

I built everything else: the schema, the container wiring, the S3 switch, the
scripts, and this guide.

---

## 1. Get the code onto the box

```bash
git clone https://github.com/RW2523/ajace-timesheet-platform.git
cd ajace-timesheet-platform/deploy-aws
```
(Use the `deploy/aws-timesheet` branch until it's merged: `git checkout deploy/aws-timesheet`.)

## 2. AWS prerequisites (once)

- **Launch** `t4g.medium`, Ubuntu 24.04, 20–30 GB gp3. Security group: allow 22 (your IP), 80, 443.
- **S3 bucket**: create `ajace-ts-files` (Block Public Access ON — files are served through signed URLs).
- **IAM role** → attach to the instance → policy allowing `s3:GetObject/PutObject/DeleteObject/ListBucket` on `arn:aws:s3:::ajace-ts-files` and `/*`. Using the role means **no AWS keys in any file**.

## 3. DNS (Cloudflare or Route 53)

Point two records at the instance's public IP:
```
A   timesheet.ajace.com   ->  <EC2_PUBLIC_IP>
A   api.ajace.com         ->  <EC2_PUBLIC_IP>
```
On Cloudflare, set them **DNS-only (grey cloud)** for the first certificate issue; you can switch to proxied later. Put your real hostnames into `caddy/Caddyfile` (replace `timesheet.ajace.com` / `api.ajace.com` and the ACME `email`).

## 4. Bootstrap + configure

```bash
bash scripts/setup.sh          # installs Docker+swap, clones Supabase, creates .env
```
Then **edit `.env`** (README values):

- `POSTGRES_PASSWORD` → `openssl rand -base64 24`
- `JWT_SECRET` → `openssl rand -base64 40` (must be 40+ chars)
- `ANON_KEY` and `SERVICE_ROLE_KEY` → two JWTs signed with that `JWT_SECRET`.
  Generate at **supabase.com/docs/guides/self-hosting#api-keys** (or any JWT tool):
  payloads `{"role":"anon"}` and `{"role":"service_role"}`.
  *Quick demo shortcut:* keep the example's default `JWT_SECRET`/`ANON_KEY`/`SERVICE_ROLE_KEY`
  (they're publicly-known demo values — fine for a throwaway demo, **not** for real data).
- In the appended app block: set `NEXT_PUBLIC_SUPABASE_ANON_KEY` = your `ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY` = your `SERVICE_ROLE_KEY`, the `api.`/`timesheet.` URLs,
  `STORAGE_S3_BUCKET`, `STORAGE_S3_REGION`, and `OPENROUTER_API_KEY`.

## 5. Start it

```bash
scripts/up.sh          # build + start the whole stack (first run pulls images + builds the app)
scripts/migrate.sh     # create the ts_ schema, RLS, and the ts-uploads bucket
scripts/smoke.sh       # health check
```

## 6. Verify end-to-end (the real test)

1. Open `https://timesheet.ajace.com` → **Sign up** (auto-confirmed, no email needed).
2. Upload a timesheet (PDF/image) → Direct++ reads it via OpenRouter → hours appear.
3. Confirm the record and the file landed:
   ```bash
   aws s3 ls s3://ajace-ts-files/ts-uploads/ --recursive     # {userId}/{YYYY-MM}/…
   ```
4. **Make yourself admin** (first user), via the helper:
   ```bash
   scripts/make-admin.sh you@ajace.com
   ```
   (Role changes are blocked for normal users by the `ts_guard_role_change` trigger — this runs as `postgres`, which is exempt.)

## 7. Bring over existing data (optional)

Skip for a fresh pilot. To migrate: see `scripts/import-from-supabase.sh` (copies auth
users + `ts_*` rows, and shows the `aws s3 sync` for the files). Review it first.

## 8. Sizing & cost

- **Default = full Supabase stack** → **`t4g.medium` (4 GB)**. Most reliable; no fragile trims.
- **Budget = `LEAN=1 scripts/up.sh`** → minimal service set, fits **`t4g.small` (2 GB)** + swap.
  If lean mode complains that a service is unhealthy waiting on `analytics`, either run full,
  or in `supabase-docker/docker-compose.yml` remove the `analytics`/`vector` services and any
  `depends_on: analytics` lines, then retry. Verify with `scripts/smoke.sh`.

| Mode | Box | AWS $/mo (24/7) | + OpenRouter |
|---|---|--:|--:|
| Reliable | `t4g.medium` | ~$28 | usage (~$1–15) |
| Budget | `t4g.small` | ~$16 | usage |
| **Turned down** (stop instance) | — | **~$2** (S3 + EBS) | — |

## 9. Day-2

- **Nightly DB backup → S3** (files are already in S3): `crontab -e`
  ```
  15 3 * * *  cd /home/ubuntu/ajace-timesheet-platform/deploy-aws && scripts/backup.sh >> /tmp/ts-backup.log 2>&1
  ```
- **Turn it down** between pilots: `scripts/down.sh` (takes a backup, stops containers),
  then **Stop** the instance in the console → ~$2/mo. Bring back: Start instance, `scripts/up.sh`.
- **Update the app** after code changes: `git pull && scripts/up.sh` (rebuilds the app image).

## 10. Troubleshooting

- `smoke.sh` shows `rest+db: FAIL` → run `scripts/migrate.sh`.
- Certs not issued → DNS not pointing at the box yet, or Cloudflare proxied during first issue (set grey-cloud).
- Uploads fail / not in S3 → check the instance IAM role + `STORAGE_S3_BUCKET`/`STORAGE_S3_REGION`;
  the storage env keys are in `app.compose.yml` (the one place to match your storage-api version).
- App shows old Supabase → `NEXT_PUBLIC_*` are baked at build; fix `.env` then `scripts/up.sh` to rebuild.

---

### What's verified vs. what you run
The database schema is reproduced **1:1 from your live project** (tables, columns,
keys, RLS, functions, triggers, the `ts-uploads` bucket) plus one added security
guard. The container wiring uses the **official Supabase self-host compose** (not
hand-rolled). I can't launch AWS or run Docker from here, so **§5–6 is where you
prove it** — `smoke.sh` plus the browser upload test confirm it end-to-end.
