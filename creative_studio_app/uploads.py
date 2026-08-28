"""Validated, metadata-stripped image upload persistence."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}


def save_image_upload(
    upload,
    upload_dir: Path,
    *,
    purpose: str,
    owner_id: str,
    max_bytes: int = 16 * 1024 * 1024,
    max_dimension: int = 12_000,
    max_pixels: int = 40_000_000,
    retention_days: int = 30,
) -> Path:
    """Decode and re-encode one bounded image, discarding all source metadata."""
    if not upload or not getattr(upload, "filename", ""):
        raise ValueError("Image upload is empty")
    raw = upload.stream.read(max_bytes + 1)
    if not raw:
        raise ValueError("Image upload is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"Image exceeds {max_bytes // (1024 * 1024)}MB limit")
    try:
        with Image.open(BytesIO(raw)) as probe:
            source_format = (probe.format or "").upper()
            if source_format not in ALLOWED_FORMATS:
                raise ValueError("Unsupported decoded image format")
            if getattr(probe, "is_animated", False) and getattr(probe, "n_frames", 1) > 1:
                raise ValueError("Animated images are not supported")
            width, height = probe.size
            if width < 1 or height < 1 or width > max_dimension or height > max_dimension:
                raise ValueError(f"Image dimensions must be 1..{max_dimension}px")
            if width * height > max_pixels:
                raise ValueError(f"Image exceeds {max_pixels:,} decoded-pixel limit")
            probe.verify()
        with Image.open(BytesIO(raw)) as decoded:
            normalized = ImageOps.exif_transpose(decoded)
            normalized.load()
            if normalized.mode not in ("RGB", "RGBA"):
                normalized = normalized.convert("RGBA" if "transparency" in decoded.info else "RGB")
            else:
                normalized = normalized.copy()
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise ValueError("Upload is not a valid supported image") from error

    upload_dir.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    destination = upload_dir / f"{purpose}_{identifier}.png"
    normalized.save(destination, "PNG", optimize=True)
    now = datetime.now(timezone.utc)
    metadata = {
        "schema_version": 1,
        "owner_id": owner_id,
        "purpose": purpose,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=retention_days)).isoformat(),
        "source_format": source_format,
        "width": width,
        "height": height,
        "bytes_received": len(raw),
        "stored_name": destination.name,
    }
    sidecar = destination.with_suffix(".meta.json")
    temporary = sidecar.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    os.replace(temporary, sidecar)
    return destination


def purge_expired_uploads(
    upload_dir: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = True,
) -> list[Path]:
    """Remove expired canonical uploads and sidecars; ignore malformed records."""
    now = now or datetime.now(timezone.utc)
    removed = []
    if not upload_dir.is_dir():
        return removed
    root = upload_dir.resolve()
    for sidecar in upload_dir.glob("*.meta.json"):
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(metadata["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            stored_name = metadata["stored_name"]
            if Path(stored_name).name != stored_name:
                continue
            image = (upload_dir / stored_name).resolve()
            image.relative_to(root)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError, OSError):
            continue
        if expires_at > now:
            continue
        removed.append(image)
        if not dry_run:
            if image.is_file() and not image.is_symlink():
                image.unlink()
            sidecar.unlink(missing_ok=True)
    return removed
