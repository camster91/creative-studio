# Campaign Factory integrated release candidate

Date: 2026-09-01

Status: **assembled for verification; not approved, merged, or deployed**

## Release intent

This candidate combines the independently reviewed production-recovery and QC
calibration work with the stacked Campaign Factory product expansion. It is the
single candidate that should be evaluated for a later merge to `main`; the
component pull requests remain evidence sources and must not be merged
separately after this integration.

Component heads:

- verified encrypted backups: `0e5fd924e3df7f3f3b76f15e346cdbb940e6638a`
- privacy-safe QC calibration: `8bd49902843fc55137a017563ad3602769b6a91c`
- Campaign Factory stack through governed claims: `cfca274337b7e84b0ca8a73365a5fa42d4d46883`

Production baseline before this candidate:
`0aa3bcaac3c469347f8c16068245a54c156aaebb`.

## Customer-visible scope

- Reuse owner-scoped Brand Passports and Product Truth records.
- Require an exact pack asset or an explicit concept-only waiver.
- Produce deterministic channel assets and a private manifest-backed bundle.
- Block channel-rule and crop failures before delivery.
- Route criterion-level findings through a reason-required exception inbox.
- Register market- and channel-scoped claim evidence, fail readiness for
  missing or expired evidence, and preserve applicable claim provenance in the
  generation plan and bundle manifest.

No feature in this release publishes to an ad or commerce platform. No feature
certifies legal or regulatory compliance, and no customer-visible pricing or
offer changes are included.

## Operational scope

- Add SQLite-consistent snapshots, manifest integrity checks, `age` encryption,
  off-host artifact storage, and isolated restore verification.
- Add a privacy-safe QC calibration harness that records aggregate reviewer and
  rubric outcomes without creative payloads.
- Keep the application deployment model unchanged: an immutable SHA-tagged
  image is smoke-tested on staging and the same SHA is promoted to production.

## Data and compatibility

- Campaign records gain a market, defaulting legacy records to `US`.
- Approved claims are additive owner-scoped records. Legacy claim strings remain
  readable but cannot satisfy Campaign Factory readiness until matching active,
  applicable evidence is registered.
- Claim retirement is append-preserving and reason-required; it does not delete
  the original approval evidence.
- Existing campaign, project, generation, and version data remain in their
  current stores. There is no destructive migration in this release.

Rolling application binaries back does not delete additive records. The prior
binary may ignore new Campaign Factory fields, so operators must not alter or
delete persistent data during rollback. Restore from the verified encrypted
backup only for confirmed data corruption, not for an application regression.

## Verification gate

The exact final pull-request head must pass:

- Python compilation and full Python tests;
- JavaScript syntax and Node tests;
- desktop and mobile browser release journeys;
- diff hygiene and ancestry checks for every component head;
- all default-branch required checks, including Ashbi Local CI, Node build,
  Python tests, Browser release gate, and GitGuardian;
- staged immutable-image smoke and health checks after an authorized merge;
- production identity, image, public-page, authenticated-read, bundle, backup,
  and rollback-signal verification after deployment.

## Rollback

Record the currently running production image digest immediately before merge.
If staging fails, promotion stops. If production verification fails, run the
documented rollback workflow to the recorded previous immutable image while
preserving the data and output mounts. Reverify `/api/whoami`, landing and
Campaign Factory pages, login delivery, and a non-billable authenticated read.

## Known boundaries after release

- The product owner remains the only approval actor; workspace roles and
  independent legal/brand approval are pending.
- Licensed representative creative evidence, pinned-provider comparison, and
  calibrated quality thresholds are pending.
- Semantic subject detection, rendered-copy OCR, platform-specific safe zones,
  and autonomous external publishing are not claimed.
- Real CPG customer interviews, time-to-value measurement, acceptance and
  correction rates, willingness-to-pay evidence, and customer-validated market
  leadership remain pending.
- The recovery identity is still shared with deployment; a separate offline
  recovery identity and drill remain required.

## Authorization boundary

Opening and validating the integration pull request does not authorize
production. Because a merge to `main` triggers the staged production workflow,
the accountable owner must explicitly approve the exact final pull-request SHA
after all required checks are green. Earlier general approval does not authorize
that future SHA.
