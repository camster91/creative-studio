# Creative Studio (Photogen)

AI product photography for CPG and DTC brands. Your exact prompt goes straight to the image model, with no hidden "creative director" rewriting.

## What it is

Creative Studio is two tools that share one generation core:

- **Web app (Photogen):** a Flask app where marketing teams upload a product photo, pick a scene, and generate on-brand product shots. A guided Campaign Factory turns brand and SKU details into repeatable, checked production runs.
- **CLI:** a command-line tool for power users that runs the same Google Gemini image pipeline from the terminal, including a variations then refine workflow and Figma-aware generation.

The goal is predictable product imagery: the real packaging is composited into AI-generated scenes, so the product itself is never hallucinated.

## Key features

### Web app

- Text-to-image and image-to-image generation with Google Gemini, using your own API key (BYOK) or an optional shared key
- Product compositing: upload transparent packaging and the AI builds only the scene around it
- Scene presets (In-hand, Studio, Action, Lifestyle, With props) and six aspect ratios
- Four quality tiers (fast, balanced, quality, ultra) with per-image cost tracking
- Batch generation with background jobs and streaming partial results
- Version history with branching, favorites, undo, and cost recovery
- Pin annotations, refine, variations, and a multi-turn chat mode
- Advisory quality checks (QC) and multi-format export bundles (ZIP)
- Campaign Factory: Brand Passport, SKU Product Truth, deterministic readiness checks, and channel export bundles
- Accounts with magic-link login, Stripe billing, Figma OAuth for design context
- Server-side daily spend limit, rate limiting, upload validation, and owner-scoped data
- Privacy-safe provider ledger (no prompts, images, or keys stored) for cost, latency, and outcome metrics

### CLI

| Command | What it does |
|---|---|
| `direct` | One-shot generation from your exact prompt |
| `chat` | Multi-turn session where each output feeds the next turn |
| `variations` | Generate 1 to 8 variations to pick from |
| `refine` | Pick a variation and refine it |
| `composite` | Generate a background, then place the real product on top |
| `figma` | Generate an asset that matches a Figma design's layout and colors |
| `brainstorm` | Ask clarifying questions and surface directions before generating |
| `analyze`, `qc`, `quality` | Vision analysis and automatic quality checks |
| `export` | Crop one image into Amazon, Shopify, Meta, Pinterest, web hero, and print formats |
| `review` | Browse output folders |

Most generation commands accept `--tier`, `--aspect-ratio`, and `--smart` (prompt enhancement with a reasoning model). Prompt recipes live in `recipes/`.

## Tech stack

- **Backend:** Python 3.10+, Flask, gunicorn
- **AI:** Google Gemini via `google-genai`
- **Images:** Pillow
- **Data:** SQLite plus JSON on persistent volumes
- **Payments:** Stripe
- **Frontend:** server-rendered HTML templates with vanilla JavaScript and CSS
- **Tooling:** uv, pytest, Playwright (browser release gate with axe-core), Docker
- **CI:** GitHub Actions (pytest, JS syntax check, Playwright, CodeQL)

## Getting started

Requirements: Python 3.10+ (3.12 recommended), [uv](https://docs.astral.sh/uv/), and a Google Gemini API key. Node 20 is only needed for browser tests.

```bash
git clone https://github.com/camster91/creative-studio.git
cd creative-studio
make install            # creates .venv and installs the app with test deps
```

Configuration is read from environment variables. Copy `.env.example` as a reference for the available settings (never commit real keys).

### Run the web app

```bash
export GEMINI_API_KEY="your-key"
.venv/bin/python scripts/creative-studio-web.py --port 5173
```

Then open http://localhost:5173. The editor is at `/app` and the Campaign Factory at `/campaigns`.

Or run it in Docker:

```bash
make build
docker run -p 5173:5173 -e GEMINI_API_KEY="your-key" creative-studio:latest
```

### Run the CLI

```bash
export GEMINI_API_KEY="your-key"
bash launch.sh variations --prompt "Protein tub on a clean oak shelf, warm light" \
  --input-image product.png --tier quality --aspect-ratio 16:10 -v 4
bash launch.sh refine --session <vars-folder> --pick v2 --changes "softer shadows"
```

`FIGMA_ACCESS_TOKEN` is optional and only needed for the `figma` command.

## Testing

```bash
make test               # pytest suite in tests/
make test-js            # syntax check for static/app.js
npm ci
npx playwright install chromium
npm test                # Playwright browser release gate (starts the app itself)
```

## Project structure

```
creative_studio_app/   Service modules and Flask route blueprints
scripts/               Web entry point, CLI, Figma and vision helpers, backups
templates/             HTML pages (landing, editor, campaigns, billing, auth)
static/                Frontend JavaScript and CSS
recipes/               Prompt recipe templates
content/blog/          Blog posts served by the app
tests/                 pytest suite and Playwright browser tests
docs/                  Architecture, QC, and release notes
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/QC.md](docs/QC.md), and [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

### Product photoshoots (`/shoot`)

Upload a PNG, JPG or WebP (up to 16 MB), choose a vibe and press Go. The pack
contains six channel formats by default; Customise changes quality, shots and
a short brand note. Progress resumes when you return. Review each label against
the real packaging before publishing; download individual shots or the ZIP.

`PHOTOGEN_IMAGE_PROVIDER` selects `auto` (default), `higgsfield` or `gemini`.
Only Gemini accepts a user's own key. Higgsfield uses the server's account and
always requires a signed-in user with enough PhotoGen credits.

Successful Higgsfield renders enter the shared `costs.json` daily spend ledger.
Per-render USD estimates default to $0.08 Balanced, $0.12 High and $0.72 Ultra;
set `HIGGSFIELD_COST_USD_BALANCED`, `HIGGSFIELD_COST_USD_QUALITY` and
`HIGGSFIELD_COST_USD_ULTRA` to your account's actual rates. These are planning
estimates, not provider price guarantees. Invalid or nonpositive overrides use
the defaults. Local white-background cutouts and failed renders add no provider
spend. `CREATIVE_DAILY_LIMIT` checks this ledger using the same estimates.

Failed-shot refunds use per-pack/per-shot keys in `photoshoot_refunds` inside
the existing auth SQLite database. The credit return and guard commit together;
recovery safely retries the key before settling the pack JSON. Keep both the
auth database and `data/packs/` in persistent backups.

The shoot page uses the PhotoGen assets in `static/photogen/` and a self-hosted
Archivo variable Latin font (weight and width axes). The app currently sets
no CSP. No external font origins are needed.

Refresh the mocked screenshot set with:

```sh
CI=1 SHOOT_SCREENSHOTS=1 SHOOT_SCREENSHOT_DIR=/workspace/photogen-rebuild/screenshots npx playwright test tests/browser/shoot.spec.js
```

The run writes the same PNGs to `docs/screenshots/` and the requested directory.
Sample product photos are illustrative brand assets, not real customer outputs.
