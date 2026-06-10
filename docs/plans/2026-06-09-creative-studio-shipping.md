# Creative Studio — Shipping Plan (2026-06-09)

> **For Cam:** This is a 3-phase plan to harden the app so it can be shared publicly without embarrassing you. Defaults are listed at the bottom — redirect me if any are wrong, otherwise I start in the next turn.

**Goal:** Make `https://photogen.ashbi.ca` reliable enough that you can share the URL with anyone without it breaking or being down.

**Architecture:** Stabilize the existing Flask + Coolify + GHCR setup. Fix the broken deploy pipeline, add backups, clean up the operational debt. No new features, no marketing polish, no scale work.

**Tech Stack:** Python 3.12 + Flask, Coolify self-hosted on VPS at `187.77.26.99`, Docker, GitHub Actions, GHCR.

---

## Where the leaks are (ranked by leverage)

1. **Smoke test asserts `4.5.1`, live is `4.9.0`** (`.github/workflows/deploy.yml:125`). Every prod deploy fails the smoke test and rolls back, even when healthy. **This is issue #33.** Also blocks you from actually using CI for deploys.
2. **Two redundant deploy workflows** (`deploy.yml` SSH + `deploy-coolify-api.yml` API) fight over the same container name. Whichever runs last wins. They've probably never both fired, but the moment they do, you'll get a confusing "deploy succeeded but container is old" state.
3. **No automated backups.** RUNBOOK.md says backups go to `/root/backups/creative-studio/`, but no cron, no GH Action. The only thing keeping `/root/photogen-data` and `/root/photogen-outputs` is the VPS not failing.
4. **No monitoring or error tracking.** No uptime monitor, no Sentry, no log aggregation. If the app crashes at 2am, you find out when you next try to use it.
5. **`ci.yml` runs pytest with `continue-on-error: true`.** Tests never block. So a real test regression could ship undetected.
6. **8 zero-byte orphan files at repo root** (e.g., `app_id}n;`, `source_id}n;`) — leftover from a shell-injection accident. Not tracked by git but they're on disk and listed by `ls`. Messy and confusing.
7. **No usage analytics.** If a user tries the app, you'll never know.
8. **Stale prod tag.** Only git tag is `v4.5.1-prod`. Any "rollback to last stable" command that defaults to a tag is rolling back 4 versions.

**No payment integration exists.** Pure BYOK Gemini. The "public" launch shape that fits the actual state is "shareable link" — make it not break, don't add Stripe or auth yet.

---

## Phase 0 — Stop the bleeding (1 weekend)

**Objective:** Make the existing deploy pipeline actually work. Anyone who pushes to main should be able to ship a working version.

### Task 0.1: Fix the broken smoke test (issue #33)

**Files:**
- Modify: `.github/workflows/deploy.yml:125`

**Step 1: Read the failing assertion**

```bash
grep -n "4.5.1" .github/workflows/deploy.yml
```

**Step 2: Replace the hardcoded version with a dynamic one**

The smoke test should hit `/api/whoami` and verify a non-empty response + the app renders. Replace the version assertion with:
```yaml
# Old:
- assert d['version'] == '4.5.1', f'expected 4.5.1, got {d["version"]}'
# New:
- assert d.get('version'), f'no version field in whoami response: {d}'
```
The version mismatch is real but the fix isn't "update the version" — it's "stop asserting on a value that drifts with every release." The version is informational; the smoke test should check that the app is *up*.

**Step 3: Add a second smoke test that checks the actual editor renders**

```yaml
- name: Smoke test — editor renders
  run: |
    body=$(curl -sf https://photogen.ashbi.ca/app)
    echo "$body" | grep -q "dropzone" || exit 1
    echo "$body" | grep -q "Generate" || exit 1
    echo "$body" | grep -q "SCENE" || exit 1
```

**Step 4: Verify by re-running the workflow manually**

Use `gh workflow run deploy.yml` from the Actions tab. Should now pass.

**Step 5: Commit**
```bash
git add .github/workflows/deploy.yml
git commit -m "fix(ci): make smoke test version-agnostic (closes #33)"
```

### Task 0.2: Pick one deploy workflow and disable the other

**Files:**
- Modify: `.github/workflows/deploy.yml` OR `.github/workflows/deploy-coolify-api.yml`

**Step 1: Decide which to keep**

The SSH-based one (`deploy.yml`) is the older, simpler workflow. The Coolify API one (`deploy-coolify-api.yml`) is cleaner but uses the manual `workflow_dispatch` only. Recommendation: **disable `deploy-coolify-api.yml` for now** (the SSH one already auto-deploys on push to main). Move the API workflow to a subfolder so it's "archived" but not deleted:

```bash
mkdir -p .github/workflows/archived
git mv .github/workflows/deploy-coolify-api.yml .github/workflows/archived/
git commit -m "chore: archive duplicate deploy workflow (re-enable if needed)"
```

