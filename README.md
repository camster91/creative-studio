# CPG/DTC AI Photography Studio

> AI-powered product photography for Consumer Packaged Goods (CPG) and Direct-to-Consumer (DTC) brands.
> Your exact prompts go straight to the model. No creative director rewriting.

## Two Products in One Repo

| Product | Description | Audience |
|---------|-------------|----------|
| **CLI Skill** | Command-line tool for power users | Developers, AI researchers |
| **Web App** | Visual platform for marketing teams to generate product photography at scale | CPG brands, DTC marketers |

**Web app:** production deployment is operator-managed; verify `/api/whoami`
before relying on a previously published version.

---

## CLI Skill (v4.5)

```bash
cd cli/
env GEMINI_API_KEY="..." FIGMA_ACCESS_TOKEN="..." bash launch.sh variations \
  --prompt "G FUEL shelf display" \
  --input-image product.png \
  --tier quality --smart \
  --aspect-ratio 16:10 \
  -v 4
```

**Features:**
- ✅ `--smart` prompt enhancement with reasoning model
- ✅ `--tier` quality presets (fast → ultra)
- ✅ `variations` → `refine` pick-and-refine workflow
- ✅ Figma-aware design context extraction
- ✅ Vision pre-analysis of reference images
- ✅ Cost tracking + config persistence
- ✅ Aspect ratio control (1:1, 16:9, 16:10, 4:3, 3:2, 9:16)

**Files:**
- `cli/scripts/creative_studio.py` — Main CLI
- `cli/scripts/figma_utils.py` — Figma API integration
- `cli/scripts/analyze.py` — Vision analysis helpers
- `cli/scripts/plan.py` — Prompt planning
- `cli/launch.sh` — Entry point
- `cli/recipes/*.json` — Prompt templates

---

## Web App (Deployed at https://photogen.ashbi.ca)

### Current (v4.6.0)

- Direct generation (text-to-image, BYOK via Gemini API)
- Product compositing (upload your packaging, AI builds the scene around it)
- 4 quality tiers (Fast $0.02 / Balanced $0.05 / Quality $0.09 / Ultra $0.24)
- 6 aspect ratios (1:1, 4:3, 16:9, 9:16, 2:3, 4:5)
- 4 platform presets (Amazon / Instagram / Email / Pinterest) — auto-set prompt + aspect
- Batch 4-up (parallel generation with streaming partial results)
- Server-side daily cost guardrail (`CREATIVE_DAILY_LIMIT`, default $5/day)
- Live session gallery with multi-select + ZIP export
- Durable version history with branching, favorite, back/undo, and cost recovery
- Pin annotations, refine, variations, chat mode, and an advisory QC API
- Cost tracking (per-image, per-day, per-session)
- Privacy-safe provider ledger with cost variance, latency, outcomes, and alerts
- Lightbox, skeleton loaders, prompt history, copy-prompt, Ctrl+Enter
- `/api/whoami` endpoint to surface BYOK vs shared-key status

### Architecture

The Flask entry script composes independently tested service modules and route
blueprints from `creative_studio_app/`; it contains no route implementations.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the authoritative module,
state, CLI/web, and verification boundaries.

### UX Flow

1. **Login** → See projects: "G FUEL Summer 2026", "Prymal Rebrand"
2. **New Project** → Select brand profile → Pick scene template
3. **Upload** → Drag product PNGs (transparent background)
4. **Brief** → Type description or pick recipe
5. **Generate** → Background task spins 4 variations per SKU
6. **Compare** → v1-v4 grid, click favorite, type refinement
7. **Refine** → Iterate until satisfied
8. **Export** → Select format preset → ZIP download

### Scene Templates (Built-in)

| Template | Description |
|----------|-------------|
| Retail Shelf | Clean wooden shelf, warm lighting, brand products only |
| Studio White | Pure white background, centered product |
| Lifestyle Kitchen | Products on a marble counter, morning light |
| Lifestyle Gym | Shaker bottle being held, gym background |
| Social Media Hero | 16:9 landscape with copy space for text |
| Amazon A+ | 2000×2000 with mandatory white space |

### Format Presets

| Platform | Size | Background | Notes |
|----------|------|-----------|-------|
| Amazon PDP | 2000×2000 | White | Min 500px, max 10000px |
| Shopify | 2048×2048 | White | Square for grid, landscape for hero |
| Meta Feed | 1080×1080 | Any | 4:5 for feed, 9:16 for stories |
| Pinterest | 1000×1500 | Any | 2:3 vertical |
| Print Catalog | 300 DPI | Any | CMYK color space |

### Tech Stack

| Layer | Choice |
|-------|--------|
| Backend | Flask + gunicorn |
| Database | SQLite |
| Queue | Durable SQLite lifecycle with bounded in-process workers |
| Frontend | Vanilla JavaScript |
| Storage | Owner-scoped metadata plus mounted local volumes |
| AI | Google Gemini (same as CLI) |
| Deploy | Docker + Coolify VPS |

---

## Development Roadmap

### Phase 1: CLI Skill (v4.x) — Shipped
- [x] Prompt enhancement engine
- [x] Quality tiers (fast / balanced / quality / ultra)
- [x] Pick-and-refine workflow
- [x] Figma integration
- [x] Vision pre-analysis
- [x] Aspect ratio control
- [x] Image-to-image and text-to-image modes

### Phase 2: Web App MVP — Shipped (v4.5.1)
- [x] Flask backend with all CLI features surfaced in the UI
- [x] Upload → generate → compare flow
- [x] Session persistence
- [x] Export ZIP (multi-image)
- [x] Server-side cost guardrail
- [x] Platform presets
- [x] Batch 4-up
- [x] Product compositing
- [x] Quality tiers with real per-image pricing
- [x] BYOK + shared-key modes

### Phase 3: Production hardening — In progress
- [x] Split route implementations into Flask blueprints
- [x] Owner-scope sessions, pins, chats, and library assets
- [x] Fail closed for email identity and shared Figma credentials
- [x] Prevent pull-request deployment and require container smoke checks
- [x] Persist owner-scoped, idempotent batch job state and partial results
- [ ] Multi-brand workspaces (project → assets → export bundle)
- [ ] Asset library (reuse product PNGs across sessions)
- [ ] Review/comment system (collaborative)
- [ ] Shopify/Amazon CMS direct export
- [ ] Team accounts + spend attribution per workspace
- [ ] Inpainting / mask-based edits
- [ ] Onboarding wizard for new users

---

## Installation

### CLI Skill
```bash
git clone https://github.com/camster91/creative-studio.git
cd creative-studio/cli
pip install -r requirements.txt  # or: uv sync
---

## Web App (Live)

**Live URL:** https://photogen.ashbi.ca

```bash
# To run locally:
export GEMINI_API_KEY="..."
bash launch.sh  # or: python -m scripts.creative-studio-web
```

Production configuration, backups, rollback, and incident controls are defined
in [docs/PRODUCTION.md](docs/PRODUCTION.md). Security reports follow
[SECURITY.md](SECURITY.md).

---

## License

MIT — Free for commercial use.
