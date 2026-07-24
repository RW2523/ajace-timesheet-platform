#!/usr/bin/env bash
# =============================================================================
# ONE-FILE SETUP. Run this on the EC2 box after cloning the repo. It installs
# everything, builds, creates the DB schema, and starts the app. When it
# finishes, the app is LIVE on http://<this-box-ip>.
#
#   cd ajace-timesheet-platform
#   cp deploy-aws-native/env.production.example app/.env.production   # then edit
#   bash deploy-aws-native/scripts/install.sh
#
# Idempotent — safe to re-run (e.g. after editing .env or pulling new code).
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"     # deploy-aws-native
ROOT="$(cd "$HERE/.." && pwd)"               # repo root
ENVF="$ROOT/app/.env.production"

echo "==> [1/6] system packages (node, pm2, caddy, psql, awscli)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
sudo npm i -g pm2 >/dev/null 2>&1 || sudo npm i -g pm2

# psql client + unzip + caddy prereqs (NOTE: Ubuntu 24.04 has no 'awscli' apt pkg)
sudo apt-get update -y
sudo apt-get install -y postgresql-client unzip debian-keyring debian-archive-keyring apt-transport-https curl

# AWS CLI v2 via the official installer (arch-aware)
if ! command -v aws >/dev/null 2>&1; then
  case "$(uname -m)" in aarch64|arm64) AZ=aarch64;; *) AZ=x86_64;; esac
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$AZ.zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install --update
fi

# Caddy (auto-HTTPS reverse proxy)
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y caddy
fi

echo "==> [2/6] environment"
if [ ! -f "$ENVF" ]; then
  cp "$HERE/env.production.example" "$ENVF"
  echo "   created app/.env.production from the template — EDIT IT then re-run."
  echo "   (need: DATABASE_URL, STORAGE_S3_BUCKET, OPENROUTER_API_KEY, SITE_URL)"
  exit 1
fi
# auto-generate the JWT secret if still the placeholder
if grep -q 'CHANGE_ME_openssl' "$ENVF"; then
  SECRET=$(openssl rand -base64 48 | tr -d '\n')
  awk -v s="$SECRET" '/^AUTH_JWT_SECRET=/{print "AUTH_JWT_SECRET="s; next} {print}' "$ENVF" > "$ENVF.tmp" && mv "$ENVF.tmp" "$ENVF"
  echo "   generated AUTH_JWT_SECRET"
fi
set -a; . "$ENVF"; set +a
: "${DATABASE_URL:?set DATABASE_URL in app/.env.production}"
: "${STORAGE_S3_BUCKET:?set STORAGE_S3_BUCKET in app/.env.production}"
if [ -z "${OPENROUTER_API_KEY:-}" ] || [ "${OPENROUTER_API_KEY}" = "sk-or-REPLACE" ]; then
  echo "   ⚠ OPENROUTER_API_KEY not set — app runs, but Direct++ extraction won't work until you set it."
fi

echo "==> [3/6] build the app"
( cd "$ROOT/app" && npm ci && npm run build )

echo "==> [4/6] database schema (RDS)"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$HERE/db/schema.sql"

echo "==> [5/6] start under pm2 (+ boot on reboot)"
pm2 start "$HERE/ecosystem.config.cjs" 2>/dev/null || pm2 restart ajace-timesheet
pm2 save
sudo env PATH="$PATH" pm2 startup systemd -u "$USER" --hp "$HOME" 2>/dev/null | tail -1 | bash || true

echo "==> [6/6] reverse proxy (Caddy, port 80)"
sudo cp "$HERE/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy

IP=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "<this-box-ip>")
echo
echo "✅ Done. App is LIVE at:  http://$IP"
echo "   • make yourself admin:  deploy-aws-native/scripts/make-admin.sh you@ajace.com"
echo "   • control the app:      deploy-aws-native/scripts/app.sh {start|stop|restart|status|logs}"
echo "   • test SES email:       deploy-aws-native/scripts/ses-test.sh you@ajace.com"
