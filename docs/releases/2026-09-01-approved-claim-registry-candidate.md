# Approved claim registry candidate

Date: 2026-09-01

Status: **stacked on PR #130; not approved for production**

## Scope

- Replace claim-only strings with owner-scoped evidence records containing exact
  text, claim class, US/Canada market scope, channel scope, HTTPS substantiation,
  required disclosure, accountable approval reason, and optional expiry.
- Preserve backward compatibility for legacy strings while failing Campaign
  Factory readiness until matching applicable evidence is registered.
- Compile only active, unexpired claims that cover the selected campaign market
  and every selected channel.
- Add reason-required retirement without rewriting the original approval evidence.
- Preserve applicable claim IDs and evidence in the deterministic private bundle
  fingerprint and manifest.

## Verification contract

- Missing evidence, market mismatch, channel mismatch, and expiry fail readiness.
- Cross-owner claim listing and retirement do not disclose product existence.
- Duplicate evidence is idempotent within an owner and product.
- Retiring a claim requires a reason and does not delete its approval record.
- Generation plans and bundle manifests contain only applicable claim records and
  their required disclosures.

## Honest boundary

The product owner is the only accountable actor in this candidate. Workspace roles,
independent legal/brand approvers, substantiation-file custody, jurisdiction-specific
rule engines, OCR of rendered copy, and qualified legal review remain pending.
