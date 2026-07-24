# Sourced by the other scripts — one place that defines the compose invocation.
# Runs from deploy-aws/ with a single --env-file so BOTH compose files resolve
# their ${VARS} from the same .env, and --project-directory pins relative paths.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
set -a; [ -f .env ] && . ./.env; set +a
dc() {
  docker compose --env-file .env --project-directory . \
    -f supabase-docker/docker-compose.yml \
    -f app.compose.yml "$@"
}
db_cid() { dc ps -q db; }