**Step 2: Verify the live deploy.yml still works after the smoke test fix**

### Task 0.3: Clean up orphan files

**Files:**
- Add to: `.gitignore` (a new pattern like `source-*` `source_*` `app_id*` `installation_id*` `private_key_id*` `*n;`)

**Step 1: Delete the 8 zero-byte files**
```bash
cd /Users/biancabienaime/projects/creative-studio
for f in 'app_id}n;\n    echo source' 'installation_id}n;\n    echo source' 'private_key_id}n;\nif (->source) {\n    echo source' 'source-' 'source_id}n;\necho source_type:' 'source_type}n;\necho private_key_id:'; do
  [ -f "$f" ] && rm "$f"
done
ls -la | grep -E 'n;|source-' || echo "all clean"
```

**Step 2: Add `*.bak` and any malformed-name patterns to .gitignore**

Already added `*.bak` in commit `5920665`. Add the malformed-name ones:
```
# Shell-injection accident artifacts
source-*
*source_*
*n;
```

**Step 3: Commit the cleanup**
```bash
git add .gitignore
git rm --cached source- 2>/dev/null || true
git commit -m "chore: clean up orphan files from shell-injection accident"
```

### Task 0.4: Add `.env.example` so the deployment story is documented

**Files:**
- Create: `.env.example`

**Step 1: Create the example file** (no real secrets, just the shape)

```bash
# .env.example — copy to /root/.env on the server
# Required for the web app to start:
GEMINI_API_KEY=AIza-...your-key-here...

# Optional (defaults shown):
# FLASK_SECRET_KEY=                     # falls back to os.urandom(32) per-request
# CREATIVE_OUTPUT_DIR=/app/outputs
# CREATIVE_DATA_DIR=/app/data
# CREATIVE_DAILY_LIMIT=5                 # $ per day per IP, 0 = no limit

# Set this to "true" to use the GEMINI_API_KEY above as a server-side
# fallback for users who don't bring their own. Default is "false" (BYOK only).
# CREATIVE_ALLOW_SERVER_FALLBACK=true
```

**Step 2: Commit**
```bash
git add .env.example
git commit -m "docs: add .env.example for deployment setup"
```

---

## Phase 1 — Make deploys boring (1-2 weekends)

**Objective:** Ship a deploy, get an automated signal if it broke, roll back in 60s if it did.

### Task 1.1: Add basic uptime monitor

**Files:**
- Create: `.github/workflows/uptime-check.yml`

**Use a free service.** Two options ranked by cost:
- **UptimeRobot (free, 5 min interval)** — works without code, set up in their UI
- **GitHub Actions cron (free, every 10 min)** — built into the repo, no external service

**Recommendation: GH Actions cron** so the setup is fully in the repo (no third-party account).

```yaml
# .github/workflows/uptime-check.yml
name: Uptime check
on:
  schedule: [{ cron: '*/10 * * * *' }]
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Hit /api/whoami
        run: |
          code=$(curl -s -o /tmp/r -w "%{http_code}" https://photogen.ashbi.ca/api/whoami)
          if [ "$code" != "200" ]; then
            echo "Uptime check failed: HTTP $code"
            cat /tmp/r
            exit 1
          fi
          # Also verify the editor renders
          curl -sf https://photogen.ashbi.ca/app | grep -q "dropzone" || { echo "Editor missing dropzone"; exit 1; }
```

**Optional:** Add a Discord/Slack webhook step on failure. Skip for now — GH Actions failure emails go to Cam by default.

### Task 1.2: Add a backup cron job

**Files:**
- Create: `.github/workflows/backup-data.yml`

**Back up the data and output directories nightly, retain 7 days.**

```yaml
# .github/workflows/backup-data.yml
name: Backup photogen data
on:
  schedule: [{ cron: '0 3 * * *' }]  # 3am UTC daily
  workflow_dispatch:
jobs:
  backup:
    runs-on: ubuntu-latest
    steps:
      - name: Snapshot data + outputs
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.COOLIFY_HOST }}
          username: ${{ secrets.COOLIFY_USER }}
          key: ${{ secrets.COOLIFY_SSH_KEY }}
          script: |
            cd /root
            tar czf backups/photogen-$(date +%Y%m%d).tar.gz \
              photogen-data photogen-outputs
            # Retain 7 days
            find backups/ -name 'photogen-*.tar.gz' -mtime +7 -delete
            ls -lh backups/ | tail
```

**Note:** This requires the VPS has `tar` and enough disk. Verify before relying on this — `du -sh /root/photogen-data /root/photogen-outputs` first.

### Task 1.3: Make the smoke test in `deploy.yml` actually block

**Files:**
- Modify: `.github/workflows/deploy.yml`

