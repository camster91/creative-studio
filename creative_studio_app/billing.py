"""Stripe configuration, subscription state, and idempotent credit renewal."""

import os
from datetime import datetime
from pathlib import Path

from .auth import connect, now_iso


# Credits charged per generated image, by quality tier. One credit covers
# roughly $0.05 of provider cost, rounded up: fast ~$0.02, balanced ~$0.045,
# quality ~$0.09, ultra ~$0.24 (see COSTS / _TIER_MODEL in the web script).
CREDIT_WEIGHT_BY_TIER = {
    "fast": 1,
    "balanced": 1,
    "quality": 2,
    "ultra": 5,
}


def credits_for(tier: str, images: int) -> int:
    return CREDIT_WEIGHT_BY_TIER[tier] * images


LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_")


def configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def live_mode_blocked() -> bool:
    """Fail closed on live keys until real payments are explicitly enabled."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    return key.startswith(LIVE_KEY_PREFIXES) and os.environ.get("PHOTOGEN_ALLOW_LIVE_STRIPE") != "1"


def stripe_api(stripe_module):
    if not configured():
        raise RuntimeError("Stripe not configured (STRIPE_SECRET_KEY not set)")
    if live_mode_blocked():
        raise RuntimeError("Live Stripe key refused (set PHOTOGEN_ALLOW_LIVE_STRIPE=1)")
    stripe_module.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe_module


def resolve_price_id(plan: str, plans: dict) -> str:
    if plan not in plans:
        raise ValueError(f"Unknown plan: {plan!r}")
    environment_name = plans[plan]["price_id_env"]
    price_id = os.environ.get(environment_name, "").strip()
    if not price_id:
        raise RuntimeError(
            f"Plan {plan!r} not configured: set {environment_name} in /root/.env.photogen"
        )
    return price_id


def tier_from_price_id(price_id: str, plans: dict) -> str | None:
    for plan_id, plan in plans.items():
        configured_price = os.environ.get(plan["price_id_env"], "").strip()
        if configured_price and configured_price == price_id:
            return plan_id
    return None


def get_or_create_customer(path: Path, stripe, user: dict) -> str:
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    customer = stripe.Customer.create(
        email=user["email"], metadata={"photogen_user_id": user["id"]}
    )
    customer_id = customer["id"]
    with connect(path) as database:
        database.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (customer_id, user["id"]),
        )
        database.commit()
    return customer_id


def record_subscription(path: Path, user_id: str, subscription: dict, plans: dict) -> None:
    status = subscription.get("status", "active")
    price_id = (subscription.get("items", {}).get("data") or [{}])[0].get("price", {}).get("id")
    tier = tier_from_price_id(price_id, plans) if price_id else None
    period_end = subscription.get("current_period_end")
    renews_at = datetime.fromtimestamp(period_end).strftime("%Y-%m-%d %H:%M:%S") if period_end else None
    with connect(path) as database:
        previous = database.execute(
            "SELECT subscription_tier FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        newly_active = status in ("active", "trialing") and not (previous and previous["subscription_tier"])
        if newly_active and tier:
            database.execute(
                """UPDATE users SET stripe_subscription_id=?, subscription_tier=?,
                   subscription_status=?, subscription_renews_at=?, credits_remaining=?
                   WHERE id=?""",
                (subscription.get("id"), tier, status, renews_at, plans[tier]["monthly_credits"], user_id),
            )
        else:
            database.execute(
                """UPDATE users SET stripe_subscription_id=?, subscription_tier=?,
                   subscription_status=?, subscription_renews_at=? WHERE id=?""",
                (subscription.get("id"), tier, status, renews_at, user_id),
            )
        database.commit()


def cancel_subscription(path: Path, user_id: str) -> None:
    with connect(path) as database:
        database.execute(
            "UPDATE users SET subscription_tier=NULL, subscription_status='canceled' WHERE id=?",
            (user_id,),
        )
        database.commit()


def top_up_credits(path: Path, user_id: str, plans: dict) -> None:
    """Set, rather than increment, monthly credits so webhook retries are idempotent."""
    with connect(path) as database:
        user = database.execute(
            "SELECT subscription_tier FROM users WHERE id=?", (user_id,)
        ).fetchone()
        plan = plans.get(user["subscription_tier"]) if user and user["subscription_tier"] else None
        if not plan:
            return
        database.execute(
            """UPDATE users SET credits_remaining=?, credits_used_today=0,
               subscription_status='active' WHERE id=?""",
            (plan["monthly_credits"], user_id),
        )
        database.commit()


def mark_past_due(path: Path, user_id: str) -> None:
    with connect(path) as database:
        database.execute(
            "UPDATE users SET subscription_status='past_due' WHERE id=?", (user_id,)
        )
        database.commit()


def user_for_customer(path: Path, customer_id: str) -> str | None:
    with connect(path) as database:
        row = database.execute(
            "SELECT id FROM users WHERE stripe_customer_id=?", (customer_id,)
        ).fetchone()
    return row["id"] if row else None


def handle_event(path: Path, stripe, event: dict, plans: dict) -> None:
    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    user_id = (data.get("metadata") or {}).get("photogen_user_id") or None
    if event_type == "checkout.session.completed":
        subscription_id, customer_id = data.get("subscription"), data.get("customer")
        user_id = user_id or (user_for_customer(path, customer_id) if customer_id else None)
        if user_id and subscription_id:
            # Let lookup failures raise so the webhook returns non-2xx and Stripe retries.
            record_subscription(path, user_id, stripe.Subscription.retrieve(subscription_id), plans)
    elif event_type in ("customer.subscription.created", "customer.subscription.updated") and user_id:
        record_subscription(path, user_id, data, plans)
    elif event_type == "customer.subscription.deleted" and user_id:
        cancel_subscription(path, user_id)
    elif event_type == "invoice.payment_succeeded" and user_id:
        top_up_credits(path, user_id, plans)
    elif event_type == "invoice.payment_failed" and user_id:
        mark_past_due(path, user_id)


def event_already_processed(path: Path, event_id: str) -> bool:
    with connect(path) as database:
        row = database.execute(
            "SELECT 1 FROM stripe_events WHERE id = ?", (event_id,)
        ).fetchone()
    return row is not None


def mark_event_processed(path: Path, event_id: str, event_type: str) -> None:
    with connect(path) as database:
        database.execute(
            "INSERT OR IGNORE INTO stripe_events (id, type, processed_at) VALUES (?, ?, ?)",
            (event_id, event_type, now_iso()),
        )
        database.commit()


def process_event(path: Path, stripe, event: dict, plans: dict) -> bool:
    """Handle an event once. Returns False for an already-processed duplicate.

    The id is recorded only after handling succeeds, so a failed event is
    retried by Stripe. Handlers are set-based, so a rare concurrent duplicate
    delivery is harmless.
    """
    event_id = event.get("id")
    if event_id and event_already_processed(path, event_id):
        return False
    handle_event(path, stripe, event, plans)
    if event_id:
        mark_event_processed(path, event_id, event.get("type", ""))
    return True
