# Creative Studio agent instructions

## Product

Creative Studio is a CPG/DTC AI product-photography system with two related surfaces in one repository:

- a CLI skill for power users/research workflows; and
- the `photogen.ashbi.ca` web application for repeatable product-image generation and Campaign Factory workflows.

The product handles brand/SKU truth, uploaded product imagery, prompts, generated assets, campaign/channel bundles, cost controls, and owner-scoped history. Treat client/brand assets, prompts, generated outputs, provider credentials, cost data, and campaign metadata as sensitive.

Do not turn generated imagery or deterministic preflight results into claims of marketplace compliance, legal approval, brand approval, factual product truth, or autonomous publishing. Human review remains a release/delivery boundary.

## Authority and reading order

Use repository sources in this order:

1. `AGENTS.md` — durable coding-agent execution and safety rules.
2. `README.md` — current product surfaces, shipped/in-progress scope, setup, and documentation links.
3. `docs/ARCHITECTURE.md` and relevant architecture/state documents — authoritative module and state boundaries.
4. `RUNBOOK.md` and `docs/PRODUCTION.md` — current operational/deployment/rollback authority.
5. `ROADMAP.md` and current GitHub issues — planned implementation work.
6. `.github/workflows/*` and current scripts/Makefile — mechanical CI/release behaviour.
7. Current pull requests and exact-head evidence — live implementation and verification state.

When operational documentation conflicts, `RUNBOOK.md` controls the live `photogen.ashbi.ca` deployment procedure unless a newer explicitly authoritative production document says otherwise. The VPS host is named `coolify`, but current reverse proxy/deployment authority uses Docker plus host Caddy; do not infer that the application is managed by the Coolify platform merely from the hostname.

Historical or stacked PR descriptions are evidence for their exact branch only. They do not silently become current `main` requirements or production truth.

## Architecture

- Python 3.12 application.
- Flask/gunicorn web backend with independently tested service modules and route blueprints under `creative_studio_app/`.
- Vanilla JavaScript web UI.
- SQLite-backed durable application/campaign state plus mounted filesystem data/output volumes.
- Bounded in-process workers for generation lifecycle work.
- Google Gemini provider integration with BYOK/shared-key modes governed by current security/cost controls.
- Separate CLI tooling under `cli/`.
- Docker production image; current live reverse proxy is host Caddy as documented in `RUNBOOK.md`.

Keep route entrypoints thin and preserve owner scoping across sessions, campaigns, assets, pins, chats, generation jobs, bundles, downloads, provider ledger/cost records, and other user-owned state.

## Environment and setup

Use repository-relative paths. Do not assume a particular workstation checkout path.

Representative local setup uses `uv` and the Makefile:

```bash
make install
make dev
```

`make install` creates `.venv` with Python 3.12 and installs the project with test dependencies. Use `.env.example` for configuration names/placeholders only.

Never commit or print real Gemini keys, Figma tokens, auth/session secrets, email/provider secrets, production env files, client product assets, private prompts, private generated imagery, or production database/output contents.

The CLI has its own dependency/setup path under `cli/`; use its locked/current repository instructions rather than silently installing unrelated global packages.

## Verification

Use the repository's real layered gates rather than inventing a pass.

Representative source checks:

```bash
make test
make test-js
make lint
```

`make lint` currently runs the Python test suite plus JavaScript syntax validation. Additional focused Python, JavaScript, Playwright/browser, container, security, backup, Campaign Factory, channel-bundle, and release checks may be required by the changed scope and current workflows.

For release-adjacent work, inspect exact-head CI/Ashbi Local CI and the current deployment workflow. A prior green SHA does not verify a newer head. A queued, unavailable, cancelled, superseded, or infrastructure-failed job is not a pass and is not automatically proof of an application failure.

Do not weaken tests, owner scoping, authentication, spend controls, security scans, backup/restore, exact-artifact evidence, deployment smoke checks, or production rollback gates merely to obtain green status.

## Web QA

For rendered web changes, verify representative layouts when executable browser access exists:

- Mobile: approximately 390 px.
- Tablet: approximately 768 px.
- Desktop: approximately 1440 px.

