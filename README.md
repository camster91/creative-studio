# CPG/DTC AI Photography Studio

> AI-powered product photography for Consumer Packaged Goods (CPG) and Direct-to-Consumer (DTC) brands.
> Your exact prompts go straight to the model. No creative director rewriting.

## Two Products in One Repo

| Product | Description | Audience |
|---------|-------------|----------|
| **CLI Skill** | Command-line tool for power users | Developers, AI researchers |
| **Web App** | Visual platform for marketing teams to generate product photography at scale | CPG brands, DTC marketers |

**Live web app:** https://photogen.ashbi.ca (v4.5.1)

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

### Current (v4.5.1) — Shipped to photogen.ashbi.ca

- Direct generation (text-to-image, BYOK via Gemini API)
- Product compositing (upload your packaging, AI builds the scene around it)
- 4 quality tiers (Fast $0.02 / Balanced $0.05 / Quality $0.09 / Ultra $0.24)
- 6 aspect ratios (1:1, 4:3, 16:9, 9:16, 2:3, 4:5)
- 4 platform presets (Amazon / Instagram / Email / Pinterest) — auto-set prompt + aspect
- Batch 4-up (parallel generation with streaming partial results)
- Server-side daily cost guardrail (`CREATIVE_DAILY_LIMIT`, default $5/day)
- Live session gallery with multi-select + ZIP export
- Pin annotations, refine, variations, Figma context, chat mode
- Cost tracking (per-image, per-day, per-session)
- Lightbox, skeleton loaders, prompt history, copy-prompt, Ctrl+Enter
- `/api/whoami` endpoint to surface BYOK vs shared-key status

### Architecture

```
web/                       # Flask app (currently bundled in scripts/creative-studio-web.py)
├── app.py                 # Flask server (3571 lines, monolithic — see Phase 3)
├── services/
│   ├── generator.py       # Wraps creative_studio.py (run_cli_generate, run_cli_composite, etc.)
│   ├── session.py         # Session persistence (JSON in ~/.creative-studio-data/sessions)
│   ├── costs.py           # Cost tracking + daily limit enforcement
│   └── quality.py         # Vision-based QC scoring
├── templates/
│   ├── editor.html        # Main editor (currently inline in creative-studio-web.py)
│   ├── status.html        # /status page
│   └── history.html       # /history page
└── static/
    ├── app.js             # Frontend logic (currently inline)
    └── style.css          # Dark theme + accent
```

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
| Database | SQLite (local) / PostgreSQL (production) |
| Queue | Celery + Redis for batch jobs |
| Frontend | Vanilla JS + HTMX (fast, no build step) |
| Storage | Local disk (dev) / S3 (production) |
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

### Phase 3: Scale — Next
- [ ] Split creative-studio-web.py monolith into Flask blueprints
- [ ] Move from in-process jobs to Celery + Redis for batch processing
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
# Already deployed on Coolify (187.77.26.99)
# To redeploy: see .github/workflows/deploy.yml
# To run locally:
export GEMINI_API_KEY="..."
bash launch.sh  # or: python -m scripts.creative-studio-web
```

---

## License

MIT — Free for commercial use.

<!-- Deploy pipeline verify run 2026-06-03 (no-op, closes unmerged) -->