The current `ci.yml` has `continue-on-error: true` on all jobs. That's wrong for a smoke test — the whole point is to block. Remove `continue-on-error` from:
- The pytest job (catch real regressions)
- The smoke test job in deploy.yml (already there, just verify it blocks)

### Task 1.4: Create the v4.9.0 git tag

**Files:**
- Just a tag, no file changes

```bash
git tag -a v4.9.0 -m "v4.9.0: scene-set endpoint + landing polish + bento output"
git push origin v4.9.0
```

Now any "rollback to last stable" defaults to the current production state instead of v4.5.1.

---

## Phase 2 — Observability + shareable link (1 weekend)

**Objective:** Know when something breaks without you checking, and be confident enough to share the URL.

### Task 2.1: Add error tracking (Sentry is overkill; just log to a file)

Skip Sentry for a side project. Instead, log Flask errors to a file the backup cron can include:

**Files:**
- Modify: `scripts/creative-studio-web.py`

**Step 1: Add a file handler to the Flask logger**

```python
import logging
from logging.handlers import RotatingFileHandler
if not app.debug:
    handler = RotatingFileHandler('/app/data/flask-errors.log', maxBytes=10_000_000, backupCount=3)
    handler.setLevel(logging.WARNING)
    app.logger.addHandler(handler)
```

This is 5 lines, no new dependencies (stdlib `logging`), and gives you an error trail you can `tail` over SSH if something breaks.

### Task 2.2: Add a simple "send to a friend" link on the landing page

Right now the landing page CTAs both go to `/app`. The "shareable link" use case just needs the public URL to be obvious. The current state is fine — no work needed. Move to Phase 3.

### Task 2.3: Confirm mobile on real iPhone (one-time, Cam does this)

The audit said mobile passes all automated tests, but real-device testing is the only verification. Open `https://photogen.ashbi.ca/app` on your iPhone. Test:
- Tap "Generate" on a real product photo — confirm it doesn't break
- Touch targets feel right (chips, buttons)
- Sticky Generate button doesn't cover content
- iOS Safari doesn't zoom on input focus

If anything's broken, file an issue. This is one-time, manual, 15 minutes.

---

## Phase 3 — Only if Phase 0-2 is done and you want more (deferred)

These are NOT in this plan. Listed for the "what's pending" file so we don't lose them:

- **URL → brand-aware scene generation** — the Riverflow real deal (2-4 weeks, real work)
- **Stripe integration** — only if you decide to charge (memory says "do not use keys a user pastes, build them a proper Settings tab" — when you do add Stripe, follow that pattern)
- **SEO + landing page marketing polish** — only when you have a real audience
- **Mobile QA on real iPhone** — covered in Phase 2.3
- **CI workflow fix #33** — covered in Phase 0.1
- **Marketing surface mobile QA** — covered in Phase 2.3

---

## What you can defer (explicit NOT-in-scope)

- Adding payments. BYOK is the model.
- Adding auth. No user accounts in this product.
- Adding SEO. The landing page is fine for share-by-URL, not for search.
- Marketing copy / social proof. The 4 sample images ARE the social proof.
- URL → brand-scraping. The actual Riverflow feature, 2-4 weeks. Not shipping work.
- Custom domain. `photogen.ashbi.ca` is already on a real domain.
- Performance optimization. Single user, 5 parallel API calls. No scale problem yet.

---

## What "done" looks like

Phase 0 + 1 done = you can `git push origin main` and trust that:
1. The CI smoke test actually runs and catches a broken deploy
2. The live site has a working backup from last night
3. GH Actions pings you within 10 minutes if the site goes down

Phase 2 done = same as above, plus:
4. You can hand the URL to anyone without 2am pages
5. You have a 60s rollback path if a deploy breaks

End-state: one weekend of work, three commits per phase, the app becomes **boring** (which is the goal of shipping).

---

## Open questions (defaults to execute on)

| Question | Default |
|---|---|
| Keep `deploy.yml` (SSH) or `deploy-coolify-api.yml` (API)? | Keep `deploy.yml` (already auto-deploys), archive the API one. |
| Use UptimeRobot or GH Actions cron for uptime? | GH Actions cron (free, in-repo, no third-party signup). |
| Where should I save backups? | `/root/backups/photogen-<date>.tar.gz` (matches existing RUNBOOK). |
| Add Stripe or stay BYOK? | Stay BYOK. Adding Stripe now is feature creep, not shipping work. |
| Add a Sentry integration? | No — just log to a file. Sentry is overkill for a side project. |
| Should I do this work myself or run a subagent per task? | Subagent per task with two-stage review (subagent-driven-development skill). |
| When does Phase 0 start? | When you say go. I'll send a 1-line "starting Phase 0" before each batch. |

**Default I'll execute if you don't redirect:** Phases 0 + 1 together, in one push, this weekend. Phase 2 next weekend. Phase 3 only on signal.

If any of those are wrong, tell me which. Otherwise I start in 30s.
