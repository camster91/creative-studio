"""Refinement and multi-variation image-generation workflows."""

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable


VARIATION_SUFFIXES = [
    " eye-level composition. warm 3200K overhead lighting. shallow depth of field with creamy bokeh. Professional product photography.",
    " slightly low angle hero shot. neutral 5600K soft-diffused lighting. deep depth of field. Professional product photography.",
    " three-quarter view composition. crisp directional rim light. selective focus on hero product. Professional product photography.",
    " straight-on composition. even flat ambient lighting. sharp throughout with slight falloff. Professional product photography.",
    " eye-level composition. neutral 5600K soft-diffused lighting. shallow depth of field with creamy bokeh. Professional product photography.",
    " slightly low angle hero shot. warm 3200K overhead lighting. deep depth of field. Professional product photography.",
    " three-quarter view composition. even flat ambient lighting. selective focus on hero product. Professional product photography.",
    " straight-on composition. crisp directional rim light. sharp throughout with slight falloff. Professional product photography.",
]


def refine(
    image_path: str,
    changes: str,
    api_key: str,
    tier: str,
    *,
    output_dir: Path,
    launch_script: Path,
    run: Callable,
    record_cost: Callable,
    to_image_url: Callable,
) -> list[dict]:
    destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "refine"
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"refine-{int(time.time())}.png"
    arguments = [
        "bash", str(launch_script), "direct", "--prompt",
        f"Based on this reference image, make these changes: {changes}",
        "--input-image", image_path, "--tier", tier, "--filename", filename,
    ]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    try:
        run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
        output_path = destination / filename
        if not output_path.exists():
            files = sorted(
                (output_dir / datetime.now().strftime("%Y-%m-%d")).rglob("*.png"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if files:
                output_path = files[0]
        if output_path.exists():
            model = "gemini-3.1-flash-image-preview" if tier in ("fast", "balanced") else "gemini-3-pro-image-preview"
            return [{"path": str(output_path), "url": to_image_url(str(output_path)), "name": output_path.name, "cost": record_cost(model), "model": model}]
    except subprocess.CalledProcessError:
        return [{"error": "Refine provider request failed", "error_code": "provider_failed"}]
    except Exception:
        return [{"error": "Refine service unavailable", "error_code": "service_unavailable"}]
    return []


def variations(
    prompt: str,
    api_key: str,
    count: int,
    tier: str,
    aspect: str,
    *,
    input_image: str | None,
    output_dir: Path,
    script_path: str,
    python_executable: str,
    tier_models: dict,
    run: Callable,
    record_cost: Callable,
    to_image_url: Callable,
) -> tuple[list[dict], str]:
    today = datetime.now().strftime("%Y-%m-%d")
    session_key = f"vars-{int(time.time())}-{uuid.uuid4().hex[:4]}"
    session_dir = output_dir / today / "variations" / session_key
    session_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for index in range(count):
        name = f"v{index + 1:02d}.png"
        path = session_dir / name
        variation_prompt = (
            prompt + "\n\n" + VARIATION_SUFFIXES[index % len(VARIATION_SUFFIXES)]
            + " The shelf surface is perfectly flat and level. Products sit firmly with flat bases touching the shelf. No tilting, no floating, no falling."
        )
        model, resolution = tier_models.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
        arguments = [
            python_executable, script_path, "direct", "--prompt", variation_prompt,
            "--tier", tier, "--aspect-ratio", aspect, "--resolution", resolution,
            "--filename", str(path),
        ]
        if input_image:
            arguments += ["--input-image", input_image]
        environment = os.environ.copy()
        environment["GEMINI_API_KEY"] = api_key
        environment["CREATIVE_OUTPUT_DIR"] = str(output_dir)
        try:
            run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
            if path.exists():
                images.append({"path": str(path), "url": to_image_url(str(path)), "name": name, "cost": record_cost(model, resolution), "model": model, "variation_index": index + 1})
        except subprocess.CalledProcessError:
            continue
    manifest = {
        "count": len(images), "model": tier_models.get(tier, ("gemini-3-pro-image-preview", "2K"))[0],
        "resolution": tier_models.get(tier, ("gemini-3-pro-image-preview", "2K"))[1],
        "original_prompt": prompt, "tier": tier, "aspect_ratio": aspect,
        "files": [image["path"] for image in images],
        "prompts": [prompt + VARIATION_SUFFIXES[index % len(VARIATION_SUFFIXES)] for index in range(len(images))],
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return images, session_key


def refine_variation(
    session_key: str,
    pick_index: int,
    changes: str,
    tier: str,
    api_key: str,
    *,
    output_dir: Path,
    script_path: str,
    python_executable: str,
    tier_models: dict,
    run: Callable,
    record_cost: Callable,
    to_image_url: Callable,
) -> list[dict]:
    session_dir = output_dir / datetime.now().strftime("%Y-%m-%d") / "variations" / session_key
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists() and output_dir.exists():
        for date_dir in sorted(output_dir.iterdir(), reverse=True):
            candidate = date_dir / "variations" / session_key
            if date_dir.is_dir() and candidate.exists():
                session_dir, manifest_path = candidate, candidate / "manifest.json"
                break
    if not manifest_path.exists():
        return [{"error": f"Session not found: {session_key}"}]
    manifest = json.loads(manifest_path.read_text())
    files, index = manifest.get("files", []), pick_index - 1
    if index < 0 or index >= len(files):
        return [{"error": f"Pick must be between 1 and {len(files)}"}]
    prompts = manifest.get("prompts") or [manifest.get("original_prompt", "")]
    reference_prompt = prompts[index] if index < len(prompts) else manifest.get("original_prompt", "")
    final_prompt = f"Refinement based on version v{pick_index}:\n{changes}\n\nOriginal prompt:\n{reference_prompt}"
    filename = f"r{pick_index:02d}-{int(time.time())}.png"
    output_path = session_dir / filename
    model, resolution = tier_models.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    arguments = [
        python_executable, script_path, "direct", "--prompt", final_prompt,
        "--input-image", files[index], "--tier", tier, "--aspect-ratio",
        manifest.get("aspect_ratio", "16:9"), "--resolution", resolution,
        "--filename", str(output_path),
    ]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    environment["CREATIVE_OUTPUT_DIR"] = str(output_dir)
    try:
        run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
        if output_path.exists():
            return [{"path": str(output_path), "url": to_image_url(str(output_path)), "name": output_path.name, "cost": record_cost(model, resolution), "model": model}]
    except subprocess.CalledProcessError:
        return [{"error": "Refine provider request failed", "error_code": "provider_failed"}]
    except Exception:
        return [{"error": "Refine service unavailable", "error_code": "service_unavailable"}]
    return [{"error": "Refine produced no output"}]
