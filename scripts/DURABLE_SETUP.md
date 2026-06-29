# Durable Mac backend (engine + self-healing tunnel)

Two launchd agents keep the AI backend always running on the Mac:

- **com.ajace.timesheet-engine** — runs the FastAPI engine on :8078
  (`RunAtLoad` + `KeepAlive`, so it starts on login and restarts on crash).
- **com.ajace.timesheet-tunnel** — runs `engine_tunnel.sh`: starts a cloudflared
  quick tunnel and, whenever the URL changes, updates Vercel's `ENGINE_URL` and
  redeploys (via `VERCEL_ORG_ID`/`VERCEL_PROJECT_ID` + `vercel redeploy`, so it
  needs no project files). `KeepAlive` restarts it if cloudflared dies, which
  re-points Vercel automatically.

The script lives in `~/.ajace/` (NOT `~/Documents`, which macOS TCC blocks launchd
from executing). Plists live in `~/Library/LaunchAgents/`.

Install:
```
cp scripts/engine_tunnel.sh ~/.ajace/ && chmod +x ~/.ajace/engine_tunnel.sh
cp scripts/com.ajace.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ajace.timesheet-engine.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ajace.timesheet-tunnel.plist
```

Verify the live site can reach the engine anytime: `GET /api/engine-status`
→ `{"ai_backend":"online","llm_enabled":true}`.

Note: requires the Mac to be powered on and awake. For a URL that never changes
(no redeploys), use a named Cloudflare tunnel instead (needs a domain on Cloudflare
+ `cloudflared tunnel login`).
