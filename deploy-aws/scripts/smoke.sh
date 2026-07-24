#!/usr/bin/env bash
# Quick health check of the running stack.  scripts/smoke.sh
set -uo pipefail
source "$(dirname "$0")/_compose.sh"

echo "== containers =="
dc ps

echo
echo "== Supabase auth health (Kong :8000) =="
curl -fsS localhost:8000/auth/v1/health >/dev/null && echo "  auth: OK" || echo "  auth: FAIL (still starting?)"

echo "== Supabase REST (Kong :8000) =="
KEY="${ANON_KEY:-${NEXT_PUBLIC_SUPABASE_ANON_KEY:-}}"
curl -fsS localhost:8000/rest/v1/ts_app_settings?select=key -H "apikey: ${KEY}" >/dev/null \
  && echo "  rest+db: OK (ts_app_settings reachable)" || echo "  rest+db: FAIL (run scripts/migrate.sh?)"

echo "== Timesheet app container =="
if dc ps --status running | grep -q ajace-timesheet-app; then echo "  app: running"; else echo "  app: NOT running"; fi

echo
echo "End-to-end (do in a browser): open your site -> Sign up -> upload a timesheet"
echo "  -> confirm a row appears in ts_files and the object appears in:"
echo "     aws s3 ls s3://${STORAGE_S3_BUCKET:-<bucket>}/ts-uploads/ --recursive"
