#!/usr/bin/env bash
# Promote a user to timesheet admin.  scripts/make-admin.sh you@ajace.com
# Runs as the `postgres` superuser (exempt from the role-change guard).
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

EMAIL="${1:-}"; [ -n "$EMAIL" ] || { echo "usage: scripts/make-admin.sh <email>"; exit 1; }
CID="$(db_cid)"; [ -n "$CID" ] || { echo "db not running — scripts/up.sh first"; exit 1; }

docker exec -i "$CID" env PGPASSWORD="${POSTGRES_PASSWORD}" \
  psql -v ON_ERROR_STOP=1 -U postgres -h 127.0.0.1 -d postgres \
  -c "update public.ts_profiles set role='admin' where email='${EMAIL}';" \
  -c "select email, role from public.ts_profiles where email='${EMAIL}';"
