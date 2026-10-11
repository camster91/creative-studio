"""Higgsfield provider (https://docs.higgsfield.ai), Marketing Studio Image.

Every Higgsfield HTTP call lives in this module. Flow per shot:
  1. upload the product photo once per pack (presigned URL, cached by hash)
  2. POST the model endpoint with an Idempotency-Key
  3. poll status_url with backoff until a terminal status
  4. download the result and crop it to the exact ad ratio

Docs checked 2026-10-04:
  auth      Authorization: Key <key_id>:<key_secret>         /docs/authentication
  uploads   POST /files/generate-upload-url, then PUT bytes     /docs/concepts/file-uploads
  model     POST /marketing-studio/image (2.0 Alpha; also /flare, /sunburst)
            body: prompt, image_urls[], aspect_ratio, resolution, quality,
            enhance_prompt                                     /docs/models/marketing-studio-image
  status    queued | in_progress | completed | failed | nsfw | canceled
  billing   failed and nsfw requests are not charged by Higgsfield
Higgsfield has no 4:5 ratio, so 4:5 shots render at 3:4 and are cropped.

Credentials come from the environment and are never logged or returned.
"""

import hashlib
import json
import os
import random
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from .base import ShotRequest, failure, save_at_aspect

DEFAULT_BASE_URL = "https://api.higgsfield.ai"
DEFAULT_IMAGE_ENDPOINT = "marketing-studio/image"
TERMINAL = {"completed", "failed", "nsfw", "canceled"}

# Higgsfield aspect_ratio enum: auto 1:1 3:2 2:3 4:3 3:4 16:9 9:16 21:9.
ASPECT = {"1:1": "1:1", "16:9": "16:9", "9:16": "9:16", "4:5": "3:4"}

# Billing tier -> (resolution, quality). TODO(Cameron): confirm the per-image
# Higgsfield cost for each pair with POST /estimate/<endpoint> on the real
# account, then revisit CREDIT_WEIGHT_BY_TIER in billing.py.
TIER = {
    "fast": ("1k", "medium"),
    "balanced": ("1k", "high"),
    "quality": ("2k", "high"),
    "ultra": ("4k", "high"),
}


