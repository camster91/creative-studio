---
name: creative-studio
description: Iterative AI image generation with smart prompt enhancement, tier-based quality presets, Figma-aware design context, and Midjourney-style pick-and-refine workflow.
---

# Creative Studio v4.3

**YOUR prompt = the model sees exactly what you wrote.**

No creative director rewriting. No iteration loop overriding your instructions. Just a clean pipeline from your exact prompt to the image.

---

## Midjourney-Style Workflow (New)

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
  --changes "Shelf should be flat and horizontal. Product sits firmly with flat base touching shelf. Add more G FUEL flavors on surrounding shelves."

# Step 3 (optional): Re-refine
bash launch.sh refine \
  --session vars-123456 \
  --pick r2 \
  --changes "Warm lighting, shallow depth of field"
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
- **Lighting setup** (softbox, edge lighting, color temperature)
- **Material/texture detail** for physical objects
- **Mood words** for environment
- **Critical shelf physics:** flat level shelves, products sit firmly, no tilting/floating/falling
- **Negative prompts** (blurry, deformed, watermark, etc.)

**Important: Your subject/product/brand is NEVER changed.**

### Smart Enhancement Output

```json
{
  "prompt": "Professional product photography of ...",
  "negative_prompt": "messy, blurry, deformed, watermark, ...",
  "aspect_ratio": "16:9",
  "lighting_setup": "Overhead softbox, neutral 5600K, crisp edge lighting",
  "camera_angle": "Eye-level, slight upward hero tilt",
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
| `figma` | Design-aware generation | Matching existing Figma aesthetics |
| `brainstorm` | Q&A → 4 directions → generate | Exploring options |
| `analyze` | Vision model reads reference | Understanding a reference image |
| `quality` | Size/brightness check | QC before delivery |
| `review` | Browse all outputs | Finding past work |

---

## CLI Flags

| Flag | Description |
|------|-------------|
| `--prompt, -p` | Your exact prompt text |
| `--input-image, -i` | Reference image |
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

---

## Example Workflows

### Direct (one-shot)

```bash
bash launch.sh direct \
  --prompt "Your exact prompt here" \
  --input-image product.png \
  --tier quality --smart
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
  --changes "Shelf must be flat and horizontal. Product sits firmly with base touching shelf."
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
