"""One-button photoshoot pack: page, options, start, progress and download."""

import logging
import math
import os
import threading
from collections.abc import Callable
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from . import photoshoot
from .billing import CREDIT_WEIGHT_BY_TIER, credits_for
from .compositing import CompositeInputError, validate_foreground_file
from .costs import check_daily_limit, track_cost

logger = logging.getLogger("creative_studio.photoshoot")

HIGGSFIELD_COST_DEFAULTS = {"balanced": 0.08, "quality": 0.12, "ultra": 0.72}


def higgsfield_cost(tier: str) -> float:
    default = HIGGSFIELD_COST_DEFAULTS[tier]
    try:
        estimate = float(os.environ.get(f"HIGGSFIELD_COST_USD_{tier.upper()}") or default)
        return estimate if math.isfinite(estimate) and estimate > 0 else default
    except ValueError:
        return default

def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="photoshoot-pack").start()


def create_blueprint(
    *,
    store: photoshoot.PackStore,
    select_provider: Callable,
    require_api_key: Callable,
    require_access: Callable,
    current_session: Callable[[], dict | None],
    current_actor_id: Callable[[], str | None],
    spend_credits: Callable[[str, int], tuple[bool, int]],
    auth_db: Callable,
    cost_db: Callable[[], Path],
    cost_lock,
    save_upload: Callable,
    enforce_daily_limit: Callable,
    get_output_dir: Callable[[], Path],
    to_image_url: Callable[[str], str],
    rate_limited: Callable,
    add_entry: Callable = lambda *_args, **_kwargs: None,
    spawn: Callable[[Callable[[], None]], None] = _spawn,
) -> Blueprint:
    blueprint = Blueprint("photoshoot", __name__)

    def options() -> dict:
        try:
            provider = select_provider()
            provider_name = provider.name
            accepts_user_key = provider.name == "gemini" and provider.accepts_user_key
        except ValueError:
            provider_name, accepts_user_key = "higgsfield", False
        return {
            "provider": provider_name,
            "accepts_user_key": accepts_user_key,
            "vibes": [
                {"id": vibe.id, "label": vibe.label, "blurb": vibe.blurb, "swatch": list(vibe.swatch)}
                for vibe in photoshoot.VIBES.values()
            ],
            "shots": [
                {"id": shot.id, "label": shot.label, "use": shot.use, "aspect": shot.aspect}
                for shot in photoshoot.SHOTS
            ],
            "tiers": {tier: CREDIT_WEIGHT_BY_TIER[tier] for tier in photoshoot.PACK_TIERS},
            "default_vibe": photoshoot.DEFAULT_VIBE,
            "default_tier": photoshoot.DEFAULT_TIER,
        }

    def refund_pack(pack: dict, output: dict) -> None:
        photoshoot.refund_output_once(auth_db, pack, output)

    @blueprint.get("/shoot")
    def shoot_page():
        return render_template("shoot.html", options=options())

    @blueprint.get("/api/shoot/options")
    def shoot_options():
        return jsonify(options())

    @blueprint.post("/api/shoot")
    @rate_limited
    def start_shoot():
        access_error = require_access()
        if access_error is not None:
            return access_error
        owner_id = current_actor_id()
        form = request.form

        # ── Validate everything before any credit moves ─────────────────
        vibe = form.get("vibe") or photoshoot.DEFAULT_VIBE
        if vibe not in photoshoot.VIBES:
            return jsonify({"error": "Unknown vibe", "valid_vibes": list(photoshoot.VIBES)}), 400
        tier = form.get("tier") or photoshoot.DEFAULT_TIER
        if tier not in photoshoot.PACK_TIERS:
            return jsonify({"error": "Unknown quality", "valid_tiers": list(photoshoot.PACK_TIERS)}), 400
        shot_ids = photoshoot.parse_shots(form.get("shots"))
        if shot_ids is None:
            return jsonify({"error": "Choose at least one shot", "valid_shots": list(photoshoot.SHOT_IDS)}), 400
        note = photoshoot.clean_note(form.get("note", ""))
        if "image" not in request.files:
            return jsonify({"error": "Add a product photo to start"}), 400
        if store.owner_has_active(owner_id):
            return jsonify({
                "error": "A shoot is already running",
                "message": "Wait for your current shoot to finish, then start the next one.",
            }), 409
        try:
            provider = select_provider()
        except ValueError:
            return jsonify({
                "error": "Photoshoot is not set up yet",
                "message": "The image service isn't configured on this server. No credits were charged.",
            }), 503
        if provider.name == "higgsfield":
            try:
                daily_limit = float(os.environ.get("CREATIVE_DAILY_LIMIT") or "5")
            except ValueError:
                daily_limit = 5.0
            rejection = check_daily_limit(
                cost_db(), estimated_count=len(shot_ids), tier=tier, daily_limit=daily_limit,
                tier_models={tier: ("higgsfield", tier)},
                price_card={"higgsfield": {tier: higgsfield_cost(tier)}}, lock=cost_lock,
            )
            limit_error = (jsonify(rejection), 429) if rejection else None
        else:
            limit_error = enforce_daily_limit(len(shot_ids), tier)
        if limit_error is not None:
            return limit_error
        try:
            product_path = save_upload(request.files["image"], "photoshoot")
            validate_foreground_file(product_path)
        except (ValueError, CompositeInputError, OSError) as error:
            message = str(error) if isinstance(error, (ValueError, CompositeInputError)) else ""
            return jsonify({
                "error": "We couldn't use that photo",
                "message": message or "Try a PNG or JPG of the product on a plain background.",
            }), 400

        # ── Charge: outputs x tier weight, all-or-nothing ───────────────
        outputs = len(shot_ids)
        cost = credits_for(tier, outputs)
        session = current_session()
        if provider.accepts_user_key:
            # Gemini: the caller's own key pays, else credits (same rule as /api/generate).
            api_key, error, charged = require_api_key(credits=cost)
            if error is not None:
                return error
        else:
            # Higgsfield runs on the server's account, so it always costs credits.
            if not session:
                return jsonify({
                    "error": "Sign in to start a shoot",
                    "message": "Create a free account to get trial credits.",
                }), 401
            ok, _remaining = spend_credits(session["user_id"], cost)
            if not ok:
                balance = (current_session() or {}).get("credits_remaining", 0)
                return jsonify({
                    "error": "Out of credits" if balance == 0 else "Not enough credits",
                    "message": f"This shoot needs {cost} credits and you have {balance}.",
                    "credits_required": cost,
                    "credits_remaining": balance,
                }), 402
            api_key, charged = "", True
        user_id = session["user_id"] if (session and charged) else None

        pack = photoshoot.new_pack(
            owner_id=owner_id,
            user_id=user_id,
            vibe=vibe, tier=tier, note=note, shot_ids=shot_ids,
            provider=provider.name,
            credits_each=CREDIT_WEIGHT_BY_TIER[tier],
            charged=charged,
        )
        store.save(pack)
        store.live.add(pack["id"])

        def record_in_library(state: dict, output: dict) -> None:
            if state["provider"] == "higgsfield" and output.get("model") != "local-cutout":
                track_cost(cost_db(), {"higgsfield": {tier: higgsfield_cost(tier)}},
                           "higgsfield", tier, 1, cost_lock)
            # Shows the shot in History/Library; the pack itself never depends on it.
            try:
                add_entry("sess_" + state["id"][3:11], {
                    "type": "photoshoot",
                    "prompt": f"{photoshoot.VIBES[state['vibe']].label} · {output['label']}",
                    "image_url": output["url"],
                    "model": output.get("model", ""),
                    "note": output["aspect"],
                }, state["owner_id"])
            except Exception:
                logger.exception("photoshoot: could not add %s to the library", state["id"])

        def work():
            photoshoot.run_pack(
                store, pack["id"],
                provider=provider, api_key=api_key, product_path=Path(product_path),
                output_dir=get_output_dir(), to_image_url=to_image_url,
                refund_output=refund_pack, on_output_done=record_in_library,
            )

        spawn(work)
        return jsonify(photoshoot.public_view(store.load(pack["id"]))), 202

    @blueprint.get("/api/shoot/<pack_id>")
    @rate_limited
    def shoot_status(pack_id):
        # Polling is free: never spend credits here.
        access_error = require_access()
        if access_error is not None:
            return access_error
        pack = store.get(pack_id, current_actor_id())
        if not pack:
            return jsonify({"error": "Shoot not found"}), 404
        pack = photoshoot.recover_if_orphaned(store, pack, refund_pack)
        return jsonify(photoshoot.public_view(pack))

    @blueprint.get("/api/shoot/<pack_id>/download")
    @rate_limited
    def shoot_download(pack_id):
        access_error = require_access()
        if access_error is not None:
            return access_error
        pack = store.get(pack_id, current_actor_id())
        if not pack:
            return jsonify({"error": "Shoot not found"}), 404
        archive = photoshoot.build_zip(pack) if pack["status"] not in photoshoot.ACTIVE else None
        if archive is None:
            return jsonify({"error": "Nothing to download yet"}), 409
        return send_file(archive, mimetype="application/zip", as_attachment=True,
                         download_name=archive.name)

    return blueprint
