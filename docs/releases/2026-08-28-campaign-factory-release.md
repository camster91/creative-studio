# Campaign Factory production release record

Date: 2026-08-28

Status: **approved; publication pending external deployment access**

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

## External blockers

1. GitHub rejects CI, CodeQL, image build, and deployment jobs before execution:
   “recent account payments have failed or your spending limit needs to be
   increased.” The owner must resolve GitHub Billing & plans or provide a
   separate authorized VPS SSH path.
2. Current production backup/restore evidence and SMTP delivery cannot be read
   from this machine because the VPS key is unavailable. Those gates must run
   through the restored deployment job or an authorized operator session.

## Release and rollback

Recommended release: merge the reviewed SHA to `main` after GitHub Actions is
restored. The workflow builds one immutable SHA image, smoke-tests it, deploys
staging, then deploys the identical image to production on `32778`.

Before the container swap, record the current immutable image ID and confirm a
recent backup/restore test. Roll back by running that exact prior image with the
same persistent mounts and environment, then verify `/api/whoami`, `/`,
`/campaigns`, sign-in delivery, and one non-billable authenticated read.

## Post-release checks

- Confirm the public landing page routes its primary CTA to `/campaigns`.
- Complete a controlled email sign-in and verify the fragment token is removed
  from browser history before API redemption.
- Create a ready work order, verify cross-owner isolation, and run one bounded
  low-cost generation using an approved test SKU and provider key.
- Inspect HTTP 5xx, queue depth, provider cost, and email delivery alerts.
- Record the deployed image SHA and production verification time here.
