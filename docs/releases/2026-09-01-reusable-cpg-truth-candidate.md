# Reusable CPG truth candidate

Date: 2026-09-01

Status: **stacked on PR #125; not approved for production**

## Scope

- Expose owner-scoped Brand Passport and Product Truth libraries.
- Create a new Campaign Work Order by referencing an existing SKU, reusing its
  Brand Passport, approved facts/claims/disclosures, waiver, and pack
  provenance without duplicating the records.
- Reuse a Brand Passport while creating a separate new SKU.
- Ignore nested brand/product edits when an existing record is referenced, so a
  new work order cannot silently rewrite approved truth.
- Reject missing, cross-owner, and brand/product-mismatched references without
  revealing another owner’s records.
- Populate Campaign Factory from saved records while making the no-silent-edit
  behavior visible to the user.

## Verification contract

- Anonymous library access is rejected.
- A second work order reuses the exact original brand/product IDs and inputs.
- Reuse creates no duplicate Brand Passport or Product Truth record.
- A Brand Passport can support a distinct new SKU.
- Cross-owner creation and filtered listing do not reveal saved truth.
- Existing pack-fidelity, campaign, composite, and browser gates remain
  required because this candidate is stacked on PR #125.

## Honest boundary

This candidate supports selection and reuse. It does not yet add independent
record editing, approval states, revision history, or audited propagation of a
truth change into existing campaigns. Those remain P0 governance work and must
not be implied by the reuse UI.
