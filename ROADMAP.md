# Creative Studio — Web App Roadmap

Current status: v5.0 deployed at https://photogen.ashbi.ca

## ✅ Done
- [x] Direct generation (exact prompt passthrough, no rewriting)
- [x] Tier selection (Fast / Balanced / Quality)
- [x] Optional reference image upload (drag-and-drop)
- [x] Smart prompt enhancement (toggleable, defaults OFF)
- [x] Aspect ratio selection (hidden behind Advanced toggle)
- [x] Cost tracking (live footer)
- [x] Responsive layout
- [x] Docker containerized deployment
- [x] Production deployment on Coolify VPS
- [x] Spatial pin annotations (CLI + web)

## 🚧 CLI-Only Features — Need Web UI Integration

### 1. Composite Pipeline
**What it is**: AI generates ONLY the environment/background. User uploads a real product photo. Tool auto-removes background, composites product onto scene with drop shadow.
**Why it matters**: Zero hallucination of fake products/flavors. Real product on AI scene.
**CLI command**: `bash launch.sh composite --prompt "..." --product product.png`
**Web effort**: Add second upload (product photo). Background removal step. Compositing layer.

### 2. Export Presets
**What it is**: One click exports generated image to Amazon (1:1 white), Shopify (2048×2048), Meta Feed (4:5), Meta Stories (9:16), Pinterest (2:3), Web Hero (16:9), Print (300 DPI).
**Why it matters**: Same asset needs different crops for every platform.
**CLI command**: `bash launch.sh export --input image.png --presets amazon,shopify,meta-feed`
**Web effort**: Download buttons per preset. Multi-format generation.

### 3. QC Gate (Quality Check)
**What it is**: Vision AI scans output for 5 criteria: floating products, garbled text, detached shadows, fake products, readable labels. Returns PASS/FAIL + 1-10 score.
**Why it matters**: Catches hallucinations before they go to client.
**CLI command**: `bash launch.sh qc --input image.png`
**Web effort**: Add QC button next to each generated image. Show score ring + pass/fail grid.

### 4. Variations + Refine (Midjourney-style)
**What it is**: Generate 4 variations → pick one → refine with changes → repeat.
**Why it matters**: Best UX for iterating. Pick visually, then prompt specific changes.
**CLI commands**: `bash launch.sh variations --prompt "..." -v 4` then `bash launch.sh refine --session X --pick v2 --changes "..."`
**Web effort**: 4-up grid selection. Session tracking. Refine input panel.

### 5. Figma Integration
**What it is**: Paste a Figma URL. Tool fetches design context (colors, fonts, layout) and incorporates into prompt + posts result back as Figma comment.
**Why it matters**: Design-aware generation keeps brand consistency.
**CLI command**: `bash launch.sh figma --url "..." --prompt "..."`
**Web effort**: Figma URL input node. OAuth flow for write access.

### 6. Chat Mode (Multi-turn)
**What it is**: Generate → review → prompt changes → generate again in same session. Each output becomes next input. Commands: `done`, `restart`, `back`, `save <name>`.
**Why it matters**: Natural iterative workflow. Conversational refinement.
**CLI command**: `bash launch.sh chat --name "session" --input-image product.png`
**Web effort**: Full chat UI with history. Branching versions. Save/load sessions.

## 💡 Nice-to-Have
- [ ] Interactive canvas annotations (brush tool for "fix this area")
- [ ] Before/after comparison slider
- [ ] Prompt history + favorites
- [ ] Version timeline with thumbnails
- [ ] Batch staging area (queue multiple prompts)
- [ ] Command palette (keyboard shortcuts)
- [ ] Onboarding wizard for new users
- [ ] Cost budget alerts ("you've spent $5 today")
- [ ] Team sharing (share sessions via URL)
