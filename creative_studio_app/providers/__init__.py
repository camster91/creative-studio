"""Image providers for the photoshoot pack.

PHOTOGEN_IMAGE_PROVIDER picks one:
  auto (default)  Higgsfield when its key ID and secret are set, else Gemini
  higgsfield      Higgsfield only
  gemini          the existing Gemini composite pipeline
"""

import os
from collections.abc import Callable

from . import higgsfield
from .base import ImageProvider, ShotRequest, failure, save_at_aspect
from .gemini import GeminiCompositeProvider

__all__ = [
    "ImageProvider", "ShotRequest", "failure", "save_at_aspect",
    "GeminiCompositeProvider", "select_provider",
]


def select_provider(run_composite: Callable) -> ImageProvider:
    choice = os.environ.get("PHOTOGEN_IMAGE_PROVIDER", "auto").strip().lower()
    if choice == "higgsfield" or (choice == "auto" and higgsfield.configured()):
        return higgsfield.HiggsfieldProvider.from_env()
    return GeminiCompositeProvider(run_composite)
