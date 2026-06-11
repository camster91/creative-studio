# Creative Studio — Runbook

Operational reference for photogen.ashbi.ca. If you're paged at 2am, start here.

## Quick reference

| Thing | Value |
|---|---|
| Live URL | https://photogen.ashbi.ca |
| Staging URL | https://stage.photogen.ashbi.ca (not yet set up) |
| Server | `coolify` (vps.ashbi.ca / 187.77.26.99) — `ssh coolify` |
| Container name (prod) | `photogen` |
| Container name (stage) | `photogen-stage` (planned) |
| Data dir (prod) | `/root/photogen-data` (bind mount) |
| Outputs dir (prod) | `/root/photogen-outputs` (bind mount) |
| Data dir (stage) | `/root/photogen-stage-data` (bind mount) |
| Outputs dir (stage) | `/root/photogen-stage-outputs` (bind mount) |
| Env file (prod) | `/root/.env.photogen` (chmod 600) |
| Env file (stage) | `/root/.env.photogen-stage` (chmod 600) |
| Caddy block (prod) | `/opt/caddy/Caddyfile` → `photogen.ashbi.ca { reverse_proxy 127.0.0.1:32778 }` |
| Caddy block (stage) | `/opt/caddy/Caddyfile` → `stage.photogen.ashbi.ca { reverse_proxy 127.0.0.1:<port> }` (when set up) |
| Image tag (server-built) | `creative-studio:<sha>` (local Docker, not registry) |
| Backups | `/root/backups/creative-studio/<YYYY-MM-DD>/` |
| Health check | `curl -sf https://photogen.ashbi.ca/api/whoami` |
| Cost tracking | `curl -s https://photogen.ashbi.ca/api/costs` |
| Last deployed SHA | `cab721c` (built 2026-06-11) |

## Architecture

```
GitHub repo (camster91/creative-studio)
    │
    │  rsync or scp source to server
    ▼
VPS (coolify = vps.ashbi.ca = 187.77.26.99)
    │
    │  docker build → tags: creative-studio:<sha>, creative-studio:latest, photogen:latest
    │  docker run --name photogen -p 32778:5173 ...
    │
    ▼
Caddy (systemd, /opt/caddy/Caddyfile)
    │  Host(`photogen.ashbi.ca`) → 127.0.0.1:32778
    │  Auto-TLS via Let's Encrypt HTTP-01
    ▼
gunicorn (1 worker) → Flask app (scripts/creative-studio-web.py)
```

Reverse proxy is **Caddy on the host**, not Traefik (as of 2026-06-11). If you see Traefik config in the repo (`ops/traefik/*`), it's legacy and ignored.

## Deploy procedure (manual)

```bash
# 1. Build a source tarball locally (excludes .venv, .git, build artifacts)
cd ~/projects/creative-studio
tar --exclude='./.venv' --exclude='./__pycache__' --exclude='./.ruff_cache' \
    --exclude='./node_modules' --exclude='./.git' \
    -czf /tmp/photogen-source.tar.gz .

# 2. Copy to server and unpack
scp /tmp/photogen-source.tar.gz coolify:/root/photogen-build/
ssh coolify "cd /root/photogen-build && tar xzf photogen-source.tar.gz"

# 3. Build image with the SHA tag (also :latest + photogen:latest fallback)
ssh coolify "cd /root/photogen-build && \\
  docker build -t creative-studio:\$(git rev-parse --short HEAD) \\
               -t creative-studio:latest \\
               -t photogen:latest ."
# (Use the local SHA of the unpacked source if not a git checkout on the server.)

# 4. Swap the container
ssh coolify "docker stop photogen 2>/dev/null
docker rm photogen 2>/dev/null
docker run -d \\
  --name photogen \\
  --restart unless-stopped \\
  -p 32778:5173 \\
  -v /root/photogen-data:/app/data \\
  -v /root/photogen-outputs:/app/outputs \\
  --env-file /root/.env.photogen \\
  -e CREATIVE_OUTPUT_DIR=/app/outputs \\
  -e CREATIVE_DATA_DIR=/app/data \\
  -e PORT=5173 \\
  photogen:latest"

# 5. Verify
ssh coolify "sleep 4 && docker ps --format '{{.Names}} {{.Status}}' | grep photogen
docker inspect --format='{{.State.Health.Status}}' photogen
docker logs photogen --tail 10
curl -sf http://127.0.0.1:32778/api/whoami"
curl -sf https://photogen.ashbi.ca/api/whoami
```

The Caddy block for `photogen.ashbi.ca` is already in `/opt/caddy/Caddyfile`. No Caddy change needed for routine image updates — only when changing host port.

## Caddy / DNS changes (one-time setup, already done for prod)

- **A record** for `photogen.ashbi.ca` → `187.77.26.99` (Cloudflare, **DNS-only**, not proxied).
- **Caddy block** appended to `/opt/caddy/Caddyfile`:
  ```caddyfile
  # Creative Studio (Photogen) — Flask + gunicorn on 127.0.0.1:32778
  photogen.ashbi.ca {
      reverse_proxy 127.0.0.1:32778
  }
  ```
- Restart: `ssh coolify "systemctl restart caddy"`. Cert auto-issues via Let's Encrypt HTTP-01 (no DNS-01 needed since the apex `ashbi.ca` is on Cloudflare but this subdomain is DNS-only).

## Changing the host port

