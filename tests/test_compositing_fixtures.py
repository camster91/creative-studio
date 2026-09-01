"""Synthetic, repository-owned fixture criteria for deterministic compositing."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
import pytest
from PIL import Image, ImageDraw

from creative_studio_app.compositing import (
    CompositeInputError,
    Placement,
    composite_foreground,
    prepare_foreground_image,
    write_composite_manifest,
)
from creative_studio_app.generation import composite as run_composite


def test_supplied_transparency_and_visible_rgb_are_preserved():
    source = Image.new("RGBA", (120, 180), (0, 0, 0, 0))
    ImageDraw.Draw(source).rounded_rectangle((20, 20, 99, 159), radius=12, fill=(18, 82, 190, 220))
    foreground = prepare_foreground_image(source)
    assert foreground.size == (80, 140)
    assert foreground.getpixel((40, 70)) == (18, 82, 190, 220)


def test_connected_white_background_is_removed_but_enclosed_white_label_remains():
    source = Image.new("RGB", (160, 220), "white")
    draw = ImageDraw.Draw(source)
    draw.rounded_rectangle((30, 20, 129, 199), radius=18, fill=(15, 55, 120))
    draw.rectangle((48, 75, 111, 135), fill="white")
    foreground = prepare_foreground_image(source)
    assert foreground.width < source.width and foreground.height < source.height
    assert foreground.getpixel((50, 85))[:3] == (255, 255, 255)
    assert foreground.getpixel((50, 85))[3] == 255


@pytest.mark.parametrize("size", [(8, 80), (80, 8), (1, 1)])
def test_tiny_or_empty_foregrounds_fail_truthfully(size):
    source = Image.new("RGBA", size, (20, 70, 180, 255))
    with pytest.raises(CompositeInputError, match="too small"):
        prepare_foreground_image(source)


def test_all_white_opaque_input_has_no_detectable_product():
    with pytest.raises(CompositeInputError, match="No product foreground"):
        prepare_foreground_image(Image.new("RGB", (200, 300), "white"))


@pytest.mark.parametrize("size", [(120, 500), (500, 120), (400, 600), (600, 400)])
def test_portrait_landscape_and_rotated_geometry_never_clips(size):
    foreground = Image.new("RGBA", size, (20, 70, 180, 255))
    canvas, placement = composite_foreground(Image.new("RGB", (1200, 900), (225, 220, 210)), foreground)
    assert canvas.size == (1200, 900)
    assert placement.x >= 0 and placement.y >= 0
    assert placement.x + placement.width <= placement.canvas_width
    assert placement.y + placement.height <= placement.canvas_height
    assert placement.width / placement.height == pytest.approx(size[0] / size[1], rel=0.02)
    assert placement.width <= int(1200 * 0.35)
    assert placement.height <= int(900 * 0.70)
    assert placement.y + placement.height == int(900 * 0.90)


def test_manifest_content_addresses_every_stage_and_records_exact_geometry(tmp_path):
    source = tmp_path / "source.png"
    foreground = tmp_path / "foreground.png"
    environment = tmp_path / "environment.png"
    output = tmp_path / "result.png"
    for path, content in [(source, b"source"), (foreground, b"foreground"), (environment, b"environment"), (output, b"result")]:
        path.write_bytes(content)
    placement = Placement(10, 20, 300, 500, 1200, 900, 240, 400)
    manifest = write_composite_manifest(
        output, source_path=source, foreground_path=foreground,
        environment_path=environment, placement=placement,
    )
    persisted = json.loads(output.with_suffix(".composite.json").read_text())
    assert persisted == manifest
    assert manifest["source_sha256"] == hashlib.sha256(b"source").hexdigest()
    assert manifest["prepared_foreground_sha256"] == hashlib.sha256(b"foreground").hexdigest()
    assert manifest["environment_sha256"] == hashlib.sha256(b"environment").hexdigest()
    assert manifest["composite_sha256"] == hashlib.sha256(b"result").hexdigest()
    assert manifest["placement"] == placement.as_dict()


def test_generation_result_propagates_composite_manifest(tmp_path):
    manifest = {"schema_version": 1, "source_sha256": "a" * 64, "placement": {"x": 10}}

    def fake_run(arguments, **_kwargs):
        filename = arguments[-1]
        output = tmp_path / datetime.now().strftime("%Y-%m-%d") / "composite" / filename
        output.write_bytes(b"result")
        output.with_suffix(".composite.json").write_text(json.dumps(manifest))

    result = run_composite(
        "prompt", "/tmp/product.png", "key", "4:5", "quality",
        name_suffix="fixture", output_dir=tmp_path, script_path="script.py",
        python_executable="python", run=fake_run, record_cost=lambda _model: 0.04,
        to_image_url=lambda path: f"/image/{Path(path).name}",
    )
    assert result[0]["composite_manifest"] == manifest
