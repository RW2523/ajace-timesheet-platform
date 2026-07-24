#!/usr/bin/env bash
# Build + start the whole stack (Supabase + Storage(S3) + Timesheet app + Caddy).
#   scripts/up.sh
# Set LEAN=1 to start only the minimal service set (budget / t4g.small). Lean
# mode may need the analytics-dependency note in README §8 depending on the
# upstream compose version — verify with scripts/smoke.sh.
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

if [ "${LEAN:-0}" = "1" ]; then
  echo "== LEAN mode: db kong auth rest storage imgproxy meta timesheet caddy =="
  dc up -d --build db kong auth rest storage imgproxy meta timesheet caddy
else
  echo "== FULL stack (reliable; recommended on t4g.medium) =="
  dc up -d --build
fi

echo
echo "Started. Give it ~30–60s, then:"
echo "  scripts/migrate.sh   # first time only — creates the ts_ schema"
echo "  scripts/smoke.sh     # health check"
