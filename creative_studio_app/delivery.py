"""Export orchestration and quality-control result parsing."""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable


EXPORT_PRESETS = {
    "amazon": {"ratio": "1:1", "size": (2000, 2000), "background": "white", "dpi": 72},
    "shopify": {"ratio": "1:1", "size": (2048, 2048), "background": "white", "dpi": 72},
    "meta-feed": {"ratio": "4:5", "size": (1080, 1350), "background": "transparent", "dpi": 72},
    "meta-stories": {"ratio": "9:16", "size": (1080, 1920), "background": "transparent", "dpi": 72},
    "web-hero": {"ratio": "16:9", "size": (1920, 1080), "background": "transparent", "dpi": 72},
    "pinterest": {"ratio": "2:3", "size": (1000, 1500), "background": "transparent", "dpi": 72},
    "print-dpi": {"ratio": "3:2", "size": None, "background": "white", "dpi": 300},
}


def _crop_to_ratio(image, ratio: str):
    width_ratio, height_ratio = (int(value) for value in ratio.split(":"))
    target = width_ratio / height_ratio
    current = image.width / image.height
    if current > target:
        width = round(image.height * target)
        left = (image.width - width) // 2
        return image.crop((left, 0, left + width, image.height))
    height = round(image.width / target)
    top = (image.height - height) // 2
    return image.crop((0, top, image.width, top + height))


def export_presets(source_path: str, presets: str | None, output_dir: Path) -> list[Path]:
    """Create exactly the requested PNG presets and return only this run's files."""
    from PIL import Image

    selected = [item.strip() for item in presets.split(",") if item.strip()] if presets else list(EXPORT_PRESETS)
    unknown = [item for item in selected if item not in EXPORT_PRESETS]
    if unknown:
        raise ValueError(f"Unknown export preset(s): {', '.join(unknown)}")
    destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "exports"
    destination.mkdir(parents=True, exist_ok=True)
    source = Path(source_path)
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
        written = []
        for key in selected:
            preset = EXPORT_PRESETS[key]
            result = _crop_to_ratio(image.copy(), preset["ratio"])
            if preset["size"]:
                result = result.resize(preset["size"], Image.Resampling.LANCZOS)
            if preset["background"] == "white":
                flattened = Image.new("RGB", result.size, "white")
                flattened.paste(result, mask=result.getchannel("A"))
                result = flattened
            output = destination / f"{source.stem}-{key}.png"
            result.save(output, "PNG", dpi=(preset["dpi"], preset["dpi"]))
            written.append(output)
    return written


def export_images(
    source_path: str,
    presets: str,
    api_key: str,
    *,
    launch_script: Path,
    output_dir: Path,
    run: Callable,
    to_image_url: Callable,
) -> list[dict]:
    try:
        paths = export_presets(source_path, presets, output_dir)
        return [
            {"path": str(path), "url": to_image_url(str(path)), "name": path.name, "cost": 0.0, "model": "PIL"}
            for path in paths
        ]
    except ValueError as error:
        return [{"error": str(error), "kind": "validation"}]
    except subprocess.CalledProcessError as error:
        detail = error.stderr[:500] if error.stderr else error
        return [{"error": f"Export failed: {detail}"}]
    except Exception as error:
        return [{"error": str(error)}]


def parse_qc_output(output: str) -> dict:
    score_match = re.search(r"QC SCORE:\s*(\d)/10", output)
    issues = [line.replace("⚠", "").strip() for line in output.splitlines() if "⚠" in line]
    return {
        "quality_score": int(score_match.group(1)) if score_match else 5,
        "floating_products": "FAIL" in output and "Floating" in output,
        "garbled_text": "FAIL" in output and "Garbled" in output,
        "detached_shadows": "FAIL" in output and "Shadows" in output,
        "fake_products": "FAIL" in output and "Fake" in output,
        "readable_labels": "PASS" in output and "Labels" in output,
        "issues": issues[:5],
    }


def run_qc(image_path: str, api_key: str, *, launch_script: Path, run: Callable) -> dict:
    arguments = ["bash", str(launch_script), "qc", "--input", image_path]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    try:
        result = run(arguments, capture_output=True, text=True, timeout=120, env=environment, check=True)
        return parse_qc_output(result.stdout + result.stderr)
    except subprocess.CalledProcessError as error:
        detail = error.stderr[:500] if error.stderr else error
        return {"quality_score": 0, "error": f"QC failed: {detail}", "issues": []}
    except Exception as error:
        return {"quality_score": 0, "error": str(error), "issues": []}
