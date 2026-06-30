#!/bin/bash
# Self-healing cloudflared tunnel for the timesheet engine (lives outside
# ~/Documents so launchd is allowed to exec it). On each (re)start it brings up a
# tunnel and, if the public URL changed, updates Vercel's ENGINE_URL and triggers
# a redeploy via project IDs + `vercel redeploy` (no project files needed, so no
# ~/Documents access required).
export PATH="/opt/homebrew/bin:/Users/richardwatsonstephenamudha/.nvm/versions/node/v18.20.8/bin:/usr/bin:/bin"
export VERCEL_ORG_ID="team_TjrgIf7wOazD9zjuNwL20Isf"
export VERCEL_PROJECT_ID="prj_9ULv1EnVXMt60D2mmm1R5LEgEoBD"

URL_FILE="$HOME/.ajace/engine_tunnel_url"
CF_OUT="/tmp/ajace-cf.log"
LOG="/tmp/ajace-tunnel.log"
V="npx --yes vercel@latest"

echo "$(date '+%F %T') --- tunnel wrapper starting ---" >> "$LOG"

# Wait for the engine (its own launchd agent) to be healthy.
for i in $(seq 1 30); do
  curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8078/api/health && break
  sleep 2
done

# Start the quick tunnel and read the assigned URL.
: > "$CF_OUT"
cloudflared tunnel --url http://localhost:8078 >> "$CF_OUT" 2>&1 &
CF_PID=$!
URL=""
for i in $(seq 1 40); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$CF_OUT" | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
echo "$(date '+%F %T') tunnel URL: ${URL:-<none>}" >> "$LOG"

# When the URL changes, point Vercel at it and redeploy to apply the new value.
OLD=$(cat "$URL_FILE" 2>/dev/null)
if [ -n "$URL" ] && [ "$URL" != "$OLD" ]; then
  echo "$URL" > "$URL_FILE"
  $V env rm ENGINE_URL production --yes >> "$LOG" 2>&1
  printf "%s" "$URL" | $V env add ENGINE_URL production >> "$LOG" 2>&1
  DEP=$($V ls --prod --yes 2>>"$LOG" | grep -oE 'https://[a-z0-9.-]+\.vercel\.app' | head -1)
  if [ -n "$DEP" ]; then
    $V redeploy "$DEP" --target production --non-interactive >> "$LOG" 2>&1
    echo "$(date '+%F %T') re-pointed Vercel + redeployed $DEP -> $URL" >> "$LOG"
  else
    echo "$(date '+%F %T') WARN: could not find prod deployment to redeploy" >> "$LOG"
  fi
fi

# Health-monitor the tunnel. A quick tunnel can go DEAD while cloudflared stays
# alive (e.g. after the Mac sleeps or the network changes), so we actively probe
# the public URL -- not just whether the process is up. On a dead tunnel we kill
# cloudflared and exit, so launchd restarts us with a fresh tunnel + Vercel sync.
fails=0
while kill -0 "$CF_PID" 2>/dev/null; do
  sleep 30
  if curl -sf -o /dev/null --max-time 12 "$URL/api/health"; then
    fails=0
  else
    fails=$((fails + 1))
    echo "$(date '+%F %T') tunnel probe failed ($fails) for $URL" >> "$LOG"
    if [ "$fails" -ge 2 ]; then
      echo "$(date '+%F %T') tunnel dead -> killing cloudflared so launchd restarts" >> "$LOG"
      kill "$CF_PID" 2>/dev/null
      break
    fi
  fi
done
echo "$(date '+%F %T') wrapper ending (launchd will restart)" >> "$LOG"
