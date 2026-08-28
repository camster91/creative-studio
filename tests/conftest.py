"""Test-only configuration for intentionally hidden development credentials."""

import os


os.environ["CREATIVE_EXPOSE_MAGIC_LINK_TOKEN"] = "1"
os.environ["CREATIVE_ALLOW_UNOWNED_ASSETS"] = "1"
