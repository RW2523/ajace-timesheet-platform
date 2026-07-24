#!/usr/bin/env bash
# One-shot production bring-up ON the EC2 app host (after the CloudFormation
# stack is up and you've SSH'd in and cloned the repo).
#   1) cp deploy-aws-native/env.production.example app/.env.production   && edit it
#   2) bash deploy-aws-native/scripts/bootstrap.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"        # deploy-aws-native
ROOT="$(cd "$HERE/.." && pwd)"                   # repo root

[ -f "$ROOT/app/.env.production" ] || {
  echo "!! Create app/.env.production first:"
  echo "   cp $HERE/env.production.example $ROOT/app/.env.production   && edit it"
  exit 1
}

echo "== base packages (node/pm2/caddy/psql/awscli) =="
bash "$HERE/scripts/setup.sh"

echo "== build the app =="
( cd "$ROOT/app" && npm ci && npm run build )

echo "== create schema in RDS =="
bash "$HERE/scripts/migrate.sh"

echo "== start under pm2 + enable on boot =="
pm2 start "$HERE/ecosystem.config.cjs"
pm2 save
sudo env PATH="$PATH" pm2 startup systemd -u "$USER" --hp "$HOME" | tail -1 | bash || true

echo "== TLS reverse proxy (Caddy) =="
sudo cp "$HERE/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl reload caddy || sudo systemctl restart caddy

echo
echo "✅ Production bring-up complete."
echo "   1) DNS: point timesheet.<domain> at this box's public IP (Caddy issues TLS automatically)."
echo "   2) First admin:  $HERE/scripts/make-admin.sh you@ajace.com"
echo "   3) SES test:     $HERE/scripts/ses-test.sh you@ajace.com"
echo "   4) Nightly backup cron (optional): $HERE/scripts/backup.sh"
