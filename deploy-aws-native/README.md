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

## Deploy — production, in 3 phases

### Phase 1 — provision all AWS infra (one command, your creds)
`infra/cloudformation.yaml` creates the S3 bucket, RDS Postgres, EC2 app host,
the IAM role (S3 + SES), and security groups. Run it with the AWS CLI:

```bash
aws cloudformation deploy \
  --template-file deploy-aws-native/infra/cloudformation.yaml \
  --stack-name ajace-timesheet \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      VpcId=vpc-xxxx AppSubnetId=subnet-public \
      DbSubnetIds=subnet-aaa,subnet-bbb \
      KeyName=your-keypair SSHLocation=YOUR_IP/32 \
      DBPassword='a-strong-password' BucketName=ajace-ts-files
aws cloudformation describe-stacks --stack-name ajace-timesheet \
  --query 'Stacks[0].Outputs' --output table    # note AppPublicIP + DBEndpoint
```
> The app subnet must be **public** (auto-assign public IPv4 on). `DeletionPolicy`
> keeps the S3 bucket and snapshots RDS if you ever delete the stack.

### Phase 2 — bring the app to production (on the EC2 host)
```bash
ssh -i your-key.pem ubuntu@<AppPublicIP>
git clone -b aws-native https://github.com/RW2523/ajace-timesheet-platform.git
cd ajace-timesheet-platform
cp deploy-aws-native/env.production.example app/.env.production
#   ↳ edit: DATABASE_URL (RDS endpoint + DBPassword), AUTH_JWT_SECRET,
#           STORAGE_S3_BUCKET/REGION, OPENROUTER_API_KEY, SES_FROM_EMAIL, SITE_URL
bash deploy-aws-native/scripts/bootstrap.sh    # setup + build + migrate + pm2 + caddy
```

### Phase 3 — DNS, admin, email
```bash
# point timesheet.<domain> A record at <AppPublicIP> (Caddy auto-issues TLS)
deploy-aws-native/scripts/make-admin.sh you@ajace.com   # first admin
deploy-aws-native/scripts/ses-test.sh you@ajace.com      # confirm SES delivery
```

**SES (reset emails):** verify a sender + request production access in the SES
console (both free — new accounts start in sandbox). The instance already has
`ses:SendEmail` via the IAM role (`iam/instance-policy.json`).

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
- **Password reset email** is wired to **Amazon SES** (`lib/aws/email.js`). To turn it on:
  1. In SES, **verify a sender** (`noreply@ajace.com` or your domain — domain verification adds DKIM).
  2. **Request production access** (new SES accounts start in *sandbox* — can only send to verified
     addresses until approved; the request is free and usually approved within a day).
  3. Add `ses:SendEmail` to the EC2 instance IAM role, set `SES_FROM_EMAIL` in `.env.production`.
  If `SES_FROM_EMAIL` is unset, the flow still works — it logs the link instead of emailing.
- **First admin** must be promoted via `make-admin.sh` (no admin exists at first signup).
