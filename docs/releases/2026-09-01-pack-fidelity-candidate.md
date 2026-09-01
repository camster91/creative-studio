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
- Replace global white-pixel deletion and fixed placement with edge-connected
  background removal and aspect-preserving, centered, bounded placement.
- Reject empty and sub-32px detected products before provider credentials,
  credits, or calls are used.
- Persist a composite manifest containing SHA-256 lineage for the canonical
  source, prepared foreground, generated environment, and final output plus
  exact placement geometry; propagate it into session and version metadata.

## Local verification

- Python compile succeeded.
- Python suite: 425 passed, 1 intentionally skipped.
- Campaign and synthetic composite contracts cover cross-owner rejection, canonical pack
  provenance, readiness gating, server-side pack resolution, and three bounded
  composite variations, supplied transparency, enclosed white labels, tiny and
  empty failures, portrait/landscape geometry, non-clipping, and manifest lineage.
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

The current PIL cleanup still uses a near-white threshold and a synthetic
contact shadow. Deterministic tests cover transparency, opaque white-edge
backgrounds, tiny/empty rejection, rotated aspect ratios, and bounded geometry;
they do not prove subjective realism, perspective, occlusion, low-quality source
recovery, or color/light match. Issue #102 remains the acceptance record for
licensed visual review and live-provider observations.
