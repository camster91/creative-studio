"""Export orchestration and quality-control result parsing."""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable


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
    arguments = ["bash", str(launch_script), "export", "--input", source_path, "--presets", presets]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    environment["CREATIVE_OUTPUT_DIR"] = str(output_dir)
    try:
        run(arguments, capture_output=True, text=True, timeout=120, env=environment, check=True)
        destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "exports"
        return [
            {"path": str(path), "url": to_image_url(str(path)), "name": path.name, "cost": 0.0, "model": "PIL"}
            for path in destination.glob("*.png")
        ]
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
