#!/usr/bin/env bash
# Create the ts_ schema (tables, RLS, functions, triggers, storage bucket).
# Idempotent — safe to re-run.  scripts/migrate.sh
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

CID="$(db_cid)"
[ -n "$CID" ] || { echo "db container not running — run scripts/up.sh first"; exit 1; }

echo "== Applying db/01_ts_schema.sql =="
docker exec -i "$CID" env PGPASSWORD="${POSTGRES_PASSWORD}" \
  psql -v ON_ERROR_STOP=1 -U postgres -h 127.0.0.1 -d postgres < db/01_ts_schema.sql

echo "Schema applied. ts_ tables + RLS + ts-uploads bucket are ready."
echo "Optional: scripts/import-from-supabase.sh to bring over existing data."
