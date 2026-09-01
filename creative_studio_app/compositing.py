"""Deterministic product foreground preparation and bounded scene placement."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


class CompositeInputError(ValueError):
    """The supplied product image cannot produce a reviewable composite."""


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    width: int
    height: int
    canvas_width: int
    canvas_height: int
    source_width: int
    source_height: int

    def as_dict(self) -> dict:
        return asdict(self)


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_composite_manifest(
    output_path: str | Path,
    *,
    source_path: str | Path,
    foreground_path: str | Path,
    environment_path: str | Path,
    placement: Placement,
) -> dict:
    """Persist content-addressed input, transform, environment, and output lineage."""
    output = Path(output_path)
    manifest = {
        "schema_version": 1,
        "source_sha256": _sha256(source_path),
        "prepared_foreground_sha256": _sha256(foreground_path),
        "environment_sha256": _sha256(environment_path),
        "composite_sha256": _sha256(output),
        "placement": placement.as_dict(),
    }
    output.with_suffix(".composite.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8"
    )
    return manifest


def _connected_white_background(image: Image.Image) -> Image.Image:
    """Return alpha that removes only near-white pixels connected to the canvas edge."""
    red, green, blue, _alpha = image.split()
    threshold = lambda value: 255 if value >= 245 else 0
    candidate = ImageChops.multiply(red.point(threshold), green.point(threshold))
    candidate = ImageChops.multiply(candidate, blue.point(threshold))

    # A one-pixel white frame joins all canvas edges, allowing one bounded flood
    # fill instead of globally deleting legitimate white pixels inside a pack.
    padded = Image.new("L", (image.width + 2, image.height + 2), 255)
    padded.paste(candidate, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128, thresh=0)
    connected = padded.crop((1, 1, image.width + 1, image.height + 1))
    alpha = connected.point(lambda value: 0 if value == 128 else 255)
    return alpha.filter(ImageFilter.GaussianBlur(1))


def prepare_foreground_image(image: Image.Image, *, min_subject_pixels: int = 32) -> Image.Image:
    """Preserve supplied alpha or remove only connected white background, then crop."""
    foreground = image.convert("RGBA")
    supplied_alpha = foreground.getchannel("A")
    if supplied_alpha.getextrema() == (255, 255):
        foreground.putalpha(_connected_white_background(foreground))

    bounds = foreground.getchannel("A").getbbox()
    if not bounds:
        raise CompositeInputError("No product foreground could be detected")
    foreground = foreground.crop(bounds)
    if foreground.width < min_subject_pixels or foreground.height < min_subject_pixels:
        raise CompositeInputError(
            f"Detected product is too small; use a foreground at least {min_subject_pixels}px in each dimension"
        )
    return foreground


def prepare_foreground_file(input_path: str | Path, output_path: str | Path) -> Path:
    with Image.open(input_path) as source:
        foreground = prepare_foreground_image(source)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    foreground.save(destination, "PNG")
    return destination


def validate_foreground_file(input_path: str | Path) -> tuple[int, int]:
    """Decode and validate a pack before any provider key, credit, or call is used."""
    with Image.open(input_path) as source:
        foreground = prepare_foreground_image(source)
    return foreground.size


def composite_foreground(
    background: Image.Image,
    foreground: Image.Image,
    *,
    max_width_fraction: float = 0.35,
    max_height_fraction: float = 0.70,
    bottom_fraction: float = 0.90,
) -> tuple[Image.Image, Placement]:
    """Center and bottom-align a product without clipping or changing its aspect ratio."""
    if background.width < 1 or background.height < 1:
        raise ValueError("Background canvas is empty")
    foreground = foreground.convert("RGBA")
    if foreground.width < 1 or foreground.height < 1:
        raise CompositeInputError("Product foreground is empty")
    max_width = max(1, int(background.width * max_width_fraction))
    max_height = max(1, int(background.height * max_height_fraction))
    scale = min(max_width / foreground.width, max_height / foreground.height)
    width = max(1, int(round(foreground.width * scale)))
    height = max(1, int(round(foreground.height * scale)))
    resized = foreground.resize((width, height), Image.Resampling.LANCZOS)
    x = (background.width - width) // 2
    bottom = min(background.height, max(height, int(background.height * bottom_fraction)))
    y = bottom - height

    canvas = background.convert("RGBA").copy()
    shadow_alpha = resized.getchannel("A").point(lambda value: 60 if value > 50 else 0)
    shadow = Image.new("RGBA", resized.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha.filter(ImageFilter.GaussianBlur(14)))
    canvas.alpha_composite(shadow, (min(background.width - width, x + 4), min(background.height - height, y + 4)))
    canvas.alpha_composite(resized, (x, y))
    placement = Placement(
        x=x, y=y, width=width, height=height,
        canvas_width=background.width, canvas_height=background.height,
        source_width=foreground.width, source_height=foreground.height,
    )
    return canvas, placement
