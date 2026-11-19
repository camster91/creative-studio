# Launch log

Running record of the rename, review and launch. Newest first.

## 2026-10-14

**Done**
- Phase 0 review: 579 tests pass, 1 skipped (needs `pip install -e '.[test]'`
  first). "Photogen" appears in 45 files and "Creative Studio" in 31; about 14
  of those are customer-facing (templates, static JS/CSS, SEO and support
  routes, blog). Internal `PHOTOGEN_*` settings can keep their names.
- Phase 1 naming: checked 10 names. Cameron picked **Packstage**.
  Dropped: Packscene (live Shopify AI photo app), Shelfmade (CPG founder
  community), Plinth, Brightpack, Packlight, Packframe, Packpose.
- Phase 2 started: brand brief, design tokens and three wordmark concepts.

**Found**
- Self-serve pricing is thin: Starter $19/100 credits, Pro $49/500,
  Studio $99/1,500. A credit is about $0.05 of AI cost, so Studio can cost up
  to ~$75 per month to serve. Raise prices or cut credits before launch.
- Launch blockers still open: #97 (CI evidence), #98 (magic-link email),
  #100 (data lifecycle), #101 (cost tracking), #108 (outage drill). Terms and
  privacy are drafts. Live Stripe is off by design.

**Waiting on Cameron or Bianca**
- Bianca: pick and refine a wordmark concept.
- Cameron: check packstage domains at a registrar; run a trademark search.
- Cameron: decide on self-serve pricing.
