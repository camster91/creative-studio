# Campaign exception inbox candidate

Date: 2026-09-01

Status: **stacked on PR #128; not approved for production**

## Scope

- Persist immutable, owner-scoped evidence for criterion-level QC failures and
  manual claims, channel, or human-review findings.
- Require a visible reason for approve, reject, or repair decisions and require
  an owner-scoped replacement asset for repair.
- Block bundle creation while an applicable blocking exception is open.
- Prevent rejected and repaired originals from re-entering a bundle; repaired
  replacements can proceed through their own exception gate.
- Permit advisory warnings while preserving them in the private bundle manifest.
- Include exception and resolution state in the content-addressed bundle identity,
  preventing a cached pre-review manifest from being reused after review changes.
- Expose a focused exception inbox beside the Campaign Factory work order.

## Verification contract

- Anonymous and cross-owner access fails without leaking campaign records.
- Duplicate evidence is idempotent within an owner and campaign.
- Blocking findings return an actionable HTTP 409 before bundle rendering.
- Approval without a reason and repair without an owned replacement fail closed.
- Approved evidence and its reason are present in the persisted ZIP manifest.
- Criterion-level QC failures create blocking or warning records; passing criteria
  do not create exceptions.

## Honest boundary

This candidate creates a reviewable control point; it does not prove the QC model
is calibrated. The current severity mapping is deliberately narrow and only
criterion-level failures block delivery. A representative licensed CPG corpus,
rules-first channel and claims checks, reviewer roles, and live provider evidence
remain required before describing this as autonomous compliance or approval.
