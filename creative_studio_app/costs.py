"""Atomic cost accounting and daily-spend policy."""

import json
from datetime import datetime
from pathlib import Path


EMPTY_COSTS = {
    "total": 0.0,
    "by_model": {},
    "by_date": {},
    "session_count": 0,
    "image_count": 0,
}


def load_costs(path: Path) -> dict:
    if not path.exists():
        return {key: value.copy() if isinstance(value, dict) else value for key, value in EMPTY_COSTS.items()}
    return json.loads(path.read_text())


def save_costs(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def cost_for_tier(tier: str, tier_models: dict, price_card: dict) -> float:
    model, resolution = tier_models.get(
        tier, ("gemini-3.1-flash-image-preview", "1K")
    )
    prices = price_card.get(model, {})
    if isinstance(prices, dict):
        return prices.get(resolution, prices.get("1K", 0.04))
    return float(prices) if prices else 0.04


def track_cost(
    path: Path,
    price_card: dict,
    model: str,
    resolution: str,
    count: int,
    lock,
) -> float:
    """Atomically record generated-image spend and return the charge."""
    with lock:
        costs = load_costs(path)
        prices = price_card.get(model, {})
        if isinstance(prices, dict):
            unit = prices.get(resolution) or prices.get("1K") or 0.04
        else:
            unit = float(prices) if prices else 0.04
        charge = unit * count
        costs["total"] += charge
        costs["by_model"][model] = costs["by_model"].get(model, 0.0) + charge
        today = datetime.now().strftime("%Y-%m-%d")
        costs["by_date"][today] = costs["by_date"].get(today, 0.0) + charge
        costs["image_count"] += count
        save_costs(path, costs)
        return charge


def check_daily_limit(
    path: Path,
    *,
    estimated_count: int,
    tier: str,
    daily_limit: float,
    tier_models: dict,
    price_card: dict,
    lock,
) -> dict | None:
    """Return rejection metadata when estimated spend exceeds the cap."""
    if daily_limit <= 0:
        return None
    estimated_cost = cost_for_tier(tier, tier_models, price_card) * max(1, estimated_count)
    with lock:
        costs = load_costs(path)
        today = datetime.now().strftime("%Y-%m-%d")
        spent_today = float(costs.get("by_date", {}).get(today, 0.0))
        if spent_today + estimated_cost <= daily_limit:
            return None
        return {
            "error": (
                f"Daily limit ${daily_limit:.2f} reached. Spent today: "
                f"${spent_today:.2f}. Request would cost ${estimated_cost:.2f}. "
                "Set CREATIVE_DAILY_LIMIT to a higher value or wait until tomorrow."
            ),
            "spent_today": round(spent_today, 4),
            "limit": daily_limit,
            "est_cost": round(estimated_cost, 4),
        }
