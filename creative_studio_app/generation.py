"""CLI-backed direct generation and product compositing orchestration."""

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .provider_errors import error_code


def generate(
    prompt: str,
    mode: str,
    api_key: str,
    tier: str,
    aspect: str,
    smart: bool,
    *,
    input_image: str | None,
    variations: int,
    output_dir: Path,
    script_path: str,
    python_executable: str,
    tier_models: dict,
    run: Callable,
    record_cost: Callable,
    to_image_url: Callable,
) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    (output_dir / today / mode).mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    environment["CREATIVE_OUTPUT_DIR"] = str(output_dir)
    images = []
    count = max(1, min(8, variations))
    tier_info = tier_models.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    resolution = tier_info[1] if isinstance(tier_info, tuple) else "1K"
    for index in range(count):
        arguments = [
            python_executable, script_path, "direct", "--prompt", prompt,
            "--tier", tier, "--aspect-ratio", aspect, "--resolution", resolution,
        ]
        if smart:
            arguments.append("--smart")
        if input_image:
            arguments += ["--input-image", input_image]
        try:
            run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if index == 0:
                return [{"error": "Generation provider request failed", "error_code": error_code(error)}]
            break
        except Exception:
            if index == 0:
                return [{"error": "Generation service unavailable", "error_code": "service_unavailable"}]
            break
        files = sorted(
            (output_dir / today).rglob("*.png"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        recent = [path for path in files if time.time() - path.stat().st_mtime < 180]
        if recent:
            image = recent[0]
            model, used_resolution = tier_models.get(
                tier, ("gemini-3-pro-image-preview", "2K")
            )
            images.append({
                "path": str(image), "url": to_image_url(str(image)), "name": image.name,
                "cost": record_cost(model, used_resolution), "model": model, "ratio": aspect,
            })
    return images or [{"error": "Generation produced no output"}]


def composite(
    prompt: str,
    product_path: str,
    api_key: str,
    aspect: str,
    tier: str,
    *,
    name_suffix: str,
    output_dir: Path,
    script_path: str,
    python_executable: str,
    run: Callable,
    record_cost: Callable,
    to_image_url: Callable,
) -> list[dict]:
    destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "composite"
    destination.mkdir(parents=True, exist_ok=True)
    suffix = name_suffix or uuid.uuid4().hex[:6]
    filename = f"composite-{int(time.time())}-{suffix}.png"
    output_path = destination / filename
    arguments = [
        python_executable, script_path, "composite", "--prompt", prompt,
        "--product", product_path, "--aspect-ratio", aspect, "--tier", tier,
        "--filename", filename,
    ]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    try:
        run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
        if output_path.exists():
            model = "gemini-3-pro-image-preview"
            result = {
                "path": str(output_path), "url": to_image_url(str(output_path)),
                "name": filename, "cost": record_cost(model), "model": model,
                "ratio": aspect,
            }
            manifest_path = output_path.with_suffix(".composite.json")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("schema_version") == 1:
                    result["composite_manifest"] = manifest
            except (OSError, ValueError, TypeError):
                pass
            return [result]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return [{"error": "Composite provider request failed", "error_code": error_code(error)}]
    except Exception:
        return [{"error": "Composite service unavailable", "error_code": "service_unavailable"}]
    return [{"error": "Composite produced no output"}]
