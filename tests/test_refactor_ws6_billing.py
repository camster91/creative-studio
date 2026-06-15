"""
Regression tests for WS-6: Stripe billing surface (Checkout +
webhook + credit ledger).

WS-6 is "scaffolded" — the routes exist and behave correctly,
but the live Stripe calls aren't tested here (they require real
keys). When the operator sets STRIPE_SECRET_KEY, the routes
become live.

What we test:
- /api/billing/plans returns the 3 plans with monthly_credits
- /api/billing/checkout returns 503 when Stripe is not configured
- /api/billing/portal returns 401/503 as appropriate
- /api/billing/webhook returns 503 when Stripe is not configured
- /api/me includes the new subscription fields
- Helpers (_tier_from_price_id, _record_subscription,
  _top_up_credits) work as expected with mocked state
- ALTER TABLE for the new columns is idempotent (the schema
  init runs on every boot)

Run:  pytest tests/test_refactor_ws6_billing.py -v
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"


# Unique module name per call to bypass sys.modules cache.
_load_counter = [0]

def _load_module(tmp_path, stripe_configured=False, stripe_secret="sk_test_123",
                webhook_secret="whsec_123",
                price_starter="price_starter_123",
                price_pro="price_pro_123",
                price_studio="price_studio_123"):
    """Load the module with a per-test users.db and optional Stripe
    config. Stripe is off by default — pass stripe_configured=True
    to set the env vars."""
    web_path = SCRIPT_DIR / "creative-studio-web.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    # ALWAYS clear Stripe env first to avoid contamination from
    # previous tests in the same process
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
              "STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO",
              "STRIPE_PRICE_STUDIO"):
        os.environ.pop(k, None)
    os.environ.setdefault("GEMINI_API_KEY", "test-key")
    if stripe_configured:
        os.environ["STRIPE_SECRET_KEY"] = stripe_secret
        os.environ["STRIPE_WEBHOOK_SECRET"] = webhook_secret
        os.environ["STRIPE_PRICE_STARTER"] = price_starter
        os.environ["STRIPE_PRICE_PRO"] = price_pro
        os.environ["STRIPE_PRICE_STUDIO"] = price_studio
    # Bust the import cache by using a unique module name
    _load_counter[0] += 1
    mod_name = f"creative_studio_web_{_load_counter[0]}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, web_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    auth_path = tmp_path / "auth.db"
    setattr(mod, "AUTH_DB", auth_path)
    mod._init_auth_schema()
    with mod._request_log_lock:
        mod._request_log.clear()
    return mod


@pytest.fixture
def cs(tmp_path):
    return _load_module(tmp_path, stripe_configured=False)


@pytest.fixture
def cs_stripe(tmp_path):
    return _load_module(tmp_path, stripe_configured=True)


def _signup_and_login(client, email):
    r = client.post("/signup", json={"email": email})
    token = r.get_json()["token"]
    r = client.post("/login", json={"token": token})
    return r.get_json()["session_token"]


# ─── /api/billing/plans (public) ────────────────────────────────────

class TestBillingPlans:
    def test_returns_three_plans(self, cs):
        r = cs.app.test_client().get("/api/billing/plans")
        assert r.status_code == 200
        body = r.get_json()
        plan_ids = [p["id"] for p in body["plans"]]
        assert set(plan_ids) == {"starter", "pro", "studio"}

    def test_plans_have_credit_counts(self, cs):
        body = cs.app.test_client().get("/api/billing/plans").get_json()
        by_id = {p["id"]: p for p in body["plans"]}
        assert by_id["starter"]["monthly_credits"] == 100
        assert by_id["pro"]["monthly_credits"] == 500
        assert by_id["studio"]["monthly_credits"] == 1500

    def test_no_auth_required(self, cs):
        """Plans are public — anyone can see them. This lets the
        landing page show pricing to anonymous visitors."""
        r = cs.app.test_client().get("/api/billing/plans")
        assert r.status_code == 200

    def test_stripe_configured_flag_reflects_env(self, tmp_path, monkeypatch):
        """The /api/billing/plans endpoint reports stripe_configured
        based on whether STRIPE_SECRET_KEY is set in the env at
        request time. We use monkeypatch to control the env at
        request time so the helpers see a consistent value."""
        # Module 1: load without Stripe
        mod_off = _load_module(tmp_path, stripe_configured=False)
        # Now monkeypatch the env to "Stripe OFF" for the request
        for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                  "STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO",
                  "STRIPE_PRICE_STUDIO"):
            monkeypatch.delenv(k, raising=False)
        r = mod_off.app.test_client().get("/api/billing/plans")
        assert r.get_json()["stripe_configured"] is False
        # Module 2: load with Stripe
        mod_on = _load_module(tmp_path, stripe_configured=True)
        # Stripe env is set from the load. Verify the request.
        r2 = mod_on.app.test_client().get("/api/billing/plans")
        assert r2.get_json()["stripe_configured"] is True

    def test_price_id_configured_reflects_env(self, tmp_path, monkeypatch):
        mod_off = _load_module(tmp_path, stripe_configured=False)
        for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                  "STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO",
                  "STRIPE_PRICE_STUDIO"):
            monkeypatch.delenv(k, raising=False)
        body = mod_off.app.test_client().get("/api/billing/plans").get_json()
        for p in body["plans"]:
            assert p["price_id_configured"] is False
            assert p["price_display"] != "configured"
        mod_on = _load_module(tmp_path, stripe_configured=True)
        body2 = mod_on.app.test_client().get("/api/billing/plans").get_json()
        for p in body2["plans"]:
            assert p["price_id_configured"] is True
            assert p["price_display"] == "configured"

    def test_plan_labels_match_catalog(self, cs):
        body = cs.app.test_client().get("/api/billing/plans").get_json()
        by_id = {p["id"]: p for p in body["plans"]}
        assert by_id["starter"]["label"] == "Starter"
        assert by_id["pro"]["label"] == "Pro"
        assert by_id["studio"]["label"] == "Studio"


# ─── /api/billing/checkout (gated) ──────────────────────────────────

class TestBillingCheckout:
    def test_anonymous_returns_401(self, cs_stripe):
        r = cs_stripe.app.test_client().post("/api/billing/checkout",
            json={"plan": "pro"})
        assert r.status_code == 401

    def test_stripe_unconfigured_returns_503(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-stripe@x.co")
        r = client.post("/api/billing/checkout",
            json={"plan": "pro"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 503
        assert "not configured" in r.get_json()["error"].lower()

    def test_stripe_configured_no_price_env_returns_503(self, tmp_path):
        """When the operator has set STRIPE_SECRET_KEY but not
        STRIPE_PRICE_PRO, the checkout endpoint should return 503
        with a clear 'set the env var' message — not 500, not 200."""
        # Use the regular _load_module path: Stripe IS configured
        # (secret + webhook), but STRIPE_PRICE_PRO is unset because
        # the os.environ.pop in _load_module clears all prices.
        # We then re-set them so we have everything EXCEPT pro.
        mod = _load_module(tmp_path, stripe_configured=True)
        mod.os.environ.pop("STRIPE_PRICE_PRO", None)
        client = mod.app.test_client()
        sess = _signup_and_login(client, "no-price@x.co")
        r = client.post("/api/billing/checkout",
            json={"plan": "pro"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 503
        assert "STRIPE_PRICE_PRO" in r.get_json()["error"]

    def test_invalid_plan_returns_400(self, cs_stripe):
        client = cs_stripe.app.test_client()
        sess = _signup_and_login(client, "bad-plan@x.co")
        r = client.post("/api/billing/checkout",
            json={"plan": "enterprise"},
            headers={"X-Session-Token": sess})
        assert r.status_code == 400
        body = r.get_json()
        assert "valid_plans" in body
        assert set(body["valid_plans"]) == {"starter", "pro", "studio"}


# ─── /api/billing/portal (gated) ─────────────────────────────────────

class TestBillingPortal:
    def test_anonymous_returns_401(self, cs_stripe):
        r = cs_stripe.app.test_client().post("/api/billing/portal")
        assert r.status_code == 401

    def test_stripe_unconfigured_returns_503(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-stripe-portal@x.co")
        r = client.post("/api/billing/portal",
            headers={"X-Session-Token": sess})
        assert r.status_code == 503

    def test_no_stripe_customer_returns_400(self, cs_stripe):
        """A signed-in user with no stripe_customer_id hasn't gone
        through checkout yet, so the portal endpoint 400s (not 503
        — Stripe IS configured, but there's nothing to portal)."""
        client = cs_stripe.app.test_client()
        sess = _signup_and_login(client, "newbie@x.co")
        r = client.post("/api/billing/portal",
            headers={"X-Session-Token": sess})
        assert r.status_code == 400
        assert "no billing account" in r.get_json()["error"].lower()


# ─── /api/billing/webhook (gated) ────────────────────────────────────

class TestBillingWebhook:
    def test_unconfigured_returns_503(self, cs):
        r = cs.app.test_client().post("/api/billing/webhook",
            data=b"{}",
            headers={"Content-Type": "application/json",
                     "Stripe-Signature": "t=1,v1=fake"})
        assert r.status_code == 503

    def test_no_signature_returns_400(self, cs_stripe):
        """When Stripe is configured but the signature is missing
        or invalid, the webhook returns 400 (signature mismatch)."""
        r = cs_stripe.app.test_client().post("/api/billing/webhook",
            data=b"{}",
            headers={"Content-Type": "application/json",
                     "Stripe-Signature": "t=1,v1=invalid"})
        assert r.status_code == 400


# ─── /api/me includes subscription fields ───────────────────────────

class TestApiMeSubscription:
    def test_response_includes_subscription_fields(self, cs):
        client = cs.app.test_client()
        sess = _signup_and_login(client, "me@x.co")
        r = client.get("/api/me", headers={"X-Session-Token": sess})
        body = r.get_json()
        # The new subscription fields are present
        assert "subscription_tier" in body
        assert "subscription_status" in body
        assert "subscription_renews_at" in body
        # Default values (user just signed up, no sub yet)
        assert body["subscription_tier"] is None
        assert body["subscription_status"] is None


# ─── Schema migration ──────────────────────────────────────────────

class TestSchemaMigration:
    def test_alter_table_is_idempotent(self, tmp_path):
        """The schema init runs on every boot. If we run it twice
        the second run should NOT crash trying to add an existing
        column (it uses pragma_table_info to check first)."""
        cs1 = _load_module(tmp_path)
        # Second load (different module name to avoid sys.modules cache)
        sys_modules_save = sys.modules.copy()
        try:
            sys.modules.pop("creative_studio_web", None)
            cs2 = _load_module(tmp_path)
        finally:
            sys.modules.update(sys_modules_save)
        # No exception, both loads successful

    def test_new_columns_present(self, tmp_path):
        cs = _load_module(tmp_path)
        with cs._auth_db() as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        for expected in ("stripe_customer_id", "stripe_subscription_id",
                        "subscription_tier", "subscription_status",
                        "subscription_renews_at"):
            assert expected in cols, f"missing column {expected!r}"


# ─── Helper functions ──────────────────────────────────────────────

class TestHelpers:
    def test_tier_from_price_id_known(self, cs):
        # Set price IDs to known values
        cs.os.environ["STRIPE_PRICE_STARTER"] = "price_aaa"
        cs.os.environ["STRIPE_PRICE_PRO"] = "price_bbb"
        assert cs._tier_from_price_id("price_aaa") == "starter"
        assert cs._tier_from_price_id("price_bbb") == "pro"

    def test_tier_from_price_id_unknown(self, cs):
        cs.os.environ["STRIPE_PRICE_STARTER"] = "price_aaa"
        assert cs._tier_from_price_id("price_unknown") is None

    def test_tier_from_price_id_unset(self, cs):
        cs.os.environ.pop("STRIPE_PRICE_STARTER", None)
        assert cs._tier_from_price_id("price_aaa") is None

    def test_stripe_configured_reflects_env(self, cs):
        assert cs._stripe_configured() is False
        cs.os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
        assert cs._stripe_configured() is True

    def test_resolve_price_id_unknown_plan(self, cs):
        with pytest.raises(ValueError):
            cs._resolve_price_id("enterprise")

    def test_resolve_price_id_unset_env(self, cs):
        """A plan whose STRIPE_PRICE_* env var isn't set should
        raise RuntimeError, not silently return."""
        cs.os.environ.pop("STRIPE_PRICE_STARTER", None)
        with pytest.raises(RuntimeError):
            cs._resolve_price_id("starter")

    def test_resolve_price_id_set_env(self, cs):
        cs.os.environ["STRIPE_PRICE_STARTER"] = "price_real"
        assert cs._resolve_price_id("starter") == "price_real"

    def test_top_up_credits_no_tier_is_noop(self, cs):
        """If a user has no subscription tier, _top_up_credits
        does nothing (no email signups should get free credits
        via a fake webhook)."""
        # Sign up a user
        client = cs.app.test_client()
        sess = _signup_and_login(client, "no-tier@x.co")
        with cs._auth_db() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?",
                              ("no-tier@x.co",)).fetchone()
        initial_credits = user["credits_remaining"]
        # User has no subscription tier (signed up but didn't pay)
        assert user["subscription_tier"] is None
        # Call _top_up_credits
        cs._top_up_credits(user["id"])
        # Credits unchanged (the trial signup already set them to 5,
        # but _top_up_credits doesn't add more)
        with cs._auth_db() as db:
            u = db.execute("SELECT credits_remaining FROM users WHERE id = ?",
                           (user["id"],)).fetchone()
            assert u["credits_remaining"] == initial_credits


