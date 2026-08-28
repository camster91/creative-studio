"""Stripe billing Flask routes."""

import os
from collections.abc import Callable

from flask import Blueprint, jsonify, render_template, request


def create_blueprint(
    *,
    plans: dict,
    portal_return_url: str,
    auth_db_path,
    auth_db: Callable,
    current_session: Callable[[], dict | None],
    stripe_configured: Callable[[], bool],
    stripe_api: Callable,
    get_or_create_customer: Callable[[dict], str],
    resolve_price_id: Callable[[str], str],
    handle_event: Callable,
    rate_limited: Callable,
) -> Blueprint:
    blueprint = Blueprint("billing", __name__)

    @blueprint.get("/api/billing/plans")
    @rate_limited
    def billing_plans():
        available = []
        for plan_id, plan in plans.items():
            price_id = os.environ.get(plan["price_id_env"], "").strip()
            available.append(
                {
                    "id": plan_id,
                    "label": plan["label"],
                    "monthly_credits": plan["monthly_credits"],
                    "price_id_configured": bool(price_id),
                    "price_display": plan["default_price"] if not price_id else "configured",
                }
            )
        return jsonify(
            {"plans": available, "stripe_configured": stripe_configured()}
        )

    @blueprint.post("/api/billing/checkout")
    @rate_limited
    def billing_checkout():
        session = current_session()
        if not session:
            return jsonify({"error": "Sign in required"}), 401
        if not stripe_configured():
            return jsonify(
                {
                    "error": "Billing not configured",
                    "message": "Set STRIPE_SECRET_KEY on the host to enable paid plans.",
                }
            ), 503
        plan = ((request.json or {}).get("plan") or "").strip()
        if plan not in plans:
            return jsonify(
                {"error": "Invalid plan", "valid_plans": list(plans.keys())}
            ), 400
        try:
            price_id = resolve_price_id(plan)
        except RuntimeError:
            return jsonify({"error": "Billing plan is not configured"}), 503
        with auth_db() as database:
            user = database.execute(
                "SELECT * FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 401
        user = dict(user)
        try:
            customer_id = get_or_create_customer(user)
            stripe = stripe_api()
            checkout = stripe.checkout.Session.create(
                mode="subscription",
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=portal_return_url + "?checkout=success",
                cancel_url=portal_return_url + "?checkout=canceled",
                metadata={"photogen_user_id": user["id"], "plan": plan},
            )
        except Exception:
            return jsonify({"error": "Checkout creation failed"}), 500
        return jsonify(
            {"url": checkout["url"], "session_id": checkout["id"], "plan": plan}
        )

    @blueprint.post("/api/billing/portal")
    @rate_limited
    def billing_portal():
        session = current_session()
        if not session:
            return jsonify({"error": "Sign in required"}), 401
        if not stripe_configured():
            return jsonify({"error": "Billing not configured"}), 503
        with auth_db() as database:
            user = database.execute(
                "SELECT * FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
        if not user or not user["stripe_customer_id"]:
            return jsonify({"error": "No billing account yet"}), 400
        try:
            portal = stripe_api().billing_portal.Session.create(
                customer=user["stripe_customer_id"], return_url=portal_return_url
            )
        except Exception:
            return jsonify({"error": "Portal creation failed"}), 500
        return jsonify({"url": portal["url"]})

    @blueprint.post("/api/billing/webhook")
    def billing_webhook():
        if not stripe_configured():
            return "Stripe not configured", 503
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        if not webhook_secret:
            return "Webhook secret not configured", 503
        try:
            stripe = stripe_api()
            event = stripe.Webhook.construct_event(
                request.get_data(),
                request.headers.get("Stripe-Signature", ""),
                webhook_secret,
            )
        except Exception as error:
            return f"Invalid signature: {error}", 400
        handle_event(auth_db_path, stripe, event, plans)
        return "", 200

    @blueprint.get("/settings/billing")
    def billing_settings():
        return render_template("billing.html")

    return blueprint
