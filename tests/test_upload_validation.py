import json
from io import BytesIO

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from creative_studio_app.uploads import purge_expired_uploads, save_image_upload


def upload(data: bytes, filename: str = "input.png") -> FileStorage:
    return FileStorage(stream=BytesIO(data), filename=filename)


def encoded_image(format="PNG", size=(32, 24), *, exif=False) -> bytes:
    buffer = BytesIO()
    image = Image.new("RGBA" if format == "PNG" else "RGB", size, (10, 20, 30, 128) if format == "PNG" else (10, 20, 30))
    kwargs = {}
    if exif:
        metadata = Image.Exif()
        metadata[0x010E] = "private customer description"
        kwargs["exif"] = metadata
    image.save(buffer, format, **kwargs)
    return buffer.getvalue()


def test_decodes_reencodes_and_records_lifecycle_metadata(tmp_path):
    destination = save_image_upload(
        upload(encoded_image("JPEG", exif=True), "../../customer-secret.exe"),
        tmp_path,
        purpose="product",
        owner_id="user:abc",
        retention_days=7,
    )
    assert destination.parent == tmp_path
    assert destination.name.startswith("product_") and destination.suffix == ".png"
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.size == (32, 24)
        assert not image.getexif()
        assert "private customer description" not in destination.read_bytes().decode("latin1")
    metadata = json.loads(destination.with_suffix(".meta.json").read_text())
    assert metadata["owner_id"] == "user:abc"
    assert metadata["purpose"] == "product"
    assert metadata["source_format"] == "JPEG"
    assert metadata["width"] == 32 and metadata["height"] == 24
    assert metadata["created_at"] < metadata["expires_at"]


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg onload=alert(1)></svg>"])
def test_rejects_empty_malformed_and_non_raster_inputs(tmp_path, data):
    with pytest.raises(ValueError):
        save_image_upload(upload(data), tmp_path, purpose="qc", owner_id="user:abc")


def test_rejects_encoded_byte_limit(tmp_path):
    with pytest.raises(ValueError, match="exceeds"):
        save_image_upload(
            upload(encoded_image()), tmp_path, purpose="qc", owner_id="user:abc", max_bytes=10
        )


def test_rejects_decoded_pixel_and_dimension_limits(tmp_path):
    data = encoded_image(size=(100, 100))
    with pytest.raises(ValueError, match="pixel"):
        save_image_upload(upload(data), tmp_path, purpose="qc", owner_id="user:abc", max_pixels=9_999)
    with pytest.raises(ValueError, match="dimensions"):
        save_image_upload(upload(data), tmp_path, purpose="qc", owner_id="user:abc", max_dimension=99)


def test_rejects_animated_images(tmp_path):
    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
    buffer = BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], loop=0)
    with pytest.raises(ValueError, match="Animated"):
        save_image_upload(upload(buffer.getvalue(), "animated.gif"), tmp_path, purpose="chat", owner_id="user:abc")


def test_retention_cleanup_is_dry_run_first_and_deletes_pair(tmp_path):
    from datetime import datetime, timedelta, timezone

    destination = save_image_upload(
        upload(encoded_image()), tmp_path, purpose="qc", owner_id="user:abc", retention_days=1
    )
    future = datetime.now(timezone.utc) + timedelta(days=2)
    assert purge_expired_uploads(tmp_path, now=future, dry_run=True) == [destination.resolve()]
    assert destination.exists() and destination.with_suffix(".meta.json").exists()
    assert purge_expired_uploads(tmp_path, now=future, dry_run=False) == [destination.resolve()]
    assert not destination.exists() and not destination.with_suffix(".meta.json").exists()


def test_retention_cleanup_ignores_malformed_and_traversal_sidecars(tmp_path):
    (tmp_path / "bad.meta.json").write_text("not-json")
    (tmp_path / "escape.meta.json").write_text(json.dumps({
        "expires_at": "2000-01-01T00:00:00+00:00",
        "stored_name": "../outside.png",
    }))
    assert purge_expired_uploads(tmp_path, dry_run=False) == []
