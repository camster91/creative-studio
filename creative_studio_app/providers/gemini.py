"""Gemini provider: the existing composite pipeline (scripts/creative_studio.py).

Gemini paints an empty scene and the real product pixels are pasted on top,
so labels and packaging stay exact. A user's own Gemini key can pay for it.
"""

from collections.abc import Callable
from pathlib import Path

from .base import ShotRequest, failure, save_at_aspect


class GeminiCompositeProvider:
    name = "gemini"
    accepts_user_key = True

    def __init__(self, run_composite: Callable):
        # run_composite(prompt, product_path, api_key, aspect, tier=, name_suffix=)
        # -> list[dict]; see run_cli_composite in the web script.
        self._run_composite = run_composite

    def render(self, request: ShotRequest, api_key: str) -> dict:
        try:
            results = self._run_composite(
                request.prompt,
                str(request.product_path),
                api_key,
                request.aspect,
                tier=request.tier,
                name_suffix=request.output_path.stem,
            )
        except Exception:
            return failure("Image service unavailable", "service_unavailable")
        result = next((item for item in results if "error" not in item), None)
        if result is None:
            first = results[0] if results else {}
            return failure(
                first.get("error", "Image service returned nothing"),
                first.get("error_code", "no_output"),
            )
        try:
            save_at_aspect(Path(result["path"]), request.output_path, request.aspect)
        except (OSError, ValueError, KeyError):
            return failure("Generated image could not be read", "bad_output")
        return {"path": str(request.output_path), "model": result.get("model", "gemini")}
