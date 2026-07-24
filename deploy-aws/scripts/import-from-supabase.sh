#!/usr/bin/env bash
# OPTIONAL / ADVANCED — bring EXISTING data across from your current hosted
# Supabase. For a demo/pilot you can skip this entirely and start fresh
# (employees re-register; the ts_handle_new_user trigger recreates profiles).
#
# Because ts_* rows reference auth.users(id), a data import must include the
# matching auth users. This copies BOTH. Review before running in anger.
#
#   SOURCE_DB_URL='postgresql://postgres.<ref>:<pwd>@<host>:6543/postgres' \
#     scripts/import-from-supabase.sh
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

: "${SOURCE_DB_URL:?set SOURCE_DB_URL to your current Supabase connection string}"
CID="$(db_cid)"; [ -n "$CID" ] || { echo "run scripts/up.sh first"; exit 1; }
command -v pg_dump >/dev/null || { echo "install postgresql-client: sudo apt-get install -y postgresql-client"; exit 1; }

echo "== 1/3 auth users (so ts_ FKs resolve) =="
pg_dump "$SOURCE_DB_URL" --data-only --no-owner --no-privileges \
  -t 'auth.users' -t 'auth.identities' > /tmp/ts_auth.sql
docker exec -i "$CID" env PGPASSWORD="${POSTGRES_PASSWORD}" \
  psql -U postgres -h 127.0.0.1 -d postgres < /tmp/ts_auth.sql || \
  echo "  (some auth rows may already exist — that's fine)"

echo "== 2/3 ts_ tables =="
pg_dump "$SOURCE_DB_URL" --data-only --no-owner --no-privileges \
  -t 'public.ts_profiles' -t 'public.ts_files' -t 'public.ts_timesheets' \
  -t 'public.ts_employee_edits' -t 'public.ts_admin_edits' > /tmp/ts_data.sql
docker exec -i "$CID" env PGPASSWORD="${POSTGRES_PASSWORD}" \
  psql -U postgres -h 127.0.0.1 -d postgres < /tmp/ts_data.sql

echo "== 3/3 files: copy the storage objects into your S3 bucket =="
echo "Run (with creds for BOTH sides), preserving the {userId}/{YYYY-MM}/ paths:"
echo "  # from the source Supabase storage (S3-compatible) to your bucket:"
echo "  aws s3 sync s3://<source-bucket>/ts-uploads/ s3://${STORAGE_S3_BUCKET}/ts-uploads/"
echo
rm -f /tmp/ts_auth.sql /tmp/ts_data.sql
echo "Import done. Verify with scripts/smoke.sh and a browser login."