class HiggsfieldError(Exception):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    """Default transport: returns (status, body bytes); never raises on HTTP errors."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def credentials_from_env() -> tuple[str, str]:
    return (
        os.environ.get("HIGGSFIELD_API_KEY_ID", "").strip(),
        os.environ.get("HIGGSFIELD_API_KEY_SECRET", "").strip(),
    )


def configured() -> bool:
    key_id, secret = credentials_from_env()
    return bool(key_id and secret)


class HiggsfieldProvider:
    name = "higgsfield"
    accepts_user_key = False  # always runs on the server's Higgsfield account

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        endpoint: str = DEFAULT_IMAGE_ENDPOINT,
        transport: Callable = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 300,
    ):
        if not key_id or not key_secret:
            raise ValueError("Higgsfield key ID and secret are required")
        self._auth = f"Key {key_id}:{key_secret}"
        self._base = base_url.rstrip("/")
        self._endpoint = endpoint.strip("/")
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout_seconds
        self._uploads: dict[str, str] = {}

    @classmethod
    def from_env(cls, **kwargs) -> "HiggsfieldProvider":
        key_id, secret = credentials_from_env()
        return cls(
            key_id,
            secret,
            base_url=os.environ.get("HIGGSFIELD_API_BASE", DEFAULT_BASE_URL),
            endpoint=os.environ.get("HIGGSFIELD_IMAGE_ENDPOINT", DEFAULT_IMAGE_ENDPOINT),
            **kwargs,
        )

    # ── HTTP ────────────────────────────────────────────────────────────

    def _api(self, method: str, url: str, payload: dict | None = None,
             extra_headers: dict | None = None) -> dict:
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        headers.update(extra_headers or {})
        try:
            status, raw = self._transport(method, url, headers, body, 30)
        except OSError as error:
            raise HiggsfieldError("network_error") from error
        if status >= 400:
            raise HiggsfieldError(_status_code(status), status)
        try:
            return json.loads(raw or b"{}")
        except ValueError as error:
            raise HiggsfieldError("bad_response", status) from error

    def _upload_product(self, product_path: Path) -> str:
        png = _as_png(product_path)
        digest = hashlib.sha256(png).hexdigest()
        if digest in self._uploads:
            return self._uploads[digest]
        ticket = self._api("POST", f"{self._base}/files/generate-upload-url",
                           {"content_type": "image/png"})
        upload_url, public_url = ticket.get("upload_url"), ticket.get("public_url")
        if not upload_url or not public_url:
            raise HiggsfieldError("bad_response")
        # Presigned storage URL: send only the returned headers, never our credentials.
        headers = dict(ticket.get("upload_headers") or {"Content-Type": "image/png"})
        try:
            status, _ = self._transport("PUT", upload_url, headers, png, 60)
        except OSError as error:
            raise HiggsfieldError("upload_failed") from error
        if status >= 300:
            raise HiggsfieldError("upload_failed", status)
        self._uploads[digest] = public_url
        return public_url

    def _submit(self, payload: dict) -> str:
        # Same Idempotency-Key on retry, so a lost response never double-charges.
        idempotency = {"Idempotency-Key": str(uuid.uuid4())}
        url = f"{self._base}/{self._endpoint}"
        for attempt in range(3):
            try:
                receipt = self._api("POST", url, payload, idempotency)
                break
            except HiggsfieldError as error:
                retryable = error.code in ("network_error", "provider_error")
                if not retryable or attempt == 2:
                    raise
                self._sleep(2 ** attempt)
        status_url = receipt.get("status_url")
        if not status_url and receipt.get("request_id"):
            status_url = f"{self._base}/requests/{receipt['request_id']}/status"
        if not status_url:
            raise HiggsfieldError("bad_response")
        return status_url

    def _wait(self, status_url: str) -> dict:
        deadline = self._clock() + self._timeout
        delay = 2.0
        while True:
            try:
                result = self._api("GET", status_url)
            except HiggsfieldError as error:
                if error.code not in ("network_error", "provider_error"):
                    raise
                result = {}
            if result.get("status") in TERMINAL:
                return result
            if self._clock() >= deadline:
                raise HiggsfieldError("timeout")
            self._sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 1.5, 10.0)

    def _download(self, url: str, destination: Path) -> None:
        if not url.startswith("https://"):
            raise HiggsfieldError("bad_response")
        try:
            status, raw = self._transport("GET", url, {}, None, 60)
        except OSError as error:
            raise HiggsfieldError("download_failed") from error
        if status >= 300 or not raw:
            raise HiggsfieldError("download_failed", status)
        destination.write_bytes(raw)

    # ── ImageProvider ──────────────────────────────────────────────────

    def render(self, request: ShotRequest, api_key: str = "") -> dict:
        resolution, quality = TIER.get(request.tier, TIER["balanced"])
        try:
            image_url = self._upload_product(request.product_path)
            status_url = self._submit({
                "prompt": request.prompt[:5000],
                "image_urls": [image_url],
                "aspect_ratio": ASPECT.get(request.aspect, "1:1"),
                "resolution": resolution,
                "quality": quality,
                "enhance_prompt": False,
            })
            result = self._wait(status_url)
            if result.get("status") != "completed":
                code = "moderated" if result.get("status") == "nsfw" else "provider_failed"
                return failure("The image service couldn't make this shot", code)
            images = result.get("images") or []
            if not images or not images[0].get("url"):
                return failure("The image service returned no image", "no_output")
            with tempfile.TemporaryDirectory() as scratch:
                raw_path = Path(scratch) / "raw"
                self._download(images[0]["url"], raw_path)
                save_at_aspect(raw_path, request.output_path, request.aspect)
        except HiggsfieldError as error:
            return failure("The image service couldn't make this shot", error.code)
        except (OSError, ValueError):
            return failure("Generated image could not be read", "bad_output")
        return {"path": str(request.output_path), "model": f"higgsfield/{self._endpoint}"}


def _status_code(status: int) -> str:
    return {
        400: "rejected",
        401: "auth_failed",
        403: "provider_out_of_credits",
        404: "model_unavailable",
        422: "rejected",
        423: "model_unavailable",
        429: "rate_limited",
        503: "model_unavailable",
    }.get(status, "provider_error")


def _as_png(path: Path) -> bytes:
    """Re-encode as PNG: one upload content type, and EXIF/GPS metadata is dropped."""
    from PIL import Image

    with Image.open(path) as image:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        buffer = BytesIO()
        image.save(buffer, "PNG")
    return buffer.getvalue()
