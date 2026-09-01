# Campaign Factory production release record

Date: 2026-08-28

Status: **released and independently verified 2026-09-01**

## Scope

- Campaign Factory foundation: Brand Passport, Product Truth, Campaign Work
  Order, readiness gate, and generation-plan handoff.
- Public positioning and primary conversion updated from prompt-first image
  generation to the guided CPG work-order workflow.
- Password-free email sign-in link now opens and consumes the one-time token
  without requiring manual copy/paste.
- Deployment workflow reconciled with the documented Caddy route on host port
  `32778`; manual deploy and rollback can no longer run concurrently.
- Production verification now checks `/api/whoami`, `/campaigns`, and the
  landing-page Campaign Factory CTA.

## Approval

The accountable owner requested “deploy” on 2026-08-28. This authorizes the
production release after the documented technical gates pass. It does not
approve new legal representations, pricing changes, purchases, or acceptance
of a critical/high release risk.

## Verification evidence

- Python: 409 passed, 1 intentionally skipped.
- Browser: 10 Playwright checks passed across phone, tablet, and desktop,
  including automated WCAG checks and horizontal overflow.
- Node dependency audit: 0 known vulnerabilities in the locked browser-test
  dependency set.
- JavaScript syntax and repository diff checks passed.
- Live pre-release baseline: `/api/whoami` returned HTTP 200 and version 4.6.0;
  `/campaigns` returned 404 as expected before release.
- Final image build and isolated container smoke checks passed for immutable SHA
  `0aa3bcaac3c469347f8c16068245a54c156aaebb`.
- Staging and production ran that exact image as non-root `appuser`; both
  containers reported healthy.
- Independent public verification returned HTTP 200 for `/`, `/campaigns`,
  `/app`, `/privacy`, and `/api/whoami`; anonymous `/api/costs` returned 401.
- Production deployment run:
  <https://github.com/camster91/creative-studio/actions/runs/33461507115>

## Resolved deployment blockers

1. Hosted Actions execution was restored and the required Node, Python,
   browser, secret-scan, image, staging, and production gates executed.
2. The stale deployment SSH secret was replaced with the already-authorized
   Ashbi recovery key; no server key rotation or service interruption occurred.
3. The image packaging omission, obsolete staging network, stale smoke markers,
   and legacy bind-mount ownership were corrected before promotion.
4. Encrypted off-host backup and isolated restore evidence was subsequently
   established in run `33462661827`; see the 2026-09-01 recovery evidence.

## Release and rollback

Released image: `ghcr.io/camster91/creative-studio:0aa3bcaac3c469347f8c16068245a54c156aaebb`.
The workflow built one immutable SHA image, smoke-tested it, deployed staging,
then deployed the identical image to production on `32778`.

For later releases, record the current immutable image ID and confirm a recent
backup/restore test. Roll back by running that exact prior image with the same
persistent mounts and environment, then verify `/api/whoami`, `/`, `/campaigns`,
sign-in delivery, and one non-billable authenticated read.

## Post-release checks

- Confirm the public landing page routes its primary CTA to `/campaigns`.
- Complete a controlled email sign-in and verify the fragment token is removed
  from browser history before API redemption.
- Create a ready work order, verify cross-owner isolation, and run one bounded
  low-cost generation using an approved test SKU and provider key.
- Inspect HTTP 5xx, queue depth, provider cost, and email delivery alerts.
- Record the deployed image SHA and production verification time here.
