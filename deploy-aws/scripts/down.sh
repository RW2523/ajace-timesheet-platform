#!/usr/bin/env bash
# Stop everything (keeps data in Docker volumes + S3). Takes a safety DB backup
# to S3 first. Restart later with scripts/up.sh — data is intact.
#   scripts/down.sh
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

echo "== safety backup before stopping =="
"$(dirname "$0")/backup.sh" || echo "(backup skipped/failed — continuing)"

echo "== stopping containers =="
dc stop
echo "Stopped. DB is on the EBS-backed Docker volume; files are in S3."
echo "To also free the EC2 compute cost, stop the instance in the AWS console."
