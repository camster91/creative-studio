"""One-button product photoshoot: vibes, the pack shot list, and the pack runner.

A brand uploads one product photo, optionally picks a vibe, and gets a pack:
hero banner, lifestyle shot, square / portrait / story ads and a clean
white-background cutout. Each output is charged at the tier weight
(billing.credits_for) up front and refunded one by one if it fails.
"""

import json
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .compositing import CompositeInputError, prepare_foreground_image
from .providers import ShotRequest, failure

DEFAULT_VIBE = "clean"
DEFAULT_TIER = "balanced"
PACK_TIERS = ("balanced", "quality", "ultra")
MAX_NOTE_CHARS = 300
PARALLEL_SHOTS = 3


@dataclass(frozen=True)
class Vibe:
    id: str
    label: str
    blurb: str
    swatch: tuple[str, str, str]
    set_design: str


VIBES = {
    vibe.id: vibe
    for vibe in (
        Vibe("clean", "Clean studio", "Bright and minimal. Lets the product speak.",
             ("#f6f5f2", "#e4e1da", "#c9c4ba"),
             "a bright, minimal studio set with a soft warm-grey seamless backdrop, "
             "large diffused key light, gentle natural shadows, calm neutral palette"),
        Vibe("sunlit", "Sunlit natural", "Warm daylight, linen and wood.",
             ("#f3e3c7", "#d9b27c", "#8a6a43"),
             "warm morning sunlight through a window, light oak and linen surfaces, "
             "soft dappled shadows, airy natural palette"),
        Vibe("bold", "Bold colour", "Punchy colour-blocked sets that stop the scroll.",
             ("#ff5a36", "#ffd23f", "#3a86ff"),
             "a bold colour-blocked set with saturated complementary backdrop and plinths, "
             "crisp hard light with clean graphic shadows, playful modern palette"),
        Vibe("luxe", "Moody luxe", "Dark, rich tones with dramatic light.",
             ("#1c1a1f", "#4a3b35", "#b8925a"),
             "a dark premium set with deep charcoal and stone textures, warm rim light, "
             "dramatic falloff, subtle brass accents, rich low-key palette"),
        Vibe("fresh", "Fresh outdoors", "Open air, greenery and natural light.",
             ("#e8f1e4", "#8fbf7f", "#3e6b48"),
             "an outdoor setting with fresh greenery, natural stone or wood surface, "
             "bright open shade daylight, fresh botanical palette"),
    )
}


@dataclass(frozen=True)
class Shot:
    id: str
    label: str
    use: str
    aspect: str
    composition: str


SHOTS = (
    Shot("hero", "Hero banner", "Website & email header", "16:9",
         "wide hero banner composition, product as the clear focal point placed "
         "slightly off-centre, generous clean negative space on one side for a headline"),
    Shot("lifestyle", "Lifestyle", "Social posts & product pages", "4:5",
         "an authentic lifestyle scene in a real-world setting where this kind of product "
         "is naturally used, lived-in details, editorial feel"),
    Shot("square", "Square ad", "Instagram & Facebook feed", "1:1",
         "centred, simple and bold square composition, clear space above the product "
         "for a short headline"),
    Shot("portrait", "Portrait ad", "Feed ads (4:5)", "4:5",
         "vertical feed-ad composition, product large in frame, uncluttered background"),
    Shot("story", "Story ad", "Stories, Reels & TikTok", "9:16",
         "tall full-screen vertical composition, product in the middle third, clear "
         "space at the top and bottom for captions and app buttons"),
    Shot("cutout", "White background", "Amazon, Shopify & marketplaces", "1:1",
         "pure white seamless background (#FFFFFF), product centred, soft contact "
         "shadow only, e-commerce packshot"),
)
SHOT_IDS = tuple(shot.id for shot in SHOTS)
SHOTS_BY_ID = {shot.id: shot for shot in SHOTS}


def build_prompt(shot: Shot, vibe: Vibe, note: str, *, product_in_scene: bool) -> str:
    """Write the prompt so the brand never has to.

    product_in_scene=True: the model edits the product reference into the
    scene (Higgsfield). False: the model paints an empty set and the real
    product is composited on top (Gemini pipeline).
    """
    style = "" if shot.id == "cutout" else f" Set design: {vibe.set_design}."
    extra = f" Brand note: {note}." if note else ""
    if product_in_scene:
        return (
            f"Professional commercial product photograph of the product in the reference "
            f"image. {shot.composition[0].upper()}{shot.composition[1:]}.{style}{extra} "
            "Keep the product exactly as it appears in the reference: same shape, colours, "
            "label, text and proportions. Do not redesign or relabel it. Photorealistic, "
            "sharp focus on the product, high-end advertising quality. No added text, "
            "logos or watermarks."
        )
    return (
        f"Empty set for a premium product photo: {shot.composition}.{style}{extra} "
        "A flat, level surface where a product will be placed. Photorealistic, "
        "high-end advertising quality. No text, logos or watermarks."
    )


