# Campaign channel bundle candidate

Date: 2026-09-01

Status: **stacked on reusable truth and PR #125; not approved for production**

## Scope

- Version `cpg-channel-recipes-v1` with exact deterministic outputs: Amazon
  2000×2000, Shopify 2048×2048, Meta feed 1080×1350, Meta story 1080×1920,
  Pinterest 1000×1500, email 1200×628, and web 1920×1080.
- Fan every returned campaign concept through every selected recipe after the
  generation job completes.
- Persist the ZIP and manifest under private application data, never the public
  image route; require owner authentication for listing and download.
- Verify source ownership against owner-scoped session lineage before reading
  an image.
- Content-address source and deliverable files; record exact dimensions,
  background, DPI, recipe version, pack provenance, and explicitly mark the
  bundle `not_published`.
- Deduplicate identical requests deterministically, limit inputs to 1–8 unique
  images, cap stored rendered bytes, and remove partial files on failure.
- Mark the campaign completed only after the bundle record is committed.

## Verification contract

- Every source × channel combination appears once in the ZIP.
- Every image dimension equals its declared recipe.
- The ZIP contains the exact persisted manifest and SHA-256 lineage.
- Sequential replay returns the same bundle and does not duplicate records.
- Empty, over-eight, duplicate, unowned, cross-owner, missing, and oversized
  inputs fail closed.
- Campaign status and source session are saved only for an owner-scoped bundle.

## Honest boundary

Exact dimensions do not prove marketplace policy compliance or good
composition. The current deterministic center crop can cut a subject positioned
near an edge. Subject-aware safe zones, channel-policy version review, QC and
human approval evidence remain required. This feature creates downloadable
assets only; it performs no autonomous publishing.
