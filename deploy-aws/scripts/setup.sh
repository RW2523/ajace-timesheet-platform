#!/usr/bin/env bash
# One-time bootstrap on a fresh Ubuntu 24.04 EC2 box.
#   bash scripts/setup.sh
# Installs Docker + swap, clones the official Supabase self-host stack, and
# builds deploy-aws/.env from the official example + our app/S3 additions.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"

echo "== Docker =="
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "NOTE: log out/in once so the docker group applies (or run: newgrp docker)."
fi

echo "== Swap (4G) — cushions the small box during builds/spikes =="
if ! sudo swapon --show 2>/dev/null | grep -q '/swapfile'; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== awscli (for S3 backups) =="
command -v aws >/dev/null 2>&1 || sudo apt-get update -y && sudo apt-get install -y awscli git

echo "== Clone official Supabase self-host stack =="
if [ ! -d supabase-docker ]; then
  rm -rf .supabase-src
  git clone --depth 1 --filter=blob:none --sparse https://github.com/supabase/supabase .supabase-src
  ( cd .supabase-src && git sparse-checkout set docker )
  cp -r .supabase-src/docker supabase-docker
  rm -rf .supabase-src
  echo "Cloned supabase/docker -> supabase-docker/"
fi

echo "== Build .env =="
if [ ! -f .env ]; then
  cp supabase-docker/.env.example .env
  printf '\n\n# ===== app/S3 additions (appended by setup.sh) =====\n' >> .env
  cat env.app.example >> .env
  echo ""
  echo ">>> Created deploy-aws/.env"
  echo ">>> EDIT IT before continuing (README §4): set POSTGRES_PASSWORD, JWT_SECRET,"
  echo ">>> ANON_KEY, SERVICE_ROLE_KEY, the api/site URLs, S3 bucket, and OPENROUTER_API_KEY."
else
  echo ".env already exists — leaving it as is."
fi

echo
echo "Setup complete. Next:"
echo "  1) edit deploy-aws/.env   (README §4)"
echo "  2) scripts/up.sh          (build + start everything)"
echo "  3) scripts/migrate.sh     (create the ts_ schema)"
echo "  4) scripts/smoke.sh       (verify)"
