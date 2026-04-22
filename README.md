# CPG/DTC AI Photography Studio

> AI-powered product photography for Consumer Packaged Goods (CPG) and Direct-to-Consumer (DTC) brands.
> Your exact prompts go straight to the model. No creative director rewriting.

## Two Products in One Repo

| Product | Description | Audience |
|---------|-------------|----------|
| **CLI Skill** | Command-line tool for Pi coding agent and power users | Developers, AI researchers |
| **Web App** *(concept)* | Visual platform for marketing teams to generate product photography at scale | CPG brands, DTC marketers |

---

## CLI Skill (Current — v4.3)

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

## Web App (Concept / Roadmap)

### Vision
A visual creative operations platform where CPG marketing teams can:
- Upload product photos → get campaign-ready images in minutes
- Manage projects by brand/campaign
- Batch-process 50+ SKUs against scene templates
- Export in format presets (Amazon, Shopify, Meta, etc.)

### Architecture

```
web/
├── app.py                 # Flask server
├── models.py              # Project, Asset, Template, SKU tables
├── services/
│   ├── generator.py       # Wraps creative_studio.py
│   ├── batch.py           # Batch processing queue
│   ├── qc.py              # Auto quality check (blur, brand safety)
│   └── export.py          # ZIP presets for Amazon/Shopify/Meta
├── templates/
│   ├── index.html         # Upload + brief
│   ├── compare.html       # Side-by-side v1-v4 grid
│   ├── refine.html        # Pick + changes
│   ├── project.html       # Saved campaigns
│   └── export.html        # Format presets
├── static/           
│   ├── app.js             # Frontend logic
│   └── style.css          # Tailwind / custom
└── alembic/               # DB migrations
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

### Phase 1: CLI Skill (v4.x) — Now
- [x] Prompt enhancement engine
- [x] Quality tiers
- [x] Pick-and-refine workflow
- [x] Figma integration
- [x] Vision pre-analysis
- [ ] True aspect ratio control (PIL post-crop)
- [ ] Batch mode (CSV input)
- [ ] Inpainting / mask-based edits
- [ ] Auto-QC (blur detection, brand safety)

### Phase 2: Web App MVP — Next
- [ ] Flask + SQLite scaffold
- [ ] Upload → generate → compare flow
- [ ] Project persistence
- [ ] Export ZIP with format presets
- [ ] Basic auth

### Phase 3: Scale — Later
- [ ] Celery batch processing
- [ ] Multi-brand workspaces
- [ ] Asset library (reuse product PNGs)
- [ ] Review/comment system
- [ ] Shopify/Amazon CMS export

---

## Installation

### CLI Skill
```bash
git clone https://github.com/camster91/creative-studio.git
cd creative-studio/cli
pip install -r requirements.txt  # or: uv sync
bash launch.sh --help
```

### Web App (coming)
```bash
cd creative-studio/web
pip install -r requirements.txt
flask run
```

---

## License

MIT — Free for commercial use.
