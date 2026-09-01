# QC calibration harness candidate

Date: 2026-09-01

Status: **local candidate; no reviewed provider corpus yet**

## Scope

- Aggregate human consensus and provider results separately for every exact
  rubric/model pair.
- Measure independent-review coverage, reviewer disagreement, provider
  coverage, known accuracy, unknown rate, false-pass rate, and false-fail rate
  per criterion.
- Reject duplicate fixtures, missing license/origin evidence, anonymous or
  duplicate reviewers, missing criterion labels, and unversioned assessments.
- Emit aggregate-only reports with explicit limitations; retain no prompt,
  image, customer, token, key, or email fields.
- Document the protocol for a licensed, independently reviewed calibration run.

## Verification

- Focused calibration contract: 7 passed.
- Full Python suite: 416 passed, 1 intentionally skipped.
- Python compileall and repository diff checks passed.

## Honest release boundary

The repository proves aggregation and validation behavior only. Issue #104
cannot close until an approved fixture owner supplies a licensed representative
corpus, two independent reviewers record original labels, an exact pinned
provider model is run within an approved cost boundary, thresholds are
predeclared, and the resulting false-pass/false-fail/unknown/disagreement rates
are reviewed. No calibration accuracy claim is made by this candidate.
