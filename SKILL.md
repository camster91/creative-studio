---
name: creative-studio
description: Iterative AI image generation with smart prompt enhancement, tier-based quality presets, Figma-aware design context, Midjourney-style pick-and-refine workflow, composite anti-hallucination pipeline, platform export presets, and auto QC.
---

# Creative Studio v4.5

**YOUR prompt = the model sees exactly what you wrote.**

No creative director rewriting. Just a clean pipeline from your exact prompt to the image.

---

## New: Three CPG/DTC Systems

### 1. Composite Pipeline (`composite`) — Zero Hallucinations
AI generates ONLY the environment (empty shelf, lighting, store interior). Your real product photo is composited on top with a soft drop shadow.

```bash
bash launch.sh composite \
  --prompt "Empty clean light wooden retail shelves in a premium supplement store. Warm overhead track lighting. No products, no bottles, no labels." \
  --product /tmp/gfuel-tub.png \
  --aspect-ratio 16:9 \
  --tier quality
```

**Flow:**
1. Remove background from your product photo (PIL threshold + feather)
2. Generate empty environment via AI (prompt explicitly excludes products)
3. Scale product to ~22% of scene width
4. Place product on lower shelf area (y≈72% of image)
5. Add soft drop shadow beneath
6. Save composite PNG

### 2. Export Pipeline (`export`) — One Image → All Platforms
Crop any image into multiple platform-specific formats.

```bash
bash launch.sh export \
  --input hero.png \
  --presets amazon,shopify,meta-feed,web-hero
```

**Presets:**
| Preset | Size | Ratio | BG | Use |
|--------|------|-------|-----|-----|
| `amazon` | 2000×2000 | 1:1 | white | PDP requirement |
| `shopify` | 2048×2048 | 1:1 | white | Square catalog |
| `meta-feed` | 1080×1350 | 4:5 | transparent | Instagram feed |
| `meta-stories` | 1080×1920 | 9:16 | transparent | Stories/Reels |
| `web-hero` | 1920×1080 | 16:9 | transparent | Website banner |
| `pinterest` | 1000×1500 | 2:3 | transparent | Pinterest |
| `print-dpi` | — | 3:2 | white | 300 DPI print |

### 3. Auto QC (`qc`) — Vision-based Quality Gate
Scan generated images for common CPG product photography issues.

```bash
bash launch.sh qc --input output.png
```

**Checks:**
- Floating products (not touching surface)
- Garbled text on labels
- Detached shadows
- Fake/off-brand products
- Label readability
- Overall quality score (1-10)

---

## Prompt Engineering Best Practices (Research-Backed)

### Prompt Structure Formula
```
[Subject] + [Environment/Setting] + [Style/Medium] + [Lighting] + [Composition/Camera] + [Mood/Atmosphere]
```

### CPG Product Photography Tips
1. **Subject**: Use exact product name. "G FUEL Berry Bomb tub" not "a pink container"
2. **Style**: "professional product photography", "commercial editorial shot"
3. **Lighting**: Name real setups
   - `softbox three-point studio lighting` — clean catalog
   - `warm overhead track lighting with soft shadows` — retail shelf
   - `golden hour side-lighting` — lifestyle
4. **Camera**: Reference real equipment
   - `Shot on Hasselblad H6D medium format`
   - `Canon EF 85mm f/1.4`
   - `Fujifilm X-T5, 35mm lens`
5. **Shelf physics**: Always include
   - `shelf perfectly flat and level`
   - `product sits firmly with flat base touching shelf`
   - `soft contact shadow beneath`
6. **Negative prompts**: blurry, lowres, distorted, watermark, signature, text, plastic look

### What NOT to Do
- ❌ "photorealistic" — causes plastic, doll-like look
- ❌ "8K" / "4K" — doesn't increase quality, wastes tokens
- ❌ Stacked superlatives: "beautiful stunning gorgeous"
- ❌ Keyword stuffing without structure
- ❌ Forgetting negative prompts (especially for Stable Diffusion/Gemini)

---

## Midjourney-Style Workflow (Pick-and-Refine)

```bash
# Step 1: Generate 4 variations
bash launch.sh variations \
  --prompt "G FUEL Berry Bomb on a clean wooden retail shelf with other G FUEL products" \
  --input-image product.png \
  --tier quality \
  --variations 4

# Step 2: Pick your favorite + refine
bash launch.sh refine \
  --session vars-123456 \
  --pick v2 \
  --changes "Shelf should be flat and horizontal. Product sits firmly with base touching shelf. Add more G FUEL flavors on surrounding shelves."
```

**Variations**: 4 outputs numbered `v1.png` to `v4.png`, each with a different angle/lighting/DoF.
**Refine**: Picks one, applies your changes, and generates `r2-{timestamp}.png`.

---

## Quality Tiers (`--tier`)

| Tier | Model | Resolution | Use Case | Cost |
|------|-------|-----------|----------|------|
| `fast` | Flash | 1K | Quick drafts, ideation | ~$0.07 |
| `balanced` | Flash | 2K | Default — speed/quality | ~$0.07 |
| `quality` | **Pro** | **2K** | Production work | ~$0.20 |
| `ultra` | **Pro** | **4K** | Maximum detail, print | ~$0.40 |

When you pass `--tier`, model and resolution are auto-selected. Override with `--model` if needed.

---

## Smart Prompt Enhancement (`--smart`)

The reasoning model (`gemini-3.1-pro-preview`) analyzes your brief and auto-crafts:

