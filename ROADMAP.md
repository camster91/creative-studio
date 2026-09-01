# Creative Studio product roadmap

Last reconciled: 2026-09-01

Repository version: 4.6.0

Production status: Campaign Factory foundation released from immutable image
`0aa3bcaac3c469347f8c16068245a54c156aaebb` and independently verified on
2026-09-01. Reverify `/api/whoami`, the running image, and the production
runbook before each later release claim.

## Product charter

Creative Studio is becoming a CPG Creative Operating System: a guided system
that turns verified brand and SKU inputs into channel-ready campaign creative.
The initial wedge is lean CPG marketing teams that need more usable variants
without repeatedly briefing agencies or steering an open-ended chatbot.

Promise: define product truth once, submit a bounded Campaign Work Order, pass a
visible readiness gate, press Go, and review a traceable set of outputs.

The dated market and technical evidence behind this direction lives in
[`docs/CPG-CREATIVE-OPERATING-SYSTEM-RESEARCH.md`](docs/CPG-CREATIVE-OPERATING-SYSTEM-RESEARCH.md).

## Verified current state

- The Flask web app supports prompt-based Gemini generation, product
  compositing, variations, refinement, version history, projects, library,
  export, advisory QC, billing, and owner-scoped persistence.
- The first Campaign Factory slice is released: Brand Passport, Product
  Truth, Campaign Work Order, deterministic readiness, and a Go action that
  compiles approved inputs into the existing generation request.
- The current candidate adds owner-scoped canonical pack assets, SHA-256
  provenance, an explicit concept-only waiver, and bounded deterministic
  composite execution. It is not production until its exact SHA is approved,
  merged, deployed, and verified.
- A stacked candidate adds owner-scoped Brand Passport and Product Truth
  libraries so a new work order can reuse approved inputs without duplicating
  or silently rewriting them. It is not merged or deployed.
- A further stacked candidate turns selected channels into exact deterministic
  assets and a private manifest-backed ZIP after generation. It performs no
  external publishing and is not merged or deployed.
- Gemini remains the only production generation provider. OpenAI support is
  researched but not implemented or evaluated.
- The staged deployment path builds and smoke-tests an immutable GHCR image,
  runs it as a non-root user on staging, then promotes the same SHA to
  production with health checks and rollback.
- A consistent encrypted backup and isolated restore drill passed from branch
  run `33462661827`; merging its reviewed workflow is still required before the
  daily schedule becomes authoritative.
- No target-customer interviews, representative AI evaluation corpus, or
  customer-validated outcome metrics are yet recorded.
- A QC calibration harness is implemented as an unmerged candidate. It
  separates exact rubric/model groups and measures independent-review coverage,
  disagreement, provider coverage, known accuracy, unknowns, false passes, and
  false fails without retaining creative payloads. It is infrastructure for a
  reviewed corpus, not evidence that such a corpus already exists.

## Primary objective — Campaign Factory foundation

Status: **foundation released; product expansion in progress**

Problem: the mature generator is still exposed primarily as a blank prompt and
manual tool collection. CPG teams need reusable truth, bounded work orders,
visible gates, and exception handling.

Outcome: a signed-in CPG marketer can move from reusable brand/SKU truth to a
reviewable multi-channel campaign without constructing prompts or using chat.

Acceptance criteria:

- [x] Persist owner-scoped Brand Passport, Product Truth, and Campaign Work Order.
- [x] Reject cross-owner reads and Go actions without revealing record existence.
- [x] Show deterministic missing-input checks before provider usage.
- [x] Compile only verified facts, approved claims, disclosures, and brand rules
  into a provider-independent generation request.
- [x] Run the approved request through the existing durable generation path.
- [x] Reuse existing Brand Passports and Product Truth records across campaigns.
- [x] Store approved claims as market/channel-scoped evidence records with exact
  text, class, HTTPS substantiation, approval reason, disclosure, and expiry.
- [x] Attach an exact product pack asset and default CPG campaigns to the
  deterministic composite path rather than text-only packaging generation.
- [x] Create channel recipes that fan one work order into exact format variants.
- [x] Route criterion-level QC, claims, channel, or human findings into an
  owner-scoped exception inbox with reason-required approve/reject/repair actions.
- [x] Save successful outputs back to the campaign and export a campaign bundle.
- [x] Verify the released foundation journey in phone, tablet, and desktop
  browser tests.