If `32778` collides with another service, pick a free port in the `32768–60999` range (avoid 80/443/22). Then:

1. Update the `-p <PORT>:5173` flag in the `docker run` block above.
2. Update the Caddyfile `reverse_proxy 127.0.0.1:<PORT>` to match.
3. `ssh coolify "systemctl restart caddy"`.

## Rollback procedure

```bash
ssh coolify

# List recent images
docker images creative-studio --format "table {{.Repository}}:{{.Tag}}\t{{.CreatedAt}}\t{{.ID}}"

# Pick the previous good SHA
PREVIOUS=cab721c

# Stop current, start previous
docker stop photogen && docker rm photogen
docker run -d --name photogen --restart unless-stopped -p 32778:5173 \
  -v /root/photogen-data:/app/data -v /root/photogen-outputs:/app/outputs \
  --env-file /root/.env.photogen \
  -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
  "creative-studio:${PREVIOUS}"

sleep 4
curl -sf https://photogen.ashbi.ca/api/whoami
```

Total rollback time: ~30 seconds.

## Common failure modes

### "502 Bad Gateway" on photogen.ashbi.ca

**Symptom:** Site returns 502, `/api/whoami` hangs or fails.

**Likely cause:** Container crashed or isn't running. Caddy can't reach `127.0.0.1:32778`.

**Fix:**
```bash
ssh coolify
docker ps --format "{{.Names}} {{.Status}}" | grep photogen
docker logs photogen --tail 30
# If container is down:
docker start photogen
# Otherwise follow the "Deploy procedure" above.
```

### Site works but generation returns "API_KEY_INVALID"

**Symptom:** `/api/whoami` returns OK, but `/api/generate` fails with `API key not valid`.

**Likely cause:** User-supplied key (X-API-Key header from the UI) is wrong, expired, or revoked. The server itself runs with **BYOK default** (`CREATIVE_ALLOW_SERVER_FALLBACK=false`), so a broken UI key is the most common cause.

**Fix:** Have the user re-paste their key in the editor sidebar. If you want to verify the server fallback works, set `CREATIVE_ALLOW_SERVER_FALLBACK=true` AND a real `GEMINI_API_KEY` in `/root/.env.photogen`, then restart the container.

### "Daily limit $X reached" error

**Symptom:** `/api/generate` returns 429 with "Daily limit".

**Likely cause:** `CREATIVE_DAILY_LIMIT` in env file was hit (default $5).

**Fix:**
```bash
ssh coolify
# Wait until tomorrow (UTC date changes at 00:00), or:
docker stop photogen
rm /root/photogen-data/costs.json
docker start photogen

# Or raise the limit
echo "CREATIVE_DAILY_LIMIT=20" >> /root/.env.photogen
docker stop photogen && docker rm photogen
docker run -d --name photogen --restart unless-stopped -p 32778:5173 \
  -v /root/photogen-data:/app/data -v /root/photogen-outputs:/app/outputs \
  --env-file /root/.env.photogen \
  -e CREATIVE_OUTPUT_DIR=/app/outputs -e CREATIVE_DATA_DIR=/app/data -e PORT=5173 \
  photogen:latest
```

### "JS is dead — buttons do nothing"

**Symptom:** Page loads, but clicking presets/chips/generate does nothing.

**Likely cause:** A new deploy introduced a JS syntax error. Most common: literal `\u003e` in a raw-string HTML template instead of the actual `>` character. See `test_no_literal_unicode_escapes_in_frontend` in `tests/test_core.py` — if this test fails on the new commit, that's the bug.

**Fix:** Roll back to the previous known-good SHA. Fix the bug locally, run `pytest tests/`, re-deploy.

### Caddy returns 521 (origin down)

**Symptom:** Browser shows "521 Origin Down" from Cloudflare (if you later turn proxy on) or Caddy 502.

**Likely cause:** Container died. Check `docker ps` and `docker logs photogen`.

### TLS cert won't issue

**Symptom:** `caddy validate` says valid, restart succeeds, but `https://photogen.ashbi.ca/` fails with cert error.

**Likely cause:** A record doesn't point to `187.77.26.99` (or it's still propagating), or Cloudflare proxy is intercepting the HTTP-01 challenge.

**Fix:**
```bash
dig +short photogen.ashbi.ca   # must be 187.77.26.99
ssh coolify "find /var/lib/caddy -name 'photogen.ashbi.ca.crt'"  # must exist
ssh coolify "journalctl -u caddy --since '5 min ago' | grep -i acme"
```

## Backup procedure (manual, when cron is down)

```bash
ssh coolify
BACKUP=/root/backups/creative-studio/$(date +%Y-%m-%d)
mkdir -p "$BACKUP"
cp -R /root/photogen-data "$BACKUP/data"
cp -R /root/photogen-outputs "$BACKUP/outputs"
cp /root/.env.photogen "$BACKUP/env"
ls -la "$BACKUP"
```

Restore:
```bash
ssh coolify
docker stop photogen
cp -R /root/backups/creative-studio/<DATE>/data /root/photogen-data
cp -R /root/backups/creative-studio/<DATE>/outputs /root/photogen-outputs
cp /root/backups/creative-studio/<DATE>/env /root/.env.photogen
chmod 600 /root/.env.photogen
docker start photogen
```

The `backup-data.yml` GitHub workflow handles daily snapshots via SSH if it's still wired up. If it's been broken, run this manually.