def clean_note(raw: str) -> str:
    note = re.sub(r"\s+", " ", str(raw or "")).strip()
    return note[:MAX_NOTE_CHARS]


def parse_shots(raw) -> list[str] | None:
    """Return the requested shot ids in pack order, or None if invalid/empty."""
    if raw in (None, ""):
        return list(SHOT_IDS)
    requested = raw if isinstance(raw, list) else str(raw).split(",")
    requested = {str(item).strip() for item in requested if str(item).strip()}
    if not requested or not requested <= set(SHOT_IDS):
        return None
    return [shot_id for shot_id in SHOT_IDS if shot_id in requested]


def make_white_cutout(product_path: Path, destination: Path, size: int = 2000) -> bool:
    """Place the product on pure white locally. False if the background can't be removed.

    Uses the same deterministic background removal as the composite pipeline:
    supplied transparency, or near-white background touching the edges.
    """
    try:
        with Image.open(product_path) as source:
            source_size = source.size
            foreground = prepare_foreground_image(source)
    except (CompositeInputError, OSError, ValueError):
        return False
    if foreground.size == source_size and foreground.getchannel("A").getextrema() == (255, 255):
        return False  # opaque photo: no background was removed, so not a real cutout
    scale = min(size * 0.76 / foreground.width, size * 0.76 / foreground.height)
    width = max(1, round(foreground.width * scale))
    height = max(1, round(foreground.height * scale))
    product = foreground.resize((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    x = (size - width) // 2
    y = (size - height) // 2
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ellipse = Image.new("L", (size, size), 0)
    ImageDraw.Draw(ellipse).ellipse(
        (x + width * 0.1, y + height - height * 0.03, x + width * 0.9, y + height + height * 0.04),
        fill=70,
    )
    shadow.putalpha(ellipse.filter(ImageFilter.GaussianBlur(size // 80)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(product, (x, y))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(destination, "PNG")
    return True


# ── Pack state ─────────────────────────────────────────────────────────

ACTIVE = ("queued", "running")


class PackStore:
    """One JSON file per pack, guarded by a process lock (gunicorn runs -w 1)."""

    def __init__(self, data_dir: Path):
        self._dir = data_dir / "packs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.live: set[str] = set()

    def _path(self, pack_id: str) -> Path | None:
        if not re.fullmatch(r"pk_[0-9a-f]{24}", pack_id or ""):
            return None
        return self._dir / f"{pack_id}.json"

    def save(self, pack: dict) -> None:
        with self._lock:
            path = self._path(pack["id"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(pack), encoding="utf-8")
            temporary.replace(path)

    def load(self, pack_id: str) -> dict | None:
        path = self._path(pack_id)
        if path is None or not path.exists():
            return None
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def get(self, pack_id: str, owner_id: str) -> dict | None:
        pack = self.load(pack_id)
        if not pack or pack.get("owner_id") != owner_id:
            return None
        return pack

    def update(self, pack_id: str, change: Callable[[dict], None]) -> dict:
        with self._lock:
            pack = self.load(pack_id)
            change(pack)
            self.save(pack)
            return pack

    def owner_has_active(self, owner_id: str) -> bool:
        with self._lock:
            return any(
                (pack := self.load(pack_id)) and pack["owner_id"] == owner_id
                and pack["status"] in ACTIVE
                for pack_id in self.live
            )


def new_pack(*, owner_id: str, user_id: str | None, vibe: str, tier: str, note: str,
             shot_ids: list[str], provider: str, credits_each: int, charged: bool) -> dict:
    return {
        "id": "pk_" + uuid.uuid4().hex[:24],
        "owner_id": owner_id,
        "user_id": user_id,
        "created_at": time.time(),
        "vibe": vibe,
        "tier": tier,
        "note": note,
        "provider": provider,
        "credits_each": credits_each,
        "credits_charged": credits_each * len(shot_ids) if charged else 0,
        "credits_refunded": 0,
        "status": "queued",
        "outputs": [
            {"id": shot_id, "label": SHOTS_BY_ID[shot_id].label,
             "use": SHOTS_BY_ID[shot_id].use, "aspect": SHOTS_BY_ID[shot_id].aspect,
             "status": "pending"}
            for shot_id in shot_ids
        ],
    }


def finish_status(outputs: list[dict]) -> str:
    done = sum(1 for output in outputs if output["status"] == "done")
    if done == len(outputs):
        return "done"
    return "partial" if done else "failed"


def public_view(pack: dict) -> dict:
    """Pack JSON for the browser: no paths, owner keys or user ids."""
    outputs = [
        {key: output.get(key) for key in ("id", "label", "use", "aspect", "status", "url", "error")}
        for output in pack["outputs"]
    ]
    done = sum(1 for output in outputs if output["status"] == "done")
    return {
        "pack_id": pack["id"],
        "status": pack["status"],
        "vibe": pack["vibe"],
        "tier": pack["tier"],
        "provider": pack["provider"],
        "credits_charged": pack["credits_charged"],
        "credits_refunded": pack["credits_refunded"],
        "completed": done,
        "total": len(outputs),
        "outputs": outputs,
        "download_url": f"/api/shoot/{pack['id']}/download" if done else None,
    }


FRIENDLY_ERRORS = {
    "moderated": "This shot was blocked by the image service's safety filter.",
    "timeout": "This shot took too long and was stopped.",
    "interrupted": "The server restarted before this shot finished.",
    "provider_out_of_credits": "The image service is temporarily unavailable.",
}


def friendly_error(code: str | None) -> str:
    return FRIENDLY_ERRORS.get(code or "", "This shot didn't come out. Its credits were refunded.")


def run_pack(
    store: PackStore,
    pack_id: str,
    *,
    provider,
    api_key: str,
    product_path: Path,
    output_dir: Path,
    to_image_url: Callable[[str], str],
    refund_outputs: Callable[[int], None],
    on_output_done: Callable[[dict, dict], None] = lambda *_args: None,
) -> dict:
    """Render every output, refunding each failed one as soon as it fails."""
    pack = store.load(pack_id)
    vibe = VIBES[pack["vibe"]]
    pack_dir = output_dir / "packs" / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    store.update(pack_id, lambda state: state.update(status="running"))

    def render_one(index: int, output: dict) -> None:
        shot = SHOTS_BY_ID[output["id"]]
        target = pack_dir / f"{index + 1:02d}-{shot.id}-{shot.aspect.replace(':', 'x')}.png"

        def mark_running(state):
            state["outputs"][index]["status"] = "running"
        store.update(pack_id, mark_running)

        if shot.id == "cutout" and make_white_cutout(product_path, target):
            result = {"path": str(target), "model": "local-cutout"}
        else:
            try:
                result = provider.render(
                    ShotRequest(
                        prompt=build_prompt(shot, vibe, pack.get("note", ""),
                                            product_in_scene=not provider.accepts_user_key),
                        aspect=shot.aspect,
                        tier=pack["tier"],
                        product_path=product_path,
                        output_path=target,
                    ),
                    api_key,
                )
            except Exception:
                result = failure("Image service unavailable", "service_unavailable")
        ok = "error" not in result and Path(result.get("path", "")).is_file()
        if not ok:
            refund_outputs(1)

        def record(state):
            entry = state["outputs"][index]
            if ok:
                entry.update(status="done", path=result["path"],
                             url=to_image_url(result["path"]), model=result.get("model"))
            else:
                entry.update(status="failed", error=friendly_error(result.get("error_code")),
                             error_code=result.get("error_code") or "failed")
                state["credits_refunded"] += state["credits_each"] if state["credits_charged"] else 0
        updated = store.update(pack_id, record)
        if ok:
            on_output_done(updated, updated["outputs"][index])

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_SHOTS) as pool:
            list(pool.map(lambda pair: render_one(*pair), enumerate(pack["outputs"])))
    finally:
        final = store.update(pack_id, lambda state: state.update(
            status=finish_status(state["outputs"]), finished_at=time.time()))
        store.live.discard(pack_id)
    return final


def recover_if_orphaned(store: PackStore, pack: dict, refund_outputs: Callable[[dict, int], None]) -> dict:
    """A pack marked active with no live runner was cut off by a restart.

    Fail its unfinished outputs and refund them once (the status change is the guard).
    """
    if pack["status"] not in ACTIVE or pack["id"] in store.live:
        return pack
    unfinished = [0]

    def settle(state):
        if state["status"] not in ACTIVE:
            return
        for output in state["outputs"]:
            if output["status"] in ("pending", "running"):
                output.update(status="failed", error=friendly_error("interrupted"),
                              error_code="interrupted")
                unfinished[0] += 1
                if state["credits_charged"]:
                    state["credits_refunded"] += state["credits_each"]
        state["status"] = finish_status(state["outputs"])
    settled = store.update(pack["id"], settle)
    if unfinished[0]:
        refund_outputs(settled, unfinished[0])
    return settled


def build_zip(pack: dict) -> Path | None:
    """Zip the delivered images next to them; rebuilt only if missing."""
    done = [output for output in pack["outputs"] if output["status"] == "done" and output.get("path")]
    if not done:
        return None
    first = Path(done[0]["path"])
    archive = first.parent / f"photoshoot-{pack['vibe']}-{pack['id'][-6:]}-{len(done)}.zip"
    if not archive.exists():
        temporary = archive.with_suffix(".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
            for output in done:
                path = Path(output["path"])
                if path.is_file():
                    bundle.write(path, arcname=path.name)
        temporary.replace(archive)
    return archive