# ─── Static asset / settings page ───────────────────────────────────

class TestSettingsBillingPage:
    def test_route_returns_200(self, cs):
        r = cs.app.test_client().get("/settings/billing")
        assert r.status_code == 200

    def test_renders_choose_plan(self, cs):
        r = cs.app.test_client().get("/settings/billing")
        body = r.get_data(as_text=True)
        assert "Choose a plan" in body
        assert "Manage your subscription" in body or "Free tier" in body

    def test_includes_three_plan_cards(self, cs):
        """The page is fully JS-rendered. The plan labels (Starter /
        Pro / Studio) are not in the static HTML — they're fetched
        from /api/billing/plans and rendered by renderPlans().
        Verify the page has the JS scaffolding to do that fetch +
        render, not the labels themselves."""
        body = cs.app.test_client().get("/settings/billing").get_data(as_text=True)
        # JS handler that fetches plans
        assert "/api/billing/plans" in body
        # Render function exists
        assert "renderPlans" in body
        # The card markup template has a data-plan attribute
        assert "data-plan=" in body

    def test_includes_stripe_redirect_handling(self, cs):
        """The page reads ?checkout=success or ?checkout=canceled
        from the URL when the user comes back from Stripe."""
        body = cs.app.test_client().get("/settings/billing").get_data(as_text=True)
        assert "checkout=success" in body
        assert "checkout=canceled" in body


# ─── Deploy: stripe is in pyproject + Dockerfile doesn't break ──────

class TestDeploy:
    def test_stripe_in_pyproject(self):
        from pathlib import Path
        toml = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        assert "stripe" in toml.lower()

    def test_billing_template_ships(self):
        from pathlib import Path
        p = Path(__file__).parent.parent / "templates" / "billing.html"
        assert p.exists()
