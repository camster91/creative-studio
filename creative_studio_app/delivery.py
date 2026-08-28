"""Export orchestration and quality-control result parsing."""

import os
import re
import subprocess
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from .provider_errors import error_code


EXPORT_PRESETS = {
    "amazon": {"ratio": "1:1", "size": (2000, 2000), "background": "white", "dpi": 72},
    "shopify": {"ratio": "1:1", "size": (2048, 2048), "background": "white", "dpi": 72},
    "meta-feed": {"ratio": "4:5", "size": (1080, 1350), "background": "transparent", "dpi": 72},
    "meta-stories": {"ratio": "9:16", "size": (1080, 1920), "background": "transparent", "dpi": 72},
    "web-hero": {"ratio": "16:9", "size": (1920, 1080), "background": "transparent", "dpi": 72},
    "pinterest": {"ratio": "2:3", "size": (1000, 1500), "background": "transparent", "dpi": 72},
    "print-dpi": {"ratio": "3:2", "size": None, "background": "white", "dpi": 300},
}

QC_RUBRIC_VERSION = "cpg-photo-v1"
QC_CRITERIA = {
    "physical_grounding": ("floating_products", False),
    "text_integrity": ("garbled_text", False),
    "shadow_attachment": ("detached_shadows", False),
    "product_authenticity": ("fake_products", False),
    "label_readability": ("readable_labels", True),
}


def normalize_qc_assessment(payload: dict, *, model: str) -> dict:
    """Normalize provider output into a versioned advisory-only contract."""
    criteria_payload = payload.get("criteria") if isinstance(payload.get("criteria"), dict) else {}
    criteria = {}
    known = 0
    confidence_values = []
    for name, (legacy_key, passing_value) in QC_CRITERIA.items():
        supplied = criteria_payload.get(name, {})
        if isinstance(supplied, dict) and supplied.get("status") in {"pass", "fail", "unknown"}:
            status = supplied["status"]
            evidence = str(supplied.get("evidence") or "")[:500]
            confidence = supplied.get("confidence") if supplied.get("confidence") in {"low", "medium", "high"} else "low"
        elif isinstance(payload.get(legacy_key), bool):
            status = "pass" if payload[legacy_key] is passing_value else "fail"
            evidence = "Legacy provider boolean; no criterion-level evidence supplied."
            confidence = "low"
        else:
            status, evidence, confidence = "unknown", "Provider did not assess this criterion.", "low"
        if status != "unknown":
            known += 1
        confidence_values.append(confidence)
        criteria[name] = {
            "status": status,
            "passed": True if status == "pass" else False if status == "fail" else None,
            "evidence": evidence,
            "confidence": confidence,
        }
    raw_score = payload.get("quality_score")
    score = raw_score if isinstance(raw_score, int) and 1 <= raw_score <= 10 else None
    overall_confidence = "low"
    if known == len(QC_CRITERIA) and all(value == "high" for value in confidence_values):
        overall_confidence = "high"
    elif known >= 3 and any(value in {"medium", "high"} for value in confidence_values):
        overall_confidence = "medium"
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    return {
        "advisory": True,
        "rubric_version": QC_RUBRIC_VERSION,
        "model": model,
        "quality_score": score,
        "confidence": overall_confidence,
        "criteria": criteria,
        "issues": [str(issue)[:500] for issue in issues[:10]],
        "limitations": [
            "AI visual review can miss defects or flag acceptable creative choices.",
            "The score is not proof of marketplace compliance, product authenticity, or readable legal copy.",
            "Export is never blocked solely by this assessment; a human reviewer may override it.",
        ],
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
    except subprocess.CalledProcessError:
        return [{"error": "Export service failed", "error_code": "service_failed"}]
    except Exception:
        return [{"error": "Export service unavailable", "error_code": "service_unavailable"}]


def parse_qc_output(output: str) -> dict:
    marker = re.search(r"^QC_JSON:\s*(\{.*\})$", output, re.MULTILINE)
    if marker:
        try:
            parsed = json.loads(marker.group(1))
            if parsed.get("rubric_version") == QC_RUBRIC_VERSION:
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    score_match = re.search(r"QC SCORE:\s*(\d)/10", output)
    issues = [line.replace("⚠", "").strip() for line in output.splitlines() if "⚠" in line]
    legacy = {
        "quality_score": int(score_match.group(1)) if score_match else None,
        "floating_products": "FAIL" in output and "Floating" in output,
        "garbled_text": "FAIL" in output and "Garbled" in output,
        "detached_shadows": "FAIL" in output and "Shadows" in output,
        "fake_products": "FAIL" in output and "Fake" in output,
        "readable_labels": "PASS" in output and "Labels" in output,
        "issues": issues[:5],
    }
    return normalize_qc_assessment(legacy, model="legacy-cli-output")


def run_qc(
    image_path: str,
    api_key: str,
    *,
    launch_script: Path,
    run: Callable,
    estimated_cost_usd: float | None = None,
) -> dict:
    arguments = ["bash", str(launch_script), "qc", "--input", image_path]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    try:
        result = run(arguments, capture_output=True, text=True, timeout=120, env=environment, check=True)
        assessment = parse_qc_output(result.stdout + result.stderr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        assessment = normalize_qc_assessment(
            {"issues": ["Provider assessment failed; review manually or retry later."]},
            model="unavailable",
        )
        assessment["error"] = "QC provider request failed"
        assessment["error_code"] = error_code(error)
    except Exception:
        assessment = normalize_qc_assessment(
            {"issues": ["QC service was unavailable; review manually or retry later."]},
            model="unavailable",
        )
        assessment["error"] = "QC service unavailable"
    assessment["estimated_cost_usd"] = estimated_cost_usd
    assessment["cost_estimate_source"] = "CREATIVE_QC_ESTIMATED_COST_USD" if estimated_cost_usd is not None else "not_configured"
    return assessment
