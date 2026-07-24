#!/usr/bin/env bash
# Dump the database and upload to S3 (files are already in S3 via the storage
# backend, so this backs up the RECORDS). Run nightly via cron, and it also
# runs automatically before scripts/down.sh.
#   scripts/backup.sh
set -euo pipefail
source "$(dirname "$0")/_compose.sh"

CID="$(db_cid)"
[ -n "$CID" ] || { echo "db not running; nothing to back up"; exit 0; }
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/tmp/ts-db-${STAMP}.sql.gz"

docker exec "$CID" env PGPASSWORD="${POSTGRES_PASSWORD}" \
  pg_dump -U postgres -h 127.0.0.1 -d postgres | gzip > "$OUT"

aws s3 cp "$OUT" "s3://${STORAGE_S3_BUCKET}/db-backups/ts-db-${STAMP}.sql.gz"
rm -f "$OUT"
echo "Backed up -> s3://${STORAGE_S3_BUCKET}/db-backups/ts-db-${STAMP}.sql.gz"
