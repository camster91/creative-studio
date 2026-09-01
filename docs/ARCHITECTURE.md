# Creative Studio architecture

## Source of truth

`scripts/creative-studio-web.py` is the deployable composition and compatibility
entry point. It owns environment-derived configuration, Flask construction, and
temporary wrapper names retained for older tests and scripts. It must not own
HTTP route implementations.

Route implementations live in `creative_studio_app/*_routes.py`. Business and
persistence behavior lives in the corresponding service modules. New behavior
should be added to those modules and injected into a blueprint, not added back
to the entry script.

The CLI scripts and the web application share provider behavior through the
same subprocess-compatible service boundaries:

- `generation.py`: text generation and product compositing
- `iterations.py`: refine, variations, and variation branching
- `delivery.py`: export and QC execution
- `assets.py`: output URL/path safety and pin prompt shaping
- `costs.py`: pricing, persistence, and daily spend checks
- `version_graph.py`: owner-scoped creative lineage, selection, and branch costs
- `provider_ledger.py`: privacy-safe call accounting, correlation, and alert inputs
- `campaigns.py`: owner-scoped Brand Passport, Product Truth, Campaign Work Order,
  deterministic readiness, and provider-independent generation-plan assembly
- `campaign_delivery.py`: versioned channel recipes, deterministic exports,
  private bundle manifests, and ZIP assembly

## HTTP ownership

| Module | Routes / responsibility |
| --- | --- |
| `core_routes.py` | landing/editor, job status, key validation, immutable images |
| `generation_routes.py` | synchronous and background generation |
| `figma_oauth.py` | PKCE client and encrypted, owner-bound Figma token storage |
| `figma_oauth_routes.py` | connect, callback, refresh, disconnect, and file-context API |
| `iteration_routes.py` | refine, variations, scene sets, variation refinement |
| `delivery_routes.py` | composite, export, QC, Figma context |
| `chat_routes.py` | multi-turn chat, history, reset, approved output save |
| `state_routes.py` | pins, sessions, cost summaries |
| `version_routes.py` | version recovery, favorite, branch, undo, deletion |
| `provider_metrics_routes.py` | operator-only provider dashboard queries |
| `account_routes.py` | signup, magic-link login, account status |
| `billing_routes.py` | plans, Checkout, portal, Stripe webhook |
| `project_routes.py` | ownership-scoped project CRUD and ZIP export |
| `campaign_routes.py` | Campaign Factory, reusable truth, readiness, Go, and private bundles |
| `library_routes.py` | generated-asset listing, filtering, deletion |
| `support_routes.py` | waitlist, templates, runtime metadata, operator export |
| `seo_routes.py` | robots, sitemap, blog index/posts |
| `informational_routes.py` | status, docs, privacy, history |

## Runtime state

`CREATIVE_DATA_DIR` contains SQLite auth/project state, JSON sessions, cost
records, pins, templates, waitlist data, approved chat outputs, private campaign
bundles, and the `versions.db` lineage graph.
`provider-ledger.db` stores only owner/job identifiers, provider/model,
estimate/recorded charge, latency, stable outcome, and a random call
correlation ID. It has no prompt, image, token, key, email, or provider error
text columns.
`CREATIVE_OUTPUT_DIR` contains generated assets. Both must be persistent volumes
in production. Local generated-image URLs are resolved beneath the configured
output root and reject traversal.

Project, library, session, job, and version records are owner-scoped. Version
nodes retain parent/child lineage, per-attempt cost, partial status, and missing
asset state without copying prompts or images into operational logs.

## Verification contract

Run before merge:

```bash
python -m pytest -q
node --check static/app.js
uv lock --check
```

Architecture tests require the entry script to contain no `@app.route`
decorators. Provider-backed endpoints must use the injected authorization and
cost boundaries. Hosted CI is the release gate when GitHub Actions account
billing permits jobs to start.
