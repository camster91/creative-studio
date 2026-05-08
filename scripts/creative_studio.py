#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.0.0",
#     "pillow>=10.0.0",
# ]
# ///
"""
Creative Studio — Iterative Image Workflow

Commands:
    direct     One-shot generation. YOUR prompt goes straight to the model.
    chat       Multi-turn conversation. Each result feeds into the next prompt.
    analyze    Visual analysis of a reference image.
    quality    Basic image quality check (brightness, size).
    review     Browse all outputs.

Usage:
    bash launch.sh direct --prompt "..." --input-image ref.png
    bash launch.sh chat --name "gfuel-shelf" --input-image ref.png
    bash launch.sh analyze --input layout.png
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from figma_utils import (
    parse_figma_url,
    fetch_figma_context,
    enhance_prompt_with_figma,
    post_figma_comment,
)

REASONING_MODEL = (
    "gemini-3.1-pro-preview"  # Latest reasoning model for planning/analysis
)

API_KEY = os.environ.get("GEMINI_API_KEY", "")
# ─── Config ──────────────────────────────────────────────────────────

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GENAI_CLIENT = None

# Models: nano-banana-2 for image-to-image, imagen-4 for text-to-image
NANO_MODEL = "gemini-3.1-flash-image-preview"
NANO_PRO = "gemini-3-pro-image-preview"
IMAGEN_MODEL = "imagen-4.0-generate-001"

# Quality tiers — map tier names to (model, resolution)
_TIER_MAP = {
    "fast": (NANO_MODEL, "1K"),
    "balanced": (NANO_MODEL, "2K"),
    "quality": (NANO_PRO, "2K"),
    "ultra": (NANO_PRO, "4K"),
}

_DEFAULT_NEGATIVES = (
    "blurry, out of focus, distorted, deformed, ugly, disfigured, mutated, cropped, "
    "low quality, watermark, signature, text error, gibberish text, extra text, "
    "extra limbs, floating objects, unrealistic physics, plastic look, doll-like, "
    "oversaturated, overexposed, underexposed, chromatic aberration"
)

# Output directory — prefer Windows Downloads when in WSL
if os.environ.get("CREATIVE_OUTPUT_DIR"):
    _OUT = Path(os.environ["CREATIVE_OUTPUT_DIR"])
elif Path("/mnt/c/Users").exists():
    # WSL: try to find the user's Downloads folder
    try:
        import subprocess as _sp

        _user = _sp.run(
            ["cmd.exe", "/c", "echo %USERNAME%"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        _win_dl = Path(f"/mnt/c/Users/{_user}/Downloads/creative-studio-outputs")
        if _win_dl.parent.exists():
            _OUT = _win_dl
        else:
            _OUT = Path.home() / "creative-studio-outputs"
    except Exception:
        _OUT = Path.home() / "creative-studio-outputs"
else:
    _OUT = Path.home() / "creative-studio-outputs"


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stage_input(input_path: Optional[str]) -> Optional[str]:
    """Copy input files with special characters to a clean temp path."""
    if not input_path:
        return None
    src = Path(input_path)
    if not src.exists():
        # Try shell glob for special-character filenames
        import glob

        matches = glob.glob(str(src))
        if matches:
            src = Path(matches[0])
        else:
            print(f"  ⚠ Input image not found: {input_path}", file=sys.stderr)
            return None
    # If path is clean and under /tmp, just return it
    clean = str(src)
    if clean.startswith("/tmp/") and " " not in clean and "\u00a0" not in clean:
        return clean
    # Copy to clean temp path
    try:
        import shutil

        ext = src.suffix.lower() if src.suffix else ".png"
        dest = f"/tmp/cs_input_{hashlib.md5(str(src).encode()).hexdigest()[:8]}{ext}"
        shutil.copy2(str(src), dest)
        return dest
    except Exception as e:
        print(f"  ⚠ Could not stage input image: {e}", file=sys.stderr)
        return None


def get_genai_client():
    global GENAI_CLIENT
    if GENAI_CLIENT is None:
        from google import genai

        GENAI_CLIENT = genai.Client(api_key=API_KEY)
    return GENAI_CLIENT


def now_str() -> str:
    return datetime.now().strftime("%H%M%S")


def _ensure_png(fname: str) -> str:
    """Force .png extension."""
    if not fname.lower().endswith(".png"):
        return f"{fname}.png"
    return fname


def crop_to_aspect_ratio(img, target_ratio: str):
    """PIL center-crop image to target aspect ratio."""
    from fractions import Fraction

    w, h = img.size
    r = Fraction(target_ratio.replace(":", "/"))
    target_wh = float(r)
    current_wh = w / h
    if target_wh > current_wh:
        new_h = int(w / target_wh)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif target_wh < current_wh:
        new_w = int(h * target_wh)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    return img


# ─── Config persistence ────────────────────────────────────────────────


class Config:
    """Persistent preferences in ~/.creative-studio.json"""

    PATH = Path.home() / ".creative-studio.json"

    def __init__(self):
        self._data = {
            "default_tier": "balanced",
            "default_model": NANO_MODEL,
            "default_aspect_ratio": "16:9",
            "default_resolution": "2K",
            "brand_profiles": {},
            "cost_total_usd": 0.0,
            "generation_count": 0,
            "last_outputs": [],
        }
        if self.PATH.exists():
            try:
                raw = json.loads(self.PATH.read_text())
                self._data.update(raw)
            except Exception:
                pass

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def save(self):
        try:
            self.PATH.write_text(json.dumps(self._data, indent=2, default=str))
        except PermissionError:
            pass

    def add_brand(self, name: str, colors, products, logo_path=""):
        self._data["brand_profiles"][name] = {
            "colors": colors,
            "products": products,
            "logo_path": logo_path,
            "updated": __import__("datetime").datetime.now().isoformat(),
        }
        self.save()

    def get_brand(self, name):
        return self._data["brand_profiles"].get(name)

    def search_brand(self, prompt: str):
        lower = prompt.lower()
        for brand_name in self._data["brand_profiles"]:
            if brand_name.lower() in lower:
                return brand_name
        return None

    def add_cost(self, cost_usd):
        self._data["cost_total_usd"] = self._data.get("cost_total_usd", 0.0) + cost_usd
        self._data["generation_count"] = self._data.get("generation_count", 0) + 1
        self.save()

    def track_output(self, path):
        self._data.setdefault("last_outputs", []).insert(0, str(path))
        self._data["last_outputs"] = self._data["last_outputs"][:20]
        self.save()


CONFIG = Config()


# ─── Cost tracking ──────────────────────────────────────────────────

PRICE_CARD = {
    "gemini-3.1-flash-image-preview": {"1K": 0.045, "2K": 0.090, "4K": 0.180},
    "gemini-3-pro-image-preview": {"1K": 0.134, "2K": 0.240, "4K": 0.480},
    "imagen-4.0-generate-001": {"1K": 0.040, "2K": 0.040, "4K": 0.060},
    "__default__": 0.100,
}


# Supported Gemini image aspect ratios (for validation)
_VALID_RATIOS = {
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
}
_RATIO_FALLBACK = {"16:10": "16:9"}


def resolve_aspect_ratio(raw: str) -> str:
    r = raw.strip()
    if r in _VALID_RATIOS:
        return r
    return _RATIO_FALLBACK.get(r, "16:9")


def estimate_cost(model: str, resolution: str) -> float:
    m = PRICE_CARD.get(model, PRICE_CARD["__default__"])
    if isinstance(m, dict):
        return m.get(resolution, PRICE_CARD["__default__"])
    return m


# ─── Prompt Enhancement Engine ────────────────────────────────────────

# ─── Vision Pre-Analysis ──────────────────────────────────────────────


def vision_analyze(input_image_path: str) -> dict:
    """Analyze the input reference image before generation."""
    from PIL import Image
    from google.genai import types

    p = Path(input_image_path)
    if not p.exists():
        return {"error": "File not found"}

    img = Image.open(str(p))
    w, h = img.size
    orientation = "landscape" if w > h else ("portrait" if h > w else "square")

    client = get_genai_client()
    prompt = (
        "You are a reference image analyst for AI image generation. "
        "Describe exactly what you see so the prompt engineer can place this exact "
        "subject into a scene without changing it.\n\n"
        "Analyze the image and return ONLY a JSON object with:\n"
        "subject_type, dominant_colors, key_text, physical_shape, "
        "is_photo_or_render, angle_view, lighting_quality, background_type, "
        "things_to_preserve, things_that_might_get_lost_during_editing.\n\n"
        "Be specific and literal."
    )
    try:
        resp = client.models.generate_content(
            model=NANO_MODEL,
            contents=[img, prompt],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw = resp.text or "{}"
        raw = re.sub(r"```json\s*|```\s*", "", raw)
        parsed = json.loads(raw.strip())
        parsed["width"] = w
        parsed["height"] = h
        parsed["orientation"] = orientation
        return parsed
    except Exception as e:
        return {"error": str(e), "width": w, "height": h, "orientation": orientation}


def smart_enhance_prompt(
    brief: str,
    has_reference_image: bool = False,
    figma_context: Optional[dict] = None,
    tier: str = "balanced",
) -> dict:
    """
    Use the reasoning model to craft a professional generation prompt.
    Returns a dict with: prompt, negative_prompt, model, resolution, aspect_ratio, notes
    """
    model, resolution = _TIER_MAP.get(tier, (NANO_PRO, "2K"))

    # Build design context string
    design_notes = ""
    if figma_context:
        fills = figma_context.get("fills", [])[:5]
        fonts = figma_context.get("fonts", [])[:3]
        layout = list(set(figma_context.get("layout", [])))
        if fills:
            design_notes += f"\nDesign palette: {', '.join(fills)}. "
        if fonts:
            design_notes += f"Typography: {', '.join(fonts)}. "
        if layout:
            design_notes += f"Layout orientation: {', '.join(layout)}. "

    # Whether to use Pro for enhancement depends on tier
    enhancer_model = REASONING_MODEL if tier in ("quality", "ultra") else NANO_MODEL

    system_prompt = (
        "You are a senior CPG/DTC product photographer and prompt engineer. "
        "Turn the user's brief into a professional image generation prompt using this exact structure:\n\n"
        "[Subject] + [Environment/Setting] + [Style/Medium] + [Lighting] + [Composition/Camera] + [Mood/Atmosphere]\n\n"
        "RULES:\n"
        "1. Subject: Use the user's exact product name/brand. Do NOT change or rename it.\n"
        "2. Environment: Describe precisely. 'Clean light wooden retail shelf' not 'nice background'.\n"
        "3. Style: Use 'professional product photography', 'commercial editorial shot', or 'lifestyle product photography'.\n"
        "4. Lighting: Name specific setups: 'softbox three-point studio lighting', 'warm golden hour backlight', 'overhead track lighting with soft shadows', 'Rembrandt side-lighting'.\n"
        "5. Camera: Reference real equipment: 'Shot on Fujifilm X-T5 35mm f/1.4', 'Hasselblad H6D medium format', 'Canon EF 85mm f/1.4'.\n"
        "6. Composition: 'eye-level angle', 'shallow depth of field', 'macro close-up', 'rule of thirds'.\n"
        "7. Mood: Use precise atmosphere words: 'clean and minimalist', 'warm and inviting', 'premium and luxurious'.\n"
        "8. CRITICAL shelf physics: Shelf must be perfectly FLAT and LEVEL. Products sit firmly with flat base touching shelf. No tilting, no floating, no falling. Add 'soft contact shadow beneath' to ground the product.\n"
        "9. NEVER use: 'photorealistic' (causes plastic look), '8K'/'4K' (doesn't increase quality, adds noise), stacked superlatives like 'beautiful stunning gorgeous'.\n"
        "10. Output ONLY JSON: {prompt, negative_prompt, aspect_ratio, lighting_setup, camera_angle, notes}.\n\n"
        f"User brief: {brief}\n"
        f"Reference image provided: {'yes' if has_reference_image else 'no'}. "
        f"Preserve the reference subject exactly, place in described scene.\n"
        f"Target tier: {tier} ({model}, {resolution}).\n"
        f"{design_notes}"
    )

    client = get_genai_client()
    try:
        resp = client.models.generate_content(
            model=enhancer_model, contents=system_prompt, config={"temperature": 0.3}
        )
        text = (resp.text or "{}").strip()
        text = re.sub(r"```json\s*|```\s*", "", text)
        parsed = json.loads(text)
    except Exception as e:
        print(
            f"  ⚠ Prompt enhancement failed ({e}), using raw prompt.", file=sys.stderr
        )
        parsed = {}

    result = {
        "prompt": parsed.get("prompt", brief),
        "negative_prompt": parsed.get("negative_prompt", _DEFAULT_NEGATIVES),
        "model": model,
        "resolution": resolution,
        "aspect_ratio": parsed.get("aspect_ratio", "16:9"),
        "lighting_setup": parsed.get("lighting_setup", "unspecified"),
        "camera_angle": parsed.get("camera_angle", "unspecified"),
        "notes": parsed.get("notes", ""),
    }
    return result


# ─── Core Generation ──────────────────────────────────────────────────


def generate_nano(
    prompt: str,
    output_path: Path,
    input_image_path: Optional[str] = None,
    model_name: str = NANO_MODEL,
    resolution: str = "2K",
    aspect_ratio: str = "16:9",
) -> Optional[str]:
    """Image-to-image or text-to-image via Nano Banana chat model."""
    from google.genai import types
    from PIL import Image

    client = get_genai_client()

    contents = []
    if input_image_path:
        contents.append(Image.open(input_image_path))
    contents.append(prompt)

    print(f"  Generating ({model_name}, {resolution}, {aspect_ratio})...")
    try:
        # Build image config
        img_cfg = types.ImageConfig()
        # Try setting aspect_ratio first (preferred for controlling orientation)
        try:
            img_cfg.aspect_ratio = resolve_aspect_ratio(aspect_ratio)
        except Exception:
            pass
        # Try setting image_size (falls back if aspect_ratio not supported)
        try:
            img_cfg.image_size = resolution
        except Exception:
            pass

        resp = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=img_cfg,
            ),
        )
        for part in resp.parts:
            if part.inline_data is not None:
                data = part.inline_data.data
                if isinstance(data, str):
                    data = base64.b64decode(data)
                img = Image.open(BytesIO(data))
                if img.mode == "RGBA":
                    rgb = Image.new("RGB", img.size, (255, 255, 255))
                    rgb.paste(img, mask=img.split()[3])
                    img = rgb
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(str(output_path), "PNG")
                est = estimate_cost(model_name, resolution)
                CONFIG.add_cost(est)
                print(f"  Saved: {output_path}  (cost ~${est:.3f})")
                return str(output_path)
    except Exception as e:
        print(f"  Generation failed: {e}", file=sys.stderr)
    return None


def generate_imagen(
    prompt: str, output_path: Path, aspect_ratio: str = "1:1"
) -> Optional[str]:
    """Pure text-to-image via Imagen 4 using Google GenAI SDK (latest method)."""
    from google.genai import types

    client = get_genai_client()
    est = estimate_cost(IMAGEN_MODEL, "2K")
    print(f"  Generating with imagen-4 ({aspect_ratio}, ~${est:.3f})...")

    try:
        config = types.GenerateImagesConfig(number_of_images=1)
        # Best-effort aspect ratio (SDK may not expose this field in all versions)
        if aspect_ratio != "1:1":
            try:
                config.aspect_ratio = aspect_ratio
            except AttributeError:
                pass
        resp = client.models.generate_images(
            model=IMAGEN_MODEL,
            prompt=prompt,
            config=config,
        )
        if resp.generated_images:
            data = resp.generated_images[0].image.image_bytes
            ensure_dir(output_path.parent)
            output_path.write_bytes(data)
            est = estimate_cost(IMAGEN_MODEL, "2K")
            CONFIG.add_cost(est)
            print(f"  Saved: {output_path}  (cost ~${est:.3f})")
            return str(output_path)
    except Exception as e:
        print(f"  Imagen generation failed: {e}", file=sys.stderr)
    return None


# ─── Composite Pipeline (Real Product + AI Environment) ─────────────────


def remove_background_pil(input_path: str) -> str:
    """Remove background from product photo using PIL edge detection."""
    from PIL import Image, ImageFilter, ImageChops

    img = Image.open(input_path).convert("RGBA")
    gray = img.convert("L")
    # Build mask: pure white bg gets removed, everything else kept
    mask = gray.point(lambda x: 0 if x > 245 else 255, mode="L")
    # Feather edges slightly to avoid hard white halos
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    # Blend original alpha with computed mask
    r, g, b, a = img.split()
    # Only reduce alpha where mask is dark (near-white background was cut)
    combined = ImageChops.multiply(a, mask)
    img.putalpha(combined)
    out = f"/tmp/cs_composite_fg_{hashlib.md5(input_path.encode()).hexdigest()[:8]}.png"
    img.save(out)
    print(f"  🧊 Background removed: {out}")
    return out


def _add_drop_shadow(bg, fg, pos_tuple, blur=8, alpha=80):
    """Add a soft drop shadow beneath the pasted product."""
    from PIL import ImageFilter, Image

    shadow = fg.copy()
    r, g, b, a = shadow.split()
    shadow = Image.merge(
        "RGBA",
        (
            Image.new("L", fg.size, 0),
            Image.new("L", fg.size, 0),
            Image.new("L", fg.size, 0),
            a.point(lambda x: alpha if x > 50 else 0),
        ),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    x, y = pos_tuple
    bg.paste(shadow, (x + 4, y + 4), shadow)
    return bg


def cmd_composite(args):
    """Generate AI environment WITHOUT product, then composite real product on top."""
    from PIL import Image

    product_path = _stage_input(args.product)
    if not product_path:
        print("ERROR: --product required", file=sys.stderr)
        sys.exit(1)
    print("\n── COMPOSITE MODE")
    print("  Step 1: Remove background from product...")
    fg_path = remove_background_pil(product_path)
    fg = Image.open(fg_path)
    fg_w, fg_h = fg.size
    print("  Step 2: Generate clean environment...")
    env_file = (
        _OUT
        / datetime.now().strftime("%Y-%m-%d")
        / "composite"
        / f"env-{now_str()}.png"
    )
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_prompt = (
        f"{args.prompt}\n\n"
        "IMPORTANT: The scene must contain NO products, NO bottles, NO containers, NO labels. "
        "Only empty shelf surfaces and environmental elements."
    )
    result_env = generate_nano(
        env_prompt,
        env_file,
        input_image_path=None,
        model_name=NANO_PRO,
        resolution="2K",
        aspect_ratio=args.aspect_ratio,
    )
    if not result_env:
        print("  ✗ Environment generation failed.", file=sys.stderr)
        sys.exit(1)
    print("  Step 3: Compositing product onto environment...")
    bg = Image.open(env_file).convert("RGBA")
    target_w = int(bg.size[0] * 0.22)
    scale = target_w / fg_w
    target_h = int(fg_h * scale)
    fg = fg.resize((target_w, target_h), Image.LANCZOS)
    x = int(bg.size[0] * 0.38)
    y = int(bg.size[1] * 0.72)
    bg = _add_drop_shadow(bg, fg, (x, y), blur=14, alpha=60)
    bg.paste(fg, (x, y), fg)
    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / "composite")
    fname = _ensure_png(
        args.filename
        or f"{now_str()}-composite-{hashlib.md5(args.prompt[:30].encode()).hexdigest()[:5]}.png"
    )
    outpath = outdir / fname
    bg.convert("RGB").save(str(outpath), "PNG", quality=95)
    print(f"\n✓ {outpath}")


# ─── Export Pipeline (Multi-format crops) ─────────────────────────────


def cmd_export(args):
    """Crop a source image to multiple platform-specific formats."""
    from PIL import Image

    src = Path(args.input)
    if not src.exists():
        print(f"File not found: {src}", file=sys.stderr)
        sys.exit(1)
    presets = {
        "amazon": {"ratio": "1:1", "w": 2000, "h": 2000, "bg": "white"},
        "shopify": {"ratio": "1:1", "w": 2048, "h": 2048, "bg": "white"},
        "meta-feed": {"ratio": "4:5", "w": 1080, "h": 1350, "bg": "transparent"},
        "meta-stories": {"ratio": "9:16", "w": 1080, "h": 1920, "bg": "transparent"},
        "web-hero": {"ratio": "16:9", "w": 1920, "h": 1080, "bg": "transparent"},
        "pinterest": {"ratio": "2:3", "w": 1000, "h": 1500, "bg": "transparent"},
        "print-dpi": {"ratio": "3:2", "dpi": 300, "bg": "white"},
    }
    selected = args.presets.split(",") if args.presets else list(presets.keys())
    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / "exports")
    img = Image.open(str(src)).convert("RGBA")
    print("\n── EXPORT")
    print(f"  Source: {src.name} ({img.size[0]}x{img.size[1]})")
    print(f"  Presets: {', '.join(selected)}\n")
    for key in selected:
        if key not in presets:
            print(f"  ⚠ Unknown preset: {key}", file=sys.stderr)
            continue
        p = presets[key]
        cropped = crop_to_aspect_ratio(img.copy(), p["ratio"])
        if "w" in p and "h" in p:
            cropped = cropped.resize((p["w"], p["h"]), Image.LANCZOS)
        if p.get("bg") == "white":
            base = Image.new("RGB", cropped.size, (255, 255, 255))
            base.paste(cropped, mask=cropped.split()[3])
            cropped = base
        else:
            cropped = cropped.convert("RGB")
        dpi = p.get("dpi", 72)
        fname = f"{src.stem}-{key}.png"
        out = outdir / fname
        cropped.save(str(out), "PNG", dpi=(dpi, dpi))
        print(f"  ✓ {key}: {cropped.size[0]}x{cropped.size[1]} -> {fname}")
    print(f"\n✓ All exports: {outdir}")


# ─── Auto QC / Quality Gate ───────────────────────────────────────────


def cmd_qc(args):
    """Run vision-based quality checks on generated images."""
    from PIL import Image
    from google.genai import types

    img_path = _stage_input(args.input)
    if not img_path:
        print("ERROR: --input required", file=sys.stderr)
        sys.exit(1)
    print("\n── QUALITY CHECK")
    print(f"  File: {Path(img_path).name}")
    img = Image.open(img_path)
    w, h = img.size
    print(f"  Dimensions: {w}x{h}")
    score = 100
    issues = []
    if w < 1000 or h < 1000:
        issues.append(f"Resolution too low ({w}x{h})")
        score -= 20
    if w / h > 2.5 or h / w > 2.5:
        issues.append("Extreme aspect ratio")
        score -= 10
    print("  Running vision QC...")
    client = get_genai_client()
    vision_prompt = (
        "You are a CPG/DTC product photography quality inspector. "
        "Analyze this image and return ONLY JSON: "
        '{"floating_products": bool, "garbled_text": bool, '
        '"detached_shadows": bool, "fake_products": bool, "readable_labels": bool, '
        '"quality_score": 1-10, "issues": ["..."]}'
    )
    try:
        resp = client.models.generate_content(
            model=NANO_MODEL,
            contents=[img, vision_prompt],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        raw = (
            (resp.text or "{}")
            .strip()
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )
        parsed = json.loads(raw)
    except Exception as e:
        parsed = {"error": str(e)}
    print(f"\n{'=' * 50}")
    print(f"  QC SCORE: {parsed.get('quality_score', 'N/A')}/10")
    print(f"{'=' * 50}")
    for key in [
        "floating_products",
        "garbled_text",
        "detached_shadows",
        "fake_products",
        "readable_labels",
    ]:
        val = parsed.get(key, "N/A")
        status = "PASS" if val is False else ("FAIL" if val is True else "?")
        print(f"  {status}: {key.replace('_', ' ').title()}")
    for issue in parsed.get("issues", []):
        print(f"  ⚠ {issue}")
    if issues:
        print(f"  ⚠ Basic: {', '.join(issues)}")
    print(f"  Final: {max(0, score)}/100")


# ─── Analyze ──────────────────────────────────────────────────────────


def cmd_analyze(args):
    from google.genai import types
    from PIL import Image

    staged = _stage_input(args.input)
    if not staged:
        print("  Cannot find input image.", file=sys.stderr)
        sys.exit(1)

    img = Image.open(staged)
    client = get_genai_client()

    prompt = (
        "You are a senior art director analyzing a reference image. "
        "Describe in extreme detail: scene type, shelf fixture, lighting direction and quality, "
        "camera angle, depth of field, whether products float or have real weight/shadows, "
        "how many other products, if labels are readable, overall mood, any physical plausibility flaws. "
        "Be brutally honest. Output ONLY as JSON with these keys: "
        "scene_type, shelf_fixture, lighting_direction, lighting_quality, "
        "camera_angle, depth_of_field, physical_plausibility, label_readability, "
        "surrounding_products_count, overall_mood, critical_flaws."
    )

    print(f"\n── Analyzing: {Path(staged).name}\n")
    try:
        resp = client.models.generate_content(
            model=REASONING_MODEL,
            contents=[img, prompt],
            config=types.GenerateContentConfig(temperature=0.1),
        )
        text = resp.text.strip() if resp.text else "{}"
        # strip markdown fences
        text = re.sub(r"```json\s*|```\s*", "", text)
        parsed = json.loads(text)
        print(json.dumps(parsed, indent=2))
    except Exception as e:
        print(f"  Analysis failed: {e}", file=sys.stderr)


# ─── Quality Check ─────────────────────────────────────────────────────


def cmd_quality(args):
    from PIL import Image, ImageStat

    staged = _stage_input(args.input)
    if not staged:
        print(f"File not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    p = Path(staged)
    img = Image.open(str(p))
    w, h = img.size
    stat = ImageStat.Stat(img.convert("L"))
    brightness = stat.mean[0]
    print(f"  File:   {p.name}")
    print(f"  Size:   {w}x{h}")
    print(f"  Bright: {brightness:.1f}/255")
    print(f"  MB:     {p.stat().st_size / 1024 / 1024:.2f}")


# ─── Review ───────────────────────────────────────────────────────────


def cmd_review(args):
    root = _OUT
    if not root.exists():
        print("No outputs yet.")
        return
    print(f"\n── Outputs in: {root}\n")
    for dd in sorted(root.iterdir(), reverse=True)[:7]:
        if dd.is_dir():
            print(f"  📁 {dd.name}/")
            for sub in sorted(dd.iterdir()):
                if sub.is_dir():
                    imgs = list(sub.glob("*.png"))
                    print(f"    {sub.name}: {len(imgs)} images")


# ─── Direct (one-shot) ───────────────────────────────────────────────


def cmd_direct(args):
    """One-shot generation. Your prompt goes straight to the model. No rewriting."""
    prompt = args.prompt
    # Resolve model: explicit > tier default > fallback
    if args.model:
        model = args.model
    elif args.tier:
        model, _ = _TIER_MAP.get(args.tier, (NANO_PRO, "2K"))
    else:
        model = NANO_MODEL
    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / args.format)
    filename = _ensure_png(
        args.filename
        or f"{now_str()}-{args.format}-{hashlib.md5(prompt[:50].encode()).hexdigest()[:5]}.png"
    )
    outpath = outdir / filename

    # Stage input if it has special characters
    staged_input = _stage_input(args.input_image)

    # Vision pre-analysis on reference image
    if staged_input:
        print("  🔍 Analyzing reference image...")
        analysis = vision_analyze(staged_input)
        if "error" not in analysis:
            print(f"    Subject: {analysis.get('subject_type', 'unknown')}")
            print(f"    Shape:   {analysis.get('physical_shape', 'unknown')}")
            print(
                f"    Size:    {analysis.get('width')}x{analysis.get('height')} ({analysis.get('orientation')})"
            )
            print(f"    View:    {analysis.get('angle_view', 'unknown')}")

    # Smart prompt enhancement
    enhanced_data = None
    if args.smart or args.tier:
        tier = args.tier or "balanced"
        print(f"  [Smart mode: {tier}] Analyzing prompt with reasoning model...")
        enhanced_data = smart_enhance_prompt(
            brief=prompt,
            has_reference_image=bool(staged_input),
            tier=tier,
        )
        prompt = enhanced_data["prompt"]
        model = enhanced_data["model"]
        args.resolution = enhanced_data["resolution"]
        if enhanced_data.get("negative_prompt"):
            prompt += f"\n\nAvoid: {enhanced_data['negative_prompt']}"
        print(f"  Enhanced prompt: {prompt[:120]}...")
        if enhanced_data.get("notes"):
            print(f"  Notes: {enhanced_data['notes'][:120]}")

    print("\n── DIRECT MODE")
    print(f"  Prompt: {prompt[:80]}...")
    print(f"  Model:  {model}")
    print(f"  Input:  {staged_input or '(none)'}")
    print()

    if model.startswith("imagen"):
        result = generate_imagen(prompt, outpath, aspect_ratio="16:9")
    else:
        result = generate_nano(
            prompt,
            outpath,
            input_image_path=staged_input,
            model_name=model,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
        )

    if result:
        print(f"\n✓ {outpath}")
    else:
        print("\n✗ Failed.", file=sys.stderr)
    return result


# ─── Chat (multi-turn iterative) ─────────────────────────────────────


def cmd_chat(args):
    """Interactive multi-turn generation. Each result feeds the next prompt."""

    session_name = args.name or f"session-{now_str()}"
    session_dir = ensure_dir(_OUT / "sessions" / session_name)
    initial_input = _stage_input(args.input_image)
    current_input = initial_input
    turn = 0

    print(f"\n{'=' * 60}")
    print(f"  CHAT SESSION: {session_name}")
    print(f"  Folder: {session_dir}")
    print(f"  Starting image: {current_input or '(none — text-to-image mode)'}")
    print(f"{'=' * 60}")
    print("\n  Type your prompt and press Enter.")
    print("  Commands: 'done' | 'restart' | 'back' | 'save <name>'\n")

    history = []

    while True:
        turn += 1
        try:
            prompt = input(f"\nTurn {turn}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Exiting.")
            break

        if not prompt:
            continue
        if prompt.lower() == "done":
            print(f"\n  Session saved: {session_dir}")
            break
        if prompt.lower() == "restart":
            current_input = initial_input
            turn = 0
            history.clear()
            print("  Restarted from original input.")
            continue
        if prompt.lower().startswith("save "):
            name = prompt[5:].strip()
            if current_input and current_input != initial_input:
                import shutil

                dest = session_dir / f"{name}.png"
                shutil.copy2(current_input, dest)
                print(f"  Saved as {dest.name}")
            else:
                print("  No output to save yet.")
            continue
        if prompt.lower().startswith("back"):
            if history:
                # Roll back one turn
                history.pop()
                if history:
                    current_input = history[-1]["output"]
                    turn = len(history)
                else:
                    current_input = initial_input
                    turn = 0
                print(f"  Rolled back. Now at turn {turn}.")
            else:
                print("  Nothing to go back to.")
            continue

        # Generate
        out_file = session_dir / f"turn-{turn:02d}.png"
        result = generate_nano(
            prompt,
            out_file,
            input_image_path=current_input,
            model_name=args.model,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
        )

        if result:
            history.append(
                {
                    "turn": turn,
                    "prompt": prompt,
                    "input": current_input,
                    "output": result,
                }
            )
            current_input = result  # Next turn uses this as input
            print(f"\n  → {out_file.name}")
            print("  Next prompt will build on this result.")
        else:
            print("  Turn failed. Try again or type 'back'.")
            turn -= 1

    # Save conversation log
    log = session_dir / "conversation.json"
    log.write_text(json.dumps(history, indent=2, default=str))
    print(f"  Log saved: {log}")


# ─── Brainstorm ──────────────────────────────────────────────────────


def cmd_brainstorm(args):
    """Collaborative reasoning: ask clarifying questions, surface 4 directions, then generate."""
    from google import genai

    client = get_genai_client()
    brief = args.prompt
    print(f"\n{'=' * 60}")
    print("  BRAINSTORM SESSION")
    print(f"{'=' * 60}")
    print(f"  Brief: {brief}\n")

    questions = [
        {
            "q": "What platform is this for?",
            "options": [
                "A) Social media feed",
                "B) Website hero/banner",
                "C) Packaging/print",
                "D) Email/landing page",
            ],
        },
        {
            "q": "What mood should dominate?",
            "options": [
                "A) Premium/luxury",
                "B) Fun/energetic",
                "C) Clean/minimal",
                "D) Warm/authentic",
            ],
        },
        {
            "q": "How should the product be shown?",
            "options": [
                "A) Product only, no extras",
                "B) Hands using/holding",
                "C) Full lifestyle scene with person",
                "D) Product + ingredients/context props",
            ],
        },
    ]

    answers = {}
    for i, q in enumerate(questions, 1):
        print(f"Q{i}. {q['q']}")
        for opt in q["options"]:
            print(f"    {opt}")
        try:
            ans = input("  -> ").strip()
            answers[f"q{i}"] = ans
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return None
        print()

    print(f"{'=' * 60}")
    print("  4 DIRECTIONS")
    print(f"{'=' * 60}\n")

    dir_prompt = (
        f"You are a senior art director. Based on brief: '{brief}' and answers: {json.dumps(answers)}, "
        f"create exactly 4 DISTINCT visual directions (A, B, C, D). Each has name, description, "
        f"and a vivid image generation prompt (max 120 words). "
        f"Output ONLY as JSON: "
        f'{{"A":{{"name":"...","description":"...","prompt":"..."}},"B":...,"C":...,"D":...}}'
    )
    directions = {}
    try:
        resp = client.models.generate_content(
            model=REASONING_MODEL,
            contents=dir_prompt,
            config=genai.types.GenerateContentConfig(temperature=0.6),
        )
        text = (
            (resp.text or "").strip().replace("```json", "").replace("```", "").strip()
        )
        directions = json.loads(text)
    except Exception as e:
        print(f"  Reasoning issue: {e}", file=sys.stderr)

    defaults = {
        "A": {
            "name": "Clean Hero",
            "description": "Premium product-forward, minimal distractions.",
            "prompt": brief
            + ", clean studio background, soft diffused overhead lighting, centered product, professional product photography, sharp focus",
        },
        "B": {
            "name": "Lifestyle Moment",
            "description": "Warm, relatable, in-use scene.",
            "prompt": brief
            + ", natural lifestyle setting, warm golden hour side-lighting, shallow depth of field, authentic moment, aspirational",
        },
        "C": {
            "name": "Bold Statement",
            "description": "High contrast, energetic, brand-forward.",
            "prompt": brief
            + ", bold graphic composition, dramatic directional lighting, deep saturated colors, editorial styling, strong shadows",
        },
        "D": {
            "name": "Ingredient Story",
            "description": "Flavor-forward with real ingredients.",
            "prompt": brief
            + ", surrounded by fresh ingredients, clean bright lighting from above, colorful food photography style, detailed",
        },
    }
    for key in ["A", "B", "C", "D"]:
        if key not in directions or not directions.get(key, {}).get("prompt"):
            directions[key] = defaults[key]

    for key in ["A", "B", "C", "D"]:
        d = directions[key]
        print(f"  [{key}] {d.get('name', 'Direction ' + key)}")
        print(f"      {d.get('description', '')}")
        print(f"      -> {d.get('prompt', '')[:120]}...\n")

    try:
        pick = input("  Pick A/B/C/D or type your own prompt adjustments: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        return None

    chosen = directions.get("A", {}).get("prompt", brief)
    if pick.upper() == "A":
        chosen = directions["A"]["prompt"]
    elif pick.upper() == "B":
        chosen = directions["B"]["prompt"]
    elif pick.upper() == "C":
        chosen = directions["C"]["prompt"]
    elif pick.upper() == "D":
        chosen = directions["D"]["prompt"]
    elif pick:
        chosen = pick

    print("\n  Generating...\n")
    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / "brainstorm")
    fname = f"{now_str()}-brainstorm-{hashlib.md5(chosen[:50].encode()).hexdigest()[:5]}.png"
    outpath = outdir / fname

    model = args.model
    staged_input = _stage_input(args.input_image)
    if model.startswith("imagen"):
        result = generate_imagen(chosen, outpath, aspect_ratio="16:9")
    else:
        result = generate_nano(
            chosen,
            outpath,
            input_image_path=staged_input,
            model_name=model,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
        )

    if not result:
        print("  Generation failed.", file=sys.stderr)
        return None

    print(f"✓ {outpath}\n")

    log = outdir / f"{fname}.json"
    log.write_text(
        json.dumps(
            {
                "brief": brief,
                "answers": answers,
                "directions": directions,
                "chosen_prompt": chosen,
                "output": str(outpath),
            },
            indent=2,
        )
    )
    print(f"  Saved session log: {log}")
    return result


# ─── Figma-Aware Generation ─────────────────────────────────────────────


def cmd_figma(args):
    """Generate a new image asset informed by an existing Figma design context."""
    file_key, node_id = parse_figma_url(args.url)
    if not file_key:
        print("ERROR: Could not parse file_key from URL.", file=sys.stderr)
        sys.exit(1)
    print()
    print("── FIGMA-AWARE GENERATION")
    print(f"  File key:  {file_key}")
    print(f"  Node ID:   {node_id or '(file level)'}")

    print()

    if not os.environ.get("FIGMA_ACCESS_TOKEN"):
        print("  ERROR: FIGMA_ACCESS_TOKEN env var is required.")
        print('  Set it with: export FIGMA_ACCESS_TOKEN="figd_..."')
        sys.exit(1)
    print("  Reading design context from Figma...")
    ctx = fetch_figma_context(file_key, node_id)
    if "error" in ctx and not ctx.get("fills"):
        print(f"  ⚠ Figma read issue: {ctx['error']}", file=sys.stderr)
    else:
        if ctx.get("fills"):
            print(f"  🎨 Colors: {', '.join(ctx['fills'][:5])}")
        if ctx.get("fonts"):
            print(f"  📝 Fonts: {', '.join(ctx['fonts'][:3])}")
        if ctx.get("layout"):
            print(f"  📐 Layout: {', '.join(set(ctx['layout']))}")

    enhanced = enhance_prompt_with_figma(args.prompt, ctx)
    print()
    print(f"  Prompt:  {args.prompt[:80]}...")
    print("  Enhancing prompt with Figma context...")
    print(f"  Final prompt: {enhanced[:120]}...")
    print()

    # Smart enhancement with Figma context
    if args.model:
        model = args.model
    else:
        model, args.resolution = _TIER_MAP.get(
            args.tier or "balanced", (NANO_PRO, "2K")
        )
    if args.smart or args.tier:
        tier = args.tier or "balanced"
        print(f"  [Smart mode: {tier}] Analyzing with reasoning model...")
        enhanced_data = smart_enhance_prompt(
            brief=args.prompt,
            has_reference_image=bool(args.input_image),
            figma_context=ctx,
            tier=tier,
        )
        enhanced = enhanced_data["prompt"]
        model = enhanced_data["model"]
        args.resolution = enhanced_data["resolution"]
        if enhanced_data.get("negative_prompt"):
            enhanced += f"\n\nAvoid: {enhanced_data['negative_prompt']}"
        print(f"  Lighting: {enhanced_data.get('lighting_setup', 'unspecified')}")
        print(f"  Camera:   {enhanced_data.get('camera_angle', 'unspecified')}")
        if enhanced_data.get("notes"):
            print(f"  Notes:    {enhanced_data['notes'][:100]}")
    else:
        enhanced = enhance_prompt_with_figma(args.prompt, ctx)

    print(f"  Prompt:   {args.prompt[:80]}...")
    print(f"  Final:    {enhanced[:120]}...")
    print()

    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / "figma")
    fname = _ensure_png(
        args.filename
        or f"{now_str()}-figma-{hashlib.md5(enhanced[:50].encode()).hexdigest()[:5]}.png"
    )
    outpath = outdir / fname

    staged_input = _stage_input(args.input_image)
    print(f"  Input:    {staged_input or '(none)'}")
    print(f"  Model:    {model}")
    print(f"  Res:      {args.resolution}")
    print(f"  Aspect:   {args.aspect_ratio}")
    print()

    if model.startswith("imagen"):
        result = generate_imagen(enhanced, outpath, aspect_ratio=args.aspect_ratio)
    else:
        result = generate_nano(
            enhanced,
            outpath,
            input_image_path=staged_input,
            model_name=model,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
        )

    if not result:
        print()
        print("✗ Generation failed.", file=sys.stderr)
        sys.exit(1)

    print(f"  Saved: {outpath}")

    if node_id:
        comment_msg = f"[AI Generated] Asset based on design context.\nPrompt: {args.prompt[:80]}...\nSaved locally at: {outpath}"
        print(f"  Posting comment to Figma node {node_id}...")
        c = post_figma_comment(file_key, node_id, comment_msg)
        if "error" in c:
            print(f"  ⚠ Comment failed: {c['error']}", file=sys.stderr)
        else:
            print(f"  💬 Comment posted at: {c.get('id', 'ok')}")
    else:
        print("  Skipping round-trip (no node_id in URL).")

    log = outdir / f"{fname}.json"
    log_payload = {
        "figma_url": args.url,
        "file_key": file_key,
        "node_id": node_id,
        "original_prompt": args.prompt,
        "enhanced_prompt": enhanced,
        "design_context": ctx,
        "output": str(outpath),
        "model": model,
        "resolution": args.resolution,
    }
    if "enhanced_data" in dir() and enhanced_data:
        log_payload["prompt_plan"] = enhanced_data
    log.write_text(json.dumps(log_payload, indent=2, default=str))
    print(f"  Log saved: {log}")


# ─── Variations (Midjourney-style 4-panel) ───────────────────────────


def cmd_variations(args):
    """Generate N variations of the same brief for user to pick from."""
    prompt = args.prompt
    count = max(1, min(8, args.variations))

    # Resolve model
    if args.model:
        model = args.model
    elif args.tier:
        model, args.resolution = _TIER_MAP.get(args.tier, (NANO_PRO, "2K"))
    else:
        model = NANO_PRO if count > 1 else NANO_MODEL

    # Stage input
    staged_input = _stage_input(args.input_image)

    outdir = ensure_dir(_OUT / datetime.now().strftime("%Y-%m-%d") / args.format)
    session_dir = ensure_dir(outdir / f"vars-{now_str()}")

    print(f"\n── VARIATIONS ({count}x)")
    print(f"  Prompt: {prompt[:80]}...")
    print(f"  Model:  {model}")
    print(f"  Input:  {staged_input or '(none)'}")
    print(f"  Folder: {session_dir}")
    print()

    # Build prompts — same brief with slight creative variance per variation
    prompts = []
    for i in range(count):
        angle = [
            "eye-level",
            "slightly low angle hero shot",
            "three-quarter view",
            "straight-on",
        ][i % 4]
        light = [
            "warm 3200K overhead",
            "neutral 5600K soft-diffused",
            "crisp directional rim light",
            "even flat ambient",
        ][i % 4]
        dof = [
            "shallow depth of field with creamy bokeh",
            "deep depth of field",
            "selective focus on hero product",
            "sharp throughout with slight falloff",
        ][i % 4]
        # Shelf-specific: force flat level shelves
        shelf_note = " The shelf surface is perfectly flat and level. Products sit firmly with flat bases touching the shelf. No tilting, no floating, no falling."
        prompts.append(
            f"{prompt}\n\nVariation {i + 1}: {angle} composition. {light} lighting. {dof}. Professional product photography.{shelf_note}"
        )

    generated = []
    for i in range(count):
        vnum = i + 1
        vname = f"v{vnum:02d}.png"
        vpath = session_dir / vname
        print(f"  Generating variation {vnum}/{count}...")

        if model.startswith("imagen"):
            result = generate_imagen(prompts[i], vpath)
        else:
            result = generate_nano(
                prompts[i],
                vpath,
                input_image_path=staged_input,
                model_name=model,
                resolution=args.resolution,
                aspect_ratio=args.aspect_ratio,
            )

        if result:
            print(f"    ✓ {vname}")
            generated.append(str(vpath))
        else:
            print(f"    ✗ {vname} failed", file=sys.stderr)

    print(f"\n✓ All done: {session_dir}")
    print("  Pick your favorite with:")
    print(
        f'    bash launch.sh refine --session {session_dir} --pick v1 --changes "..."'
    )

    # Save manifest
    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "count": count,
                "model": model,
                "resolution": args.resolution,
                "original_prompt": prompt,
                "prompts": prompts,
                "files": generated,
                "aspect_ratio": args.aspect_ratio,
            },
            indent=2,
        )
    )
    print(f"  Manifest: {manifest_path}")
    return session_dir, generated


# ─── Refine (pick + iterate) ────────────────────────────────────────────


def cmd_refine(args):
    """Pick a variation and refine it with specific instruction."""
    # Resolve session path
    session_dir = Path(args.session)
    if not session_dir.exists():
        # Search by name
        for fmt_dir in sorted(_OUT.glob("*/vars-*"), reverse=True):
            if fmt_dir.name.endswith(session_dir.name) or session_dir.name in str(
                fmt_dir
            ):
                session_dir = fmt_dir
                break

    if not session_dir.exists():
        print(f"ERROR: Session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads((session_dir / "manifest.json").read_text())
    files = manifest["files"]
    model = manifest["model"]

    # Resolve which variation to refine
    pick_clean = args.pick.replace("v", "").replace(".png", "").strip()
    pick_num = int(pick_clean)
    idx = pick_num - 1
    if idx < 0 or idx >= len(files):
        print(f"ERROR: Pick must be between v1 and v{len(files)}", file=sys.stderr)
        sys.exit(1)

    base_image = files[idx]
    changes = args.changes or ""
    ref_prompt = manifest["prompts"][idx]
    final_prompt = f"Refinement based on version {args.pick}:\n{changes}\n\nOriginal prompt:\n{ref_prompt}"

    # Refinement output
    outdir = ensure_dir(session_dir)
    rname = f"r{pick_num:02d}-{now_str()}.png"
    rout = outdir / rname

    print("\n── REFINE")
    print(f"  Session:  {session_dir}")
    print(f"  Pick:     {args.pick}")
    print(f"  Changes:  {changes[:80]}...")
    print(f"  Model:    {model}")
    print()

    if model.startswith("imagen"):
        result = generate_imagen(final_prompt, rout)
    else:
        result = generate_nano(
            final_prompt,
            rout,
            input_image_path=base_image,
            model_name=model,
            resolution=manifest["resolution"],
            aspect_ratio=manifest.get("aspect_ratio", "16:9"),
        )

    if result:
        print(f"\n✓ {rout}")
    else:
        print("\n✗ Refinement failed.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="creative-studio", description="Iterative AI image generation workflow."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # direct
    p = sub.add_parser(
        "direct", help="One-shot. Your exact prompt goes straight to the model."
    )
    p.add_argument(
        "--prompt", "-p", required=True, help="Exact prompt text — no rewriting."
    )
    p.add_argument(
        "--model",
        "-m",
        default=None,
        help="Override model. Defaults to tier selection.",
    )
    p.add_argument("--format", "-f", default="web", help="Folder name for output")
    p.add_argument(
        "--input-image", "-i", default=None, help="Reference image for image-to-image"
    )
    p.add_argument("--resolution", "-r", default="2K", help="1K, 2K, 4K (Nano only)")
    p.add_argument("--filename", default=None, help="Custom output filename")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
        help="Output aspect ratio (default 16:9)",
    )
    p.add_argument(
        "--tier",
        default=None,
        choices=["fast", "balanced", "quality", "ultra"],
        help="Quality tier: fast=Flash-1K, balanced=Flash-2K, quality=Pro-2K, ultra=Pro-4K",
    )
    p.add_argument(
        "--smart",
        action="store_true",
        help="Use reasoning model to enhance the prompt with photographic direction.",
    )

    # chat
    p = sub.add_parser(
        "chat", help="Multi-turn conversation. Each output feeds the next prompt."
    )
    p.add_argument("--name", "-n", default=None, help="Session name (creates a folder)")
    p.add_argument(
        "--input-image",
        "-i",
        default=None,
        help="Starting image for turn 1 (optional, can start text-to-image)",
    )
    p.add_argument("--model", "-m", default=NANO_MODEL)
    p.add_argument("--resolution", "-r", default="2K")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
    )

    # analyze
    p = sub.add_parser("analyze", help="Analyze a reference image with vision model.")
    p.add_argument("--input", required=True, help="Image path")

    # quality
    p = sub.add_parser("quality", help="Quick quality check: size, brightness.")
    p.add_argument("--input", required=True)

    # review
    sub.add_parser("review", help="Browse all output folders.")

    # figma
    p = sub.add_parser(
        "figma",
        help="Generate from a Figma design. Reads layout/colors and creates matching asset.",
    )
    p.add_argument("--url", "-u", required=True, help="Figma URL (file or design link)")
    p.add_argument(
        "--prompt", "-p", required=True, help="What to generate (your prompt)"
    )
    p.add_argument(
        "--input-image", "-i", default=None, help="Reference image (optional)"
    )
    p.add_argument(
        "--model", "-m", default=None, help="Override model (default from tier)"
    )
    p.add_argument("--resolution", "-r", default="2K", help="1K, 2K, 4K (Nano only)")
    p.add_argument("--filename", default=None, help="Custom output filename")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
        help="Output aspect ratio",
    )
    p.add_argument(
        "--tier",
        default="balanced",
        choices=["fast", "balanced", "quality", "ultra"],
        help="Quality tier: fast=Flash 1K, balanced=Flash 2K, quality=Pro 2K, ultra=Pro 4K",
    )
    p.add_argument(
        "--smart",
        action="store_true",
        help="Use reasoning model to enhance prompt with photographic direction.",
    )

    # brainstorm
    p = sub.add_parser(
        "brainstorm",
        help="Collaborative reasoning: ask questions, surface directions, then generate.",
    )
    p.add_argument("--prompt", "-p", required=True, help="Initial creative brief")
    p.add_argument("--model", "-m", default=NANO_MODEL, help="Model ID")
    p.add_argument(
        "--input-image", "-i", default=None, help="Reference image (optional)"
    )
    p.add_argument("--resolution", "-r", default="2K", help="1K / 2K / 4K")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
    )

    # variations
    p = sub.add_parser(
        "variations",
        help="Generate N variations for pick-and-refine workflow (like Midjourney).",
    )
    p.add_argument("--prompt", "-p", required=True, help="Your prompt / brief")
    p.add_argument(
        "--input-image", "-i", default=None, help="Reference image (optional)"
    )
    p.add_argument("--model", "-m", default=None, help="Override model")
    p.add_argument(
        "--variations",
        "-v",
        type=int,
        default=4,
        help="Number of variations (1-8, default 4)",
    )
    p.add_argument("--format", "-f", default="web", help="Folder name for output")
    p.add_argument("--resolution", "-r", default="2K", help="1K, 2K, 4K")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
        help="Output aspect ratio",
    )
    p.add_argument(
        "--tier",
        default=None,
        choices=["fast", "balanced", "quality", "ultra"],
        help="Quality tier",
    )

    # refine
    p = sub.add_parser("refine", help="Pick a variation and refine it.")
    p.add_argument(
        "--session",
        "-s",
        required=True,
        help="Session folder name (e.g. vars-123456 or full path)",
    )
    p.add_argument(
        "--pick",
        "-p",
        required=True,
        help="Which variation to refine (e.g. v1, v2, v3, v4)",
    )
    p.add_argument("--changes", "-c", default="", help="What to change / refine")
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=[
            "1:1",
            "16:9",
            "16:10",
            "4:3",
            "3:2",
            "2:3",
            "21:9",
            "4:5",
            "5:4",
            "9:16",
            "1:4",
            "4:1",
        ],
    )

    # composite
    p = sub.add_parser(
        "composite",
        help="Generate AI background then composite real product on top. No hallucinated products.",
    )
    p.add_argument(
        "--prompt",
        "-p",
        required=True,
        help="Scene description — must NOT include product, only environment",
    )
    p.add_argument(
        "--product",
        "-i",
        required=True,
        help="Product photo (AI will NOT generate product, only environment)",
    )
    p.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=["1:1", "16:9", "16:10", "4:3", "3:2", "2:3", "9:16", "4:5"],
    )
    p.add_argument("--filename", default=None, help="Custom output filename")
    p.add_argument(
        "--tier", default="quality", choices=["fast", "balanced", "quality", "ultra"]
    )

    # export
    p = sub.add_parser(
        "export", help="Crop source image into multiple platform-specific formats."
    )
    p.add_argument("--input", required=True, help="Source image path")
    p.add_argument(
        "--presets",
        default=None,
        help="Comma-separated: amazon,shopify,meta-feed,meta-stories,web-hero,pinterest,print-dpi (default: all)",
    )

    # qc
    p = sub.add_parser("qc", help="Run automatic quality checks on generated images.")
    p.add_argument("--input", "-i", required=True, help="Image to inspect")

    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: Set GEMINI_API_KEY env var.", file=sys.stderr)
        sys.exit(1)

    if args.cmd == "direct":
        result = cmd_direct(args)
        if not result:
            sys.exit(1)
    elif args.cmd == "figma":
        cmd_figma(args)
    elif args.cmd == "brainstorm":
        cmd_brainstorm(args)
    elif args.cmd == "chat":
        cmd_chat(args)
    elif args.cmd == "analyze":
        cmd_analyze(args)
    elif args.cmd == "quality":
        cmd_quality(args)
    elif args.cmd == "review":
        cmd_review(args)
    elif args.cmd == "variations":
        cmd_variations(args)
    elif args.cmd == "refine":
        cmd_refine(args)
    elif args.cmd == "composite":
        cmd_composite(args)
    elif args.cmd == "export":
        cmd_export(args)
    elif args.cmd == "qc":
        cmd_qc(args)


if __name__ == "__main__":
    main()
