# Campaign pack fidelity candidate

Date: 2026-09-01

Status: **local candidate; not approved for production**

## Scope

- Require an owner-scoped exact pack image or an explicit, reasoned
  concept-only waiver before Campaign Factory Go.
- Validate and canonicalize the uploaded image, retain it as Product Truth,
  and record the canonical source SHA-256.
- Resolve campaign pack files server-side with campaign ownership, upload
  ownership, purpose, filename, path-containment, and sidecar checks.
  The source is re-hashed before provider execution to reject drift.
- Route pack-backed campaigns through bounded deterministic composite calls;
  route waived campaigns through direct generation with a visible concept-only
  fidelity notice.
- Disclose that background cleanup and scaling may alter edge pixels. This
  candidate does not close the subjective and pinned-provider fixture work in
  issue #102.

## Local verification

- Python compile succeeded.
- Python suite: 412 passed, 1 intentionally skipped.
- Campaign contract: 9 passed, including cross-owner rejection, canonical pack
  provenance, readiness gating, server-side pack resolution, and three bounded
  composite variations.
- Browser release gate: 10 passed across phone, tablet, and desktop, including
  WCAG checks and horizontal-overflow budgets.
- JavaScript syntax and `git diff --check` passed.

## Release gates

1. Commit and push the reviewed candidate SHA.
2. Obtain green hosted CI and security checks for that exact SHA.
3. Review interaction screenshots and run at least one controlled provider
   composite with a licensed synthetic pack fixture.
4. Obtain fresh accountable-owner approval for the exact merge. A merge to
   `main` deploys automatically and is not authorized by earlier approvals.
5. Verify production identity, public routes, authenticated isolation, and a
   bounded non-customer test after deployment.

## Known residual risk

The current PIL cleanup uses a white-background heuristic, fixed placement,
fixed scale, and a synthetic contact shadow. It does not yet prove transparent,
opaque, tiny, huge, rotated, low-quality, occluded, or color/light-matched cases.
Issue #102 remains the acceptance record for those fixture and live-provider
observations.
