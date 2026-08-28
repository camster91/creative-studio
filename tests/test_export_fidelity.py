from pathlib import Path

import pytest
from PIL import Image

from creative_studio_app.delivery import EXPORT_PRESETS, export_presets


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "source.png"
    image = Image.new("RGBA", (1200, 800), (255, 0, 0, 128))
    image.save(path)
    return path


@pytest.mark.parametrize(
    ("preset", "size", "mode", "dpi"),
    [
        ("amazon", (2000, 2000), "RGB", 72),
        ("shopify", (2048, 2048), "RGB", 72),
        ("meta-feed", (1080, 1350), "RGBA", 72),
        ("meta-stories", (1080, 1920), "RGBA", 72),
        ("web-hero", (1920, 1080), "RGBA", 72),
        ("pinterest", (1000, 1500), "RGBA", 72),
        ("print-dpi", (1200, 800), "RGB", 300),
    ],
)
def test_preset_dimensions_alpha_and_dpi(source, tmp_path, preset, size, mode, dpi):
    [output] = export_presets(str(source), preset, tmp_path / "out")
    assert output.name == f"source-{preset}.png"
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == size
        assert image.mode == mode
        assert image.info["dpi"][0] == pytest.approx(dpi, abs=0.1)


def test_only_requested_files_are_returned_even_with_stale_exports(source, tmp_path):
    first = export_presets(str(source), "amazon,shopify", tmp_path / "out")
    second = export_presets(str(source), "amazon", tmp_path / "out")
    assert len(first) == 2
    assert [path.name for path in second] == ["source-amazon.png"]


def test_unknown_preset_fails_instead_of_silently_skipping(source, tmp_path):
    with pytest.raises(ValueError, match="Unknown export preset"):
        export_presets(str(source), "amazon,typo", tmp_path / "out")


def test_catalog_is_complete_and_explicit():
    assert set(EXPORT_PRESETS) == {
        "amazon", "shopify", "meta-feed", "meta-stories",
        "web-hero", "pinterest", "print-dpi",
    }
