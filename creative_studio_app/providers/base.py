"""Image provider interface for the one-button photoshoot pack.

A provider renders one scene shot of the brand's product to a local file.
Results are plain dicts so they match the rest of the app's image entries:
success -> {"path": str, "model": str}
failure -> {"error": str, "error_code": str}
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ShotRequest:
    prompt: str
    aspect: str          # "16:9", "4:5", "1:1", "9:16"
    tier: str            # billing tier: fast / balanced / quality / ultra
    product_path: Path   # validated product upload
    output_path: Path    # where the finished PNG must be written


class ImageProvider(Protocol):
    name: str
    # True when a caller's own Gemini key (X-API-Key) can pay for the render.
    # Providers that only run on the server's account always charge credits.
    accepts_user_key: bool

    def render(self, request: ShotRequest, api_key: str) -> dict:
        ...


def failure(message: str, code: str) -> dict:
    return {"error": message, "error_code": code}


def save_at_aspect(source: Path, destination: Path, aspect: str) -> None:
    """Center-crop `source` to the exact `aspect` and save it as a PNG.

    Models don't always honour a requested ratio (and Higgsfield has no 4:5),
    so every scene is normalised here; ad slots need exact sizes.
    """
    from PIL import Image

    width_ratio, height_ratio = (int(part) for part in aspect.split(":"))
    target = width_ratio / height_ratio
    with Image.open(source) as image:
        image = image.convert("RGB")
        width, height = image.size
        if width / height > target:
            new_width = round(height * target)
            left = (width - new_width) // 2
            image = image.crop((left, 0, left + new_width, height))
        elif width / height < target:
            new_height = round(width / target)
            top = (height - new_height) // 2
            image = image.crop((0, top, width, top + new_height))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "PNG")
