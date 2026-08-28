# Production operations

## Required configuration

The container fails closed when optional trust boundaries are not configured.

| Variable | Purpose |
| --- | --- |
| `FLASK_SECRET_KEY` | Stable, randomly generated application secret |
| `PUBLIC_URL` | Public HTTPS origin used in email messages |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `MAGIC_LINK_FROM` | TLS email delivery for single-use login tokens |
| `PHOTOGEN_ADMIN_SECRET` | Operator-only shared secret; store only in the deployment secret manager |
| `CREATIVE_DAILY_LIMIT` | Maximum daily provider spend |
| `CREATIVE_OUTPUT_DIR`, `CREATIVE_DATA_DIR` | Persistent mounted storage |
| `CREATIVE_MAX_UPLOAD_BYTES` | Encoded upload limit; defaults to 16 MiB |
| `CREATIVE_MAX_IMAGE_DIMENSION`, `CREATIVE_MAX_IMAGE_PIXELS` | Decoded image-bomb limits |
| `CREATIVE_UPLOAD_RETENTION_DAYS` | Canonical upload expiry; defaults to 30 days |
| `CREATIVE_QC_ESTIMATED_COST_USD` | Operator-maintained per-review cost estimate returned by QC |

`CREATIVE_EXPOSE_MAGIC_LINK_TOKEN` and `CREATIVE_ALLOW_UNOWNED_ASSETS` are
test/migration switches and must not be set in production. Shared Figma access
also remains disabled; the web product requires per-user OAuth before it can be
enabled safely.

## Release and rollback

Pull requests run CI only. A merge to `main` builds an immutable SHA-tagged
image, runs container smoke checks, deploys staging, and then deploys the same
SHA to the protected production environment. A manual production run must name
the production target. Failed smoke checks stop the chain.

Before deployment, record the currently running image digest. Roll back by
starting that exact digest with the same read-only configuration and persistent
mounts, then verify `/api/whoami`, the landing page, login delivery, and one
non-billable authenticated read. Do not use `latest` for rollback.

## Data, logs, and backups

The service stores account/project SQLite data, owner-scoped session JSON,
uploads, generated outputs, and rotating warning/error logs. Logs must not
contain API keys, session tokens, email login tokens, prompts, or image bytes.
Retain uploads and outputs only for the documented customer retention period;
deletion must cover sidecars and backups.

`request-metrics.jsonl` contains only a random/caller-safe request ID, HTTP
method, Flask route template (never concrete object IDs or query strings),
status, and latency. `/status` and `/history` are operator-authenticated because
they contain host-wide spend/job state and session prompts. Provider and billing
exceptions are mapped to stable public messages; raw stderr and third-party
exception text must not be returned to browsers.

Batch generation state is stored in `jobs.db` under the persistent data
directory. Callers must reuse the `Idempotency-Key` header when retrying the
same request; a key cannot be reused for a different payload. Job reads and
`POST /api/jobs/<id>/cancel` are owner-scoped. Cancellation is cooperative:
an in-flight provider call may finish, its result and cost are retained, and no
new call starts afterward. Provider calls have a 300-second process timeout and
there are no automatic paid-call retries. On restart, interrupted jobs become
`failed` with `service_restarted` (or `cancelled` when already requested), while
partial results and actual cost remain queryable. `CREATIVE_MAX_JOB_COST`
defaults to `1.00` and rejects an oversized batch before the provider is called.
Set `CREATIVE_DURABLE_JOBS_ENABLED=false` to roll batch requests back to the
legacy synchronous path without affecting single-image generation.

All browser uploads are decoded, bounded, metadata-stripped, and re-encoded as
canonical PNG files. Their `.meta.json` sidecars record owner, purpose, source
format, dimensions, creation, and expiry. Preview cleanup without deletion:

```bash
python scripts/purge-expired-uploads.py --upload-dir /app/data/uploads
```

After reviewing the list, execute it with `--execute`. Schedule that exact
command daily in the production scheduler; malformed or traversal sidecars are
ignored rather than followed.

Nightly local backups are not sufficient evidence of recovery. Production
requires encrypted off-host backups, retention limits, and a scheduled restore
test to an isolated directory. Record restore time and integrity results.

## Incident controls

The immediate kill switch is to disable provider credentials/server fallback
and stop the production container while retaining persistent volumes. Rotate
affected Gemini, Figma, SMTP, admin, GitHub, registry, and deploy credentials as
applicable. Preserve logs, identify owner-scoped records affected, restore the
last known-good immutable image, and document the incident before re-enabling
generation.
