## Purpose

What bounded user/product/operations outcome does this PR deliver?

## Scope

- In scope:
- Out of scope:
- Related issue/roadmap item:
- CLI / web / Campaign Factory / production area affected:

## Exact-head verification

Record only checks that actually ran on this head/artifact.

- [ ] `make test`
- [ ] `make test-js`
- [ ] `make lint`
- [ ] Relevant focused Python/JavaScript tests
- [ ] Relevant Playwright/browser tests
- [ ] Container/build checks when runtime packaging is affected
- [ ] Exact-head CI / Ashbi Local CI status inspected
- [ ] Security/secret scans inspected when applicable

Evidence/results:

## Web / accessibility QA

When UI is affected:

- [ ] Mobile (~390 px)
- [ ] Tablet (~768 px)
- [ ] Desktop (~1440 px)
- [ ] Keyboard/focus/accessibility basics
- [ ] Upload/generation/loading/error/partial states
- [ ] Campaign/readiness/export/download states as applicable
- [ ] Console/regression check

Screenshots/URLs actually verified:

## Client isolation, privacy, cost, and delivery safety

- [ ] Owner scoping remains fail-closed for private resources.
- [ ] No real client assets/prompts, provider keys, auth secrets, production env values, or private outputs were added to GitHub evidence.
- [ ] BYOK/shared-key and spend-guardrail behaviour remains explicit and correct where affected.
- [ ] Generated assets/bundles remain private and `not_published` unless an explicitly approved publishing workflow exists.
- [ ] Crop/preflight/QC results are not represented as marketplace, legal, factual-product, or client approval.
- [ ] Required tests/security/deployment/backup/rollback gates were not weakened merely to obtain green status.

## Deployment / rollback boundary

Production changed: **No unless separately and explicitly approved.**

- Deployment impact:
- Data/storage migration impact:
- Backup/restore impact:
- Previous known-good image/artifact:
- Rollback evidence:
- External publishing/client communication/billing/spend impact:

## Handoff

Use the repository status model:

1. Completed and verified
2. Completed but awaiting verification
3. In progress
4. Blocked
5. Awaiting client or teammate
6. Next action

Remaining risks/blockers:

Next action / approval gate:
