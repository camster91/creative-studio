# Creative Studio — Runbook

Operational reference for photogen.ashbi.ca. If you're paged at 2am, start here.

## Quick reference

| Thing | Value |
|---|---|
| Live URL | https://photogen.ashbi.ca |
| Staging URL | Internal host check on `127.0.0.1:5174`; public staging DNS is not configured |
| Server | `coolify` (vps.ashbi.ca / 187.77.26.99) — `ssh coolify` |
| Container name (prod) | `photogen` |
| Container name (stage) | `photogen-stage` |
| Data dir (prod) | `/root/photogen-data` (bind mount) |
| Outputs dir (prod) | `/root/photogen-outputs` (bind mount) |
| Data dir (stage) | `/root/photogen-stage-data` (bind mount) |
| Outputs dir (stage) | `/root/photogen-stage-outputs` (bind mount) |
| Env file (prod) | `/root/.env.photogen` (chmod 600) |
| Env file (stage) | `/root/.env.photogen-stage` (chmod 600) |
| Caddy block (prod) | `/opt/caddy/Caddyfile` → `photogen.ashbi.ca { reverse_proxy 127.0.0.1:32778 }` |
| Caddy block (stage) | None; staging is intentionally verified over SSH on host port `5174` |
| Image tag | `ghcr.io/camster91/creative-studio:<full-sha>` |
| Local backups | `/root/backups/creative-studio/photogen-<timestamp>-<run>.tar.gz` |
| Off-host backups | Encrypted GitHub Actions artifact, 14-day retention |
| Health check | `curl -sf https://photogen.ashbi.ca/api/whoami` |
| Cost tracking | `curl -s https://photogen.ashbi.ca/api/costs` |
| Last deployed SHA | `0aa3bcaac3c469347f8c16068245a54c156aaebb` (verified 2026-09-01) |

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

Reverse proxy is **Caddy on the host**, not Traefik (as of 2026-06-11). The Traefik config in the repo was removed (no `ops/traefik/*` files remain).

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

## Backup and restore

The authoritative scheduled path is `.github/workflows/backup-data.yml`. It:

1. uses SQLite's online backup API for every persistent database;
2. copies regular data/output files while rejecting symlinks;
3. writes a size/SHA-256 manifest and checks every copied SQLite database;
4. encrypts the archive with `age` before off-host storage;
5. decrypts into an isolated runner and verifies the complete manifest; and
6. retains the encrypted Actions artifact for 14 days and local archives for
   seven days.

The recovery recipient is `.github/backup-recipients.txt`. Its current SSH key
fingerprint is `SHA256:+pJjM5Sq5bK9Xe2yvhwLc/BIWZuld5RNYEl5n4YqN8A`.
Keep the matching private recovery key outside the VPS. When rotating it, retain
the old identity until every backup encrypted to it has expired or been
reencrypted.

Trigger and inspect a controlled drill:

```bash
gh workflow run backup-data.yml --ref main
gh run list --workflow "Backup photogen data" --limit 3
gh run view <RUN_ID> --log
```

Download the selected encrypted artifact, decrypt it into an isolated empty
directory, then run the verifier before replacing any production path:

```bash
age --decrypt --identity /secure/path/to/recovery-key photogen-<RUN>.tar.gz.age \
  | tar -xz -C /isolated/restore
PYTHONPATH=. python scripts/backup-photogen.py verify \
  --snapshot-dir /isolated/restore/snapshot-<RUN>
```

Only after verification should an operator stop `photogen`, preserve the
current directories, copy the verified `data/` and `outputs/` trees into place,
restore ownership to `1000:1000`, restart the exact intended image, and verify
the public and authenticated journeys.

### Legacy manual snapshot (when Actions is unavailable)

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

This legacy copy is local-only and is not a substitute for the encrypted
off-host workflow or an isolated restore drill.

## Photoshoot spend and recovery

For `/shoot`, configure the server image provider with `PHOTOGEN_IMAGE_PROVIDER`
(`auto`, `higgsfield`, or `gemini`). Keep all credentials outside the repository.
Higgsfield accepts no user Gemini key; it requires an account and PhotoGen credits.

The shared `CREATIVE_DAILY_LIMIT` ledger now includes each successful paid
Higgsfield output. Its estimates default to $0.08 / $0.12 / $0.72 USD per image
at Balanced / High / Ultra. Override with `HIGGSFIELD_COST_USD_BALANCED`,
`HIGGSFIELD_COST_USD_QUALITY`, `HIGGSFIELD_COST_USD_ULTRA` to match your provider
account. Only positive finite estimates are accepted; invalid values fall back
to defaults. Failed renders and local cutouts do not add spend.

Pack JSON files live under `data/packs/`. Restart recovery fails unfinished
outputs and returns their credits. `photoshoot_refunds` in the auth database
records a unique pack/shot refund key atomically with the balance update, so a
crash before JSON settlement cannot duplicate the refund. Preserve that table
with the existing auth DB backup. Polls and ZIP downloads remain free.