- **Camera angle** (eye-level, hero tilt, overhead)
- **Lighting setup** (softbox, track lighting, rim light, color temperature)
- **Material/texture details** for physical objects
- **Mood words** for environment
- **Critical shelf physics:** flat level shelves, products sit firmly, no tilting/floating/falling
- **Negative prompts** (blurry, deformed, watermark, plastic look, etc.)
- **Professional camera references** (Hasselblad, Canon, Fujifilm)

**Important: Your subject/product/brand is NEVER changed.**

### Smart Enhancement Output

```json
{
  "prompt": "G FUEL Berry Bomb tub, resting firmly on a perfectly flat and level light oak wooden retail shelf...",
  "negative_prompt": "floating, tilting, distorted text, plastic texture, messy background",
  "aspect_ratio": "16:9",
  "lighting_setup": "Overhead track lighting with soft shadows and a subtle warm rim light",
  "camera_angle": "Eye-level angle, Shot on Hasselblad H6D medium format",
  "notes": "Preserve exact G FUEL Berry Bomb tub design"
}
```

---

## Commands

| Command | Description | Best For |
|---------|-------------|----------|
| `direct` | One-shot generation | Quick drafts, exact control |
| `chat` | Multi-turn iteration | Step-by-step refinement |
| `variations` | Generate N variations (like Midjourney) | Pick-and-refine workflow |
| `refine` | Pick variation + apply changes | Iterating toward final |
| `composite` | AI env + real product (zero hallucinations) | Branded product on shelf |
| `export` | Crop to platform formats (Amazon, Meta, etc.) | Multi-platform assets |
| `qc` | Vision-based quality check | Finding issues before delivery |
| `figma` | Design-aware generation | Matching existing Figma aesthetics |
| `brainstorm` | Q&A → 4 directions → generate | Exploring options |
| `analyze` | Vision model reads reference | Understanding a reference image |
| `quality` | Size/brightness check | Basic QC |
| `review` | Browse all outputs | Finding past work |

---

## CLI Flags

| Flag | Description |
|------|-------------|
| `--prompt, -p` | Your exact prompt text |
| `--input-image, -i` | Reference image |
| `--product, -i` | Product photo for composite |
| `--model, -m` | Override model |
| `--resolution, -r` | `1K`, `2K`, `4K` |
| `--format, -f` | Output folder name |
| `--filename` | Custom output filename |
| `--tier` | `fast` / `balanced` / `quality` / `ultra` |
| `--smart` | Enable reasoning-based prompt enhancement |
| `--variations, -v` | Number of variations (1-8, default 4) |
| `--session, -s` | Session folder for refine |
| `--pick` | Which variation to refine (v1, v2, etc.) |
| `--changes, -c` | What to change in refinement |
| `--url, -u` | Figma URL |
| `--presets` | Comma-separated export presets |

---

## Example Workflows

### Direct (one-shot)

```bash
bash launch.sh direct \
  --prompt "G FUEL Berry Bomb tub, resting firmly on a flat light oak retail shelf in a premium supplement store, commercial editorial shot, overhead track lighting with soft shadows, shallow depth of field, Shot on Hasselblad H6D, 100mm f/2.8" \
  --input-image product.png \
  --tier quality --smart
```

### Composite (zero hallucinations)

```bash
bash launch.sh composite \
  --prompt "Empty clean light wooden retail shelves in a premium supplement store. Warm overhead track lighting. No products anywhere." \
  --product product.png \
  --aspect-ratio 16:9
```

### Export (multi-platform)

```bash
bash launch.sh export \
  --input final-hero.png \
  --presets amazon,shopify,meta-feed,meta-stories
```

### Midjourney-style pick-and-refine

```bash
bash launch.sh variations \
  --prompt "G FUEL shelf display" \
  --input-image product.png \
  --tier quality \
  -v 4

bash launch.sh refine \
  --session vars-123456 \
  --pick v2 \
  --changes "Shelf must be flat. Product sits firmly with base touching shelf."
```

### Figma-aware with smart enhancement

```bash
bash launch.sh figma \
  --url "https://www.figma.com/design/..." \
  --prompt "Hero banner for case study" \
  --input-image product.png \
  --tier quality --smart
```

---

## Models

| Model | Best For | Cost |
|-------|---------|------|
| `gemini-3.1-flash-image-preview` | Image-to-image edits, fast iteration | ~$0.07 |
| `gemini-3-pro-image-preview` | Complex composition, max quality | ~$0.20 |
| `imagen-4.0-generate-001` | Text-to-image backgrounds only | ~$0.04 |

---

## Output

All files go to `C:\Users\camst\Downloads\creative-studio-outputs\YYYY-MM-DD\format\`

Session folders: `C:\Users\camst\Downloads\creative-studio-outputs\YYYY-MM-DD\format\vars-{timestamp}\`

JSON logs accompany every generation.

---

## Environment

```bash
export GEMINI_API_KEY="your-key-here"
export FIGMA_ACCESS_TOKEN="figd_..."
```

---

## Recipes

JSON prompt templates in `recipes/`:

| Recipe | Use |
|--------|-----|
| `product-on-shelf` | Branded product on retail shelf |
| `social-media-hero` | 16:9 web banner / landing page hero |
| `lifestyle-in-use` | Product in someone's hands |

---

## MCP Integration

Standalone MCP server registered with mcporter:

```bash
mcporter list figma --schema
mcporter call figma.figma_get_node file_key=... node_id=...
```

Tools: `figma_get_file`, `figma_get_node`, `figma_download_image`, `figma_get_component_set`, `figma_get_comments`
