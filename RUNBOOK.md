# Creative Studio — Runbook

Operational reference for photogen.ashbi.ca. If you're paged at 2am, start here.

## Quick reference

| Thing | Value |
|---|---|
| Live URL | https://photogen.ashbi.ca |
| Staging URL | https://stage.photogen.ashbi.ca (when set up) |
| Server | `coolify` (187.77.26.99) — `ssh coolify` |
| Container name (prod) | `photogen` |
| Container name (stage) | `photogen-stage` |
| Data dir (prod) | `/root/photogen-data` (bind mount) |
| Outputs dir (prod) | `/root/photogen-outputs` (bind mount) |
| Data dir (stage) | `/root/photogen-stage-data` (bind mount) |
| Outputs dir (stage) | `/root/photogen-stage-outputs` (bind mount) |
| Env file (prod) | `/root/.env.photogen` (chmod 600) |
| Env file (stage) | `/root/.env.photogen-stage` (chmod 600) |
| Traefik config (prod) | `/data/coolify/proxy/dynamic/photogen.yml` |
| Traefik config (stage) | `/data/coolify/proxy/dynamic/photogen-stage.yml` |
| Image registry | `ghcr.io/camster91/creative-studio:<sha>` |
| Backups | `/root/backups/creative-studio/<YYYY-MM-DD>/` |
| Health check | `curl -sf https://photogen.ashbi.ca/api/whoami` |
| Cost tracking | `curl -s https://photogen.ashbi.ca/api/costs` |
| Last stable tag | `v4.5.1-prod` (git tag, points at commit `142ed75`) |

## Architecture

```
GitHub repo (camster91/creative-studio)
    │
    │  push to main
    ▼
GitHub Actions workflow (.github/workflows/deploy.yml)
    │
    │ 1. build image → 2. push to ghcr.io → 3. SSH to Coolify
    ▼
Coolify server (187.77.26.99)
    │
    │ docker pull ghcr.io/camster91/creative-studio:<sha>
    │ docker stop photogen && docker rm photogen
    │ docker run --name photogen --env-file /root/.env.photogen ...
    │
    ▼
Traefik (dynamic config: /data/coolify/proxy/dynamic/photogen.yml)
    │  Host(`photogen.ashbi.ca`) → http://photogen:5173
    ▼
gunicorn (2 workers) → Flask app (scripts/creative-studio-web.py)
```

## Deploy procedure (manual, when GitHub Actions is down)

```bash
# 1. SSH to the server
ssh coolify

# 2. Pull latest code
cd /root/repos/creative-studio
git fetch origin main
git reset --hard origin/main
SHA=$(git rev-parse --short HEAD)

# 3. Build image with the SHA tag (also tag as :latest for fallback)
docker build -t "creative-studio:${SHA}" -t creative-studio:latest -t photogen:latest .

# 4. Swap the container
docker stop photogen 2>/dev/null
docker rm photogen 2>/dev/null
docker run -d \
  --name photogen \
  --network coolify \
  --restart unless-stopped \
  -p 5173:5173 \
  -v /root/photogen-data:/app/data \
  -v /root/photogen-outputs:/app/outputs \
  --env-file /root/.env.photogen \
  -e CREATIVE_OUTPUT_DIR=/app/outputs \
  -e CREATIVE_DATA_DIR=/app/data \
  -e PORT=5173 \
  -l traefik.enable=true \
  -l "traefik.http.routers.https-0-photogen.rule=Host(\`photogen.ashbi.ca\`)" \
  -l traefik.http.routers.https-0-photogen.tls=true \
  -l traefik.http.routers.https-0-photogen.tls.certresolver=letsencrypt \
  -l traefik.http.routers.https-0-photogen.entrypoints=https \
  -l traefik.http.services.https-0-photogen.loadbalancer.server.port=5173 \
  -l coolify.managed=true \
  photogen:latest

# 5. Verify
sleep 5
docker ps --format "{{.Names}} {{.Status}}" | grep photogen
docker logs photogen --tail 10
curl -sf http://localhost:5173/api/whoami
```

If `/api/whoami` returns 200, deploy is good. The Traefik dynamic file at `/data/coolify/proxy/dynamic/photogen.yml` references the container by name, not IP, so it routes to whatever container is currently named `photogen`.

## Rollback procedure