- [ ] Verify the complete stacked end-to-end campaign journey in desktop and
  mobile browser tests on the exact integrated release candidate.

Measurement hypothesis: a representative user can reach a generation-ready
work order in under five minutes with zero invented product claims. This is
proposed until measured with target customers.

## Next priorities

### P0 — Production recovery and lifecycle

- [x] Validate upload type, dimensions, decoded pixels, metadata stripping, and
  owner/expiry sidecars.
- [x] Create SQLite-consistent snapshots with manifest hashes and path safety.
- [x] Encrypt backups before off-host artifact storage and verify an isolated
  restore including all copied SQLite databases.
- [ ] Merge the verified backup workflow so its daily schedule is authoritative.
- [ ] Schedule production upload-retention cleanup and record its first dry-run
  and execute evidence.
- [ ] Separate the backup recovery identity from the deploy identity and record
  an annual recovery-key drill.

### P0 — Product truth and pack fidelity

- Reuse Brand Passport and SKU records without silent rewrites. Candidate
  implemented; independent editing and audited change history remain pending.
- Require or explicitly waive a product pack asset before Go. Candidate
  implemented; production release remains gated.
- Measure and improve alpha edges, background cleanup, scale, contact shadow,
  perspective, occlusion, and light/color match. The current composite uses the
  validated pack source but can alter edge pixels during cleanup and scaling.
- Synthetic deterministic criteria now cover supplied alpha preservation,
  edge-connected white removal, enclosed white label survival, tiny/empty input
  rejection, aspect preservation, bounded placement, no clipping, and
  content-addressed source/foreground/environment/output lineage. Licensed
  visual review and live pinned-provider evidence remain outstanding.
- Never present concept-only packaging generation as exact fidelity.
- Add audit history for claim/rule changes that affect a campaign.
- Approved claims now fail readiness when evidence is missing, expired, or not
  scoped to every selected channel and market; multi-role approval remains pending.

### P0 — Evaluation and exception workflow

- Build a consent-safe corpus of 30–50 representative CPG work orders.
- Define rubrics for pack fidelity, claim correctness, brand adherence,
  composition, channel compliance, latency, and cost.
- Use the documented QC calibration protocol to require license/origin evidence,
  independent reviewer identities, pinned provider versions, predeclared sample
  sizes, and per-criterion false-result/unknown/disagreement thresholds.
- Run Gemini as the baseline; add OpenAI Responses API and GPT Image behind a
  provider interface only after the evaluation contract exists.
- Criterion-level QC failures now enter an immutable-evidence exception inbox;
  deterministic rules, corpus calibration, and threshold validation remain pending.

### P1 — Channel production and delivery

- Versioned Amazon, Shopify, Meta, Pinterest, email, and web recipes plus private
  bundle export are implemented in a stacked candidate.
- Alpha-bounded source and center-crop checks now block visible subject loss and
  warn at a 5% crop margin. Fully opaque sources remain explicitly unverified;
  semantic subject detection and platform-specific safe zones remain pending.
- Bundle manifests now preserve applicable exception, approval, QC model, and
  rubric evidence. Provider generation lineage and cost evidence remain pending.

### P1 — Teams and governance

- Add workspace roles, approval states, comments, and spend attribution.
- Define data retention/deletion, provider-data disclosures, and enterprise
  security requirements before selling governance claims.

### P2 — Commercial validation

- Interview 8–12 CPG marketers and creative operators using the research guide.
- Test the focused promise, switching barriers, willingness to pay, and desired
  approval workflow before changing customer-visible pricing.
- Instrument activation, time to first ready work order, Go success, output
  acceptance/correction, cost per accepted asset, and repeat campaigns.

## Deliberately deferred

- General-purpose chatbot positioning.
- Autonomous publishing to ad or commerce platforms without explicit approval.
- Broad video production before the still-image campaign loop is validated.
- Provider switching based on marketing claims rather than corpus evidence.
- Production deployment, pricing changes, and external announcements without
  accountable owner approval.

## Release gate

Before any production release, run the verification contract in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), browser release gates, a
proportional security/privacy review, container smoke tests, backup/rollback
checks, and obtain explicit owner approval as defined in
[`docs/PRODUCTION.md`](docs/PRODUCTION.md).

Current release evidence and operational follow-up are recorded in
[`docs/releases/2026-08-28-campaign-factory-release.md`](docs/releases/2026-08-28-campaign-factory-release.md).
