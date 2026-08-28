# Creative Studio product roadmap

Last reconciled: 2026-08-28

Repository version: 4.6.0

Production status: operator-managed and not inferred from repository state; verify
`/api/whoami` and the production runbook before making a release claim.

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
- The first Campaign Factory slice is implemented: Brand Passport, Product
  Truth, Campaign Work Order, deterministic readiness, and a Go action that
  compiles approved inputs into the existing generation request.
- Gemini remains the only production generation provider. OpenAI support is
  researched but not implemented or evaluated.
- Deployment is operator-managed. Repository checks do not prove that the
  current commit is released.
- No target-customer interviews, representative AI evaluation corpus, or
  customer-validated outcome metrics are yet recorded.

## Primary objective — Campaign Factory foundation

Status: **in progress**

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
- [ ] Reuse existing Brand Passports and Product Truth records across campaigns.
- [ ] Attach an exact product pack asset and default CPG campaigns to the
  deterministic composite path rather than text-only packaging generation.
- [ ] Create channel recipes that fan one work order into exact format variants.
- [ ] Route QC or claims failures into an exception inbox with repair/reject actions.
- [ ] Save successful outputs back to the campaign and export a campaign bundle.
- [ ] Verify the end-to-end journey in desktop and mobile browser tests.

Measurement hypothesis: a representative user can reach a generation-ready
work order in under five minutes with zero invented product claims. This is
proposed until measured with target customers.

## Next priorities

### P0 — Product truth and pack fidelity

- Make Brand Passport and SKU records independently editable and reusable.
- Require or explicitly waive a product pack asset before Go.
- Preserve pack pixels through background generation and deterministic
  compositing; never present text-only packaging generation as exact fidelity.
- Add audit history for claim/rule changes that affect a campaign.

### P0 — Evaluation and exception workflow

- Build a consent-safe corpus of 30–50 representative CPG work orders.
- Define rubrics for pack fidelity, claim correctness, brand adherence,
  composition, channel compliance, latency, and cost.
- Run Gemini as the baseline; add OpenAI Responses API and GPT Image behind a
  provider interface only after the evaluation contract exists.
- Convert advisory QC into rules-first pass/fail checks plus an exception inbox.

### P1 — Channel production and delivery

- Add versioned Amazon, Shopify, Meta, email, and web channel recipes.
- Generate/crop/export one approved concept into exact channel deliverables.
- Add campaign bundle manifests with inputs, provider/model, costs, checks,
  approvals, and output lineage.

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
