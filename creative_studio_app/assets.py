"""Generated-output path safety and spatial annotation helpers."""

import uuid
from pathlib import Path


def image_url(path: str, output_dir: Path) -> str:
    if not path:
        return ""
    candidate = Path(path)
    try:
        relative = candidate.relative_to(output_dir)
        return f"/image/{relative}"
    except (ValueError, NotImplementedError):
        if candidate.exists():
            return f"/image/{candidate.parent.name}/{candidate.name}"
        return ""


def safe_output_relpath(rel_path: str, output_dir: Path) -> Path | None:
    """Resolve an output-relative path without permitting traversal."""
    if not rel_path:
        return None
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") or part.startswith("..") for part in parts):
        return None
    target = output_dir.joinpath(*parts)
    try:
        resolved = target.resolve()
        resolved.relative_to(output_dir.resolve())
    except (ValueError, RuntimeError):
        return None
    return resolved if resolved.exists() and resolved.is_file() else None


def new_pin_id() -> str:
    return uuid.uuid4().hex[:8]


def pin_to_region(x: float, y: float) -> str:
    """Convert normalized coordinates into a human-readable region."""
    vertical = "top" if y < 0.33 else "middle" if y < 0.66 else "bottom"
    horizontal = "left" if x < 0.33 else "center" if x < 0.66 else "right"
    if vertical == "middle" and horizontal == "center":
        return "center"
    return f"{vertical}-{horizontal}" if vertical != "middle" else horizontal


def build_pin_prompt(pins: list[dict]) -> str:
    """Build a spatially constrained refinement prompt from annotations."""
    lines = []
    for index, pin in enumerate(pins, 1):
        region = pin_to_region(pin.get("x", 0.5), pin.get("y", 0.5))
        instruction = pin.get("text", "").strip()
        if instruction:
            lines.append(f"[{index}] {region}: {instruction}")
    if not lines:
        return ""
    return (
        "Make these targeted changes to specific areas of the image: "
        + "; ".join(lines)
        + ". Apply each change only to its specified region. Preserve all other areas exactly as they are."
    )