```bash
ssh coolify

# List recent images
docker images creative-studio --format "table {{.Repository}}:{{.Tag}}\t{{.CreatedAt}}\t{{.ID}}"

# Pick the previous good SHA. Example: 142ed75
PREVIOUS=142ed75

# Stop current, start previous
docker stop photogen
docker rm photogen
docker run -d \
  --name photogen \
  --network coolify \
  --restart unless-stopped \
  -p 5173:5173 \
  -v /root/photogen-data:/app/data \
  -v /root/photogen-outputs:/app/outputs \
  --env-file /root/.env.photogen \
  -e CREATIVE_OUTPUT_DIR=/app/outputs \
  -e CREATIVE_DATA_DIR=/app/data \
  -e PORT=5173 \
  photogen:${PREVIOUS}

sleep 5
curl -sf https://photogen.ashbi.ca/api/whoami
```

Total rollback time: ~30 seconds.

## Common failure modes

### "502 Bad Gateway" on photogen.ashbi.ca

**Symptom:** Site returns 502, /api/whoami hangs or fails.

**Likely cause:** Container crashed or isn't running. Traefik can't reach it.

**Fix:**
```bash
ssh coolify
docker ps --format "{{.Names}} {{.Status}}" | grep photogen
docker logs photogen --tail 30
# If container is down:
docker start photogen   # if it exists but is stopped
# or follow the "Deploy procedure" above to start fresh
```

### Site works but generation returns "API_KEY_INVALID"

**Symptom:** `/api/whoami` returns OK, but `/api/generate` fails with `API key not valid`.

**Likely cause:** Either the key in `/root/.env.photogen` has been revoked/expired, or no user-supplied key is set and the server fallback is broken.

**Fix:**
```bash
# Test the server's fallback key
ssh coolify
docker exec photogen env | grep GEMINI_API_KEY  # check the key is set
# User can also paste their own key in the UI sidebar
# If the server key is broken, replace it:
# 1. Get a new key from https://aistudio.google.com/app/apikey
# 2. Edit /root/.env.photogen on the server
# 3. Restart the container
docker stop photogen && docker rm photogen
docker run -d --name photogen ... (same flags as deploy)
```

### "Daily limit $X reached" error

**Symptom:** `/api/generate` returns 429 with "Daily limit".

**Likely cause:** `CREATIVE_DAILY_LIMIT` in env file was hit (default $5).

**Fix:**
```bash
# Either wait until tomorrow (UTC date changes at 00:00), or:
ssh coolify
# Reset the costs file (last resort)
docker stop photogen
rm /root/photogen-data/costs.json
docker start photogen

# Or raise the limit
echo "CREATIVE_DAILY_LIMIT=20" >> /root/.env.photogen
# then restart the container
```

### "JS is dead — buttons do nothing"

**Symptom:** Page loads, but clicking presets/chips/generate does nothing.

**Likely cause:** A new deploy introduced a JS syntax error. Most common: literal `\u003e` in a raw-string HTML template instead of the actual `>` character. See `test_no_literal_unicode_escapes_in_frontend` in `tests/test_core.py` — if this test fails on the new commit, that's the bug.

**Fix:** Roll back to the previous known-good SHA. Fix the bug locally, run `pytest tests/`, re-deploy.

## Backup procedure (manual, when cron is down)

```bash
ssh coolify
DATE=$(date +%F)
mkdir -p /root/backups/creative-studio/$DATE/{data,outputs,env}
tar czf /root/backups/creative-studio/$DATE/data/sessions-and-costs.tgz -C /root/photogen-data .
tar czf /root/backups/creative-studio/$DATE/outputs/outputs.tgz -C /root/photogen-outputs .
cp /root/.env.photogen /root/backups/creative-studio/$DATE/env/env.photogen.$DATE
chmod 600 /root/backups/creative-studio/$DATE/env/env.photogen.$DATE
```

Restoration: extract the tarballs back to `/root/photogen-data` and `/root/photogen-outputs`, restart the container.

## Log locations

- App stdout/stderr: `docker logs photogen` (no log shipping, captured by Coolify)
- Gunicorn access: same
- Costs/sessions: `/root/photogen-data/costs.json` and `/root/photogen-data/sessions/*.json`
- Outputs: `/root/photogen-outputs/YYYY-MM-DD/<mode>/*.png`

## Cost monitoring

```bash
# Today's spend
curl -s https://photogen.ashbi.ca/api/costs | python3 -m json.tool

# Per-model breakdown
curl -s https://photogen.ashbi.ca/api/costs | jq '.by_model'

# Recent sessions
ssh coolify 'ls -lt /root/photogen-data/sessions/ | head -5'
```

## When all else fails

The service is a single container. Worst case: it can be rebuilt from a git tag in 5 minutes. Data loss is bounded by how often you back up (currently: manually on deploy). Phase 3 of the shipping plan adds automated daily backups.

If you need to start completely fresh:
```bash
ssh coolify
cd /root/repos/creative-studio
git fetch --tags
git checkout v4.5.1-prod
# then run the "Deploy procedure" from a known-good tag
```
