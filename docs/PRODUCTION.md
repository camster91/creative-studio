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
