# Campaign rules preflight candidate

Date: 2026-09-01

Status: **stacked on PR #129; not approved for production**

## Scope

- Inspect transparent-source alpha bounds against every selected channel's exact
  deterministic center-crop box before rendering a private bundle.
- Create blocking channel exceptions when visible alpha pixels would be cropped.
- Create review warnings when the subject is within a 5% crop margin.
- Mark fully opaque sources as unverifiable instead of claiming subject safety.
- Mark rendered claims and disclosures as requiring human copy review because this
  deterministic pass does not perform OCR or legal validation.
- Reuse the owner-scoped exception inbox and reasoned approval/repair workflow.

## Verification contract

- Empty transparent inputs and alpha bounds outside a selected crop fail closed.
- Alpha bounds near a crop edge create a warning but do not silently disappear.
- Fully opaque sources create an explicit review warning.
- A route-level crop failure returns HTTP 409 and is visible in the campaign inbox.
- Repeated preflight findings are idempotent within a campaign and asset.

## Honest boundary

Alpha bounds are not semantic subject detection. Opaque photographs cannot be
verified by this rule, and a bounding box inside the crop does not prove strong
composition or marketplace policy compliance. OCR, legal-claims rules, channel
policy versioning, and a licensed CPG evaluation corpus remain outstanding.
