"""Stateful multi-turn image iteration."""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from .provider_errors import error_code


def turn(
    sessions: dict[str, dict],
    session_key: str,
    api_key: str,
    prompt: str,
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
) -> tuple[list[dict], dict]:
    destination = output_dir / datetime.now().strftime("%Y-%m-%d") / "chat"
    destination.mkdir(parents=True, exist_ok=True)
    session = sessions.setdefault(session_key, {
        "turn": 0, "current_input": input_image, "initial_input": input_image, "history": [],
    })
    session["turn"] += 1
    turn_number = session["turn"]
    filename = f"turn-{turn_number:02d}.png"
    output_path = destination / session_key / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model, resolution = tier_models.get(tier, ("gemini-3.1-flash-image-preview", "1K"))
    arguments = [
        python_executable, script_path, "direct", "--prompt", prompt, "--tier", tier,
        "--aspect-ratio", aspect, "--resolution", resolution, "--filename", str(output_path),
    ]
    current_input = session["current_input"]
    if current_input:
        arguments += ["--input-image", current_input]
    environment = os.environ.copy()
    environment["GEMINI_API_KEY"] = api_key
    environment["CREATIVE_OUTPUT_DIR"] = str(output_dir)
    try:
        run(arguments, capture_output=True, text=True, timeout=300, env=environment, check=True)
        images = []
        if output_path.exists():
            images.append({
                "path": str(output_path), "url": to_image_url(str(output_path)),
                "name": filename, "cost": record_cost(model, resolution), "model": model,
                "turn": turn_number,
            })
            session["current_input"] = str(output_path)
            session["history"].append({
                "turn": turn_number, "prompt": prompt, "input": current_input,
                "output": str(output_path),
            })
        return images, session
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        session["turn"] -= 1
        return [{"error": "Chat provider request failed", "error_code": error_code(error)}], session
    except Exception:
        session["turn"] -= 1
        return [{"error": "Chat service unavailable", "error_code": "service_unavailable"}], session


def history(sessions: dict[str, dict], session_key: str) -> list[dict]:
    return sessions.get(session_key, {}).get("history", [])


def reset(sessions: dict[str, dict], session_key: str) -> dict:
    session = sessions.get(session_key, {})
    session["turn"] = 0
    session["current_input"] = session.get("initial_input")
    session["history"] = []
    sessions[session_key] = session
    return session