Check the affected flow plus navigation, forms/validation, upload/drag-drop states, keyboard operation, focus visibility, modals/lightbox, loading/error/empty/partial-generation states, responsive asset previews, campaign readiness states, download/export controls, authentication/ownership boundaries, console errors, accessibility basics, and obvious performance regressions.

For image-generation/delivery changes, separately verify product-image fidelity risk, crop/safe-zone behaviour, failed/partial generations, retries/idempotency, cost accounting, private output/download access, and that bundles remain `not_published` unless an explicitly approved publishing workflow exists.

Do not claim physical packaging accuracy, channel-policy compliance, legal-claim correctness, visual quality, or client approval from automated tests alone.

## Security, privacy, client isolation, and cost safety

- Every private resource must remain scoped to the authenticated/verified owner as designed.
- Fail closed when identity/ownership cannot be established.
- Use synthetic/demo fixtures for public GitHub evidence; do not place confidential client assets or prompts in issues/PRs.
- Do not expose provider/API credentials through logs, `/api/whoami`, screenshots, traces, ZIP manifests, or exception messages.
- Preserve server-side daily cost guardrails and provider/cost-ledger integrity.
- Do not silently switch a user's BYOK request to a shared/operator key or vice versa.
- Keep private generated outputs and campaign bundles behind owner-scoped access; do not make filesystem paths or object URLs public by convenience.
- Treat uploaded product imagery, brand/SKU truth, annotations, campaign briefs, generated images, comments, and exports as potentially confidential client material.
- Do not introduce autonomous external publishing, CMS writes, marketplace submissions, email sends, billing/spend, or client communications without explicit approval for the exact action.

If a suspected credential or confidential client asset is discovered in repository evidence, stop handling the value/content and report only the repository/path plus remediation need.

## Environments and production boundary

Known repository/operational environments include:

- local development;
- CI/test environments;
- an internal host-level staging check where documented in `RUNBOOK.md` (public staging DNS is not configured there); and
- production at `photogen.ashbi.ca`.

Do not invent additional staging/preview infrastructure.

Agents may inspect, implement, test, document, branch, commit, create issues, and open draft pull requests when authorised. Without Cameron explicitly approving the exact difficult-to-reverse action, agents must not:

- merge a pull request;
- deploy/promote production or replace a running container;
- publish/tag a release;
- modify Caddy/shared VPS/network/DNS/TLS configuration;
- change credentials, secrets, permissions, access, billing, provider spend limits, backup destinations/recipients, or monitoring configuration;
- run destructive database/storage migrations or restore over real data;
- send client/customer communications;
- publish generated client assets externally;
- submit to Amazon/Shopify/Meta/Pinterest or another external channel;
- make legal/compliance/client-approval claims.

A deployment script, workflow trigger, SSH access, or prepared release artifact is not itself approval to deploy.

## Deployment and rollback

Follow `RUNBOOK.md` and `docs/PRODUCTION.md`. Current production authority distinguishes source preparation/build, host/container verification, health checks, persistent data/output mounts, backup evidence, and rollback.

Do not rely on old Traefik/Coolify-managed deployment assumptions when the current runbook says Caddy is the host reverse proxy. Do not change shared fleet configuration from repository snapshots.

Production status must be verified from the running environment, including `/api/whoami` or the current documented health/identity checks, before claiming a version is live. Keep the previous known-good image/artifact and data backups available as required by the current rollback procedure.

If the actual deployed SHA, previous image, backup integrity, restore readiness, or rollback path cannot be inspected, report it as unknown/blocked rather than inventing evidence.

## Definition of done and handoff

Use Cameron's status model:

1. Completed and verified
2. Completed but awaiting verification
3. In progress
4. Blocked
5. Awaiting client or teammate
6. Next action

Never report **Completed and verified** without the required exact-head source/browser/container/production evidence for the scope.

Every substantial coding handoff should include:

- branch;
- exact commit(s);
- pull request;
- files changed;
- implementation summary;
- checks actually executed and pass/fail/queued state;
- exact-head CI/security evidence inspected;
- screenshots/URLs only when actually captured or verified;
- owner-scope/privacy/cost impact;
- generated-asset/channel-delivery impact;
- deployment/backup/rollback impact;
- remaining risks/blockers;
- production/release status;
- next action and any approval gate.
