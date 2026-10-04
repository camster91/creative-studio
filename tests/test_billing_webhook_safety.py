"""
Regression tests for Stripe webhook safety and live-key refusal.

- A duplicate event id is processed once.
- A failed subscription lookup returns non-2xx (Stripe retries) and the
  event is not recorded as processed.
- Live keys (sk_live_/rk_live_) are refused at checkout, portal and
  webhook unless PHOTOGEN_ALLOW_LIVE_STRIPE=1.

Stripe is fully faked; nothing here touches the network.

Run:  pytest tests/test_billing_webhook_safety.py -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from creative_studio_app import billing

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
_load_counter = [0]


class FakeStripe:
    """Minimal stand-in for the stripe module."""

    def __init__(self):
        self.api_key = None
        self.next_event = None
        self.retrieve_calls = 0
        self.retrieve_error = None
        fake = self

        class Webhook:
            @staticmethod
            def construct_event(_payload, _signature, _secret):
                return fake.next_event

        class Subscription:
            @staticmethod
            def retrieve(subscription_id):
                fake.retrieve_calls += 1
                if fake.retrieve_error:
                    raise fake.retrieve_error
                return {
                    "id": subscription_id,
                    "status": "active",
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                    "current_period_end": 1900000000,
                }

        class Customer:
            @staticmethod
            def create(**_kwargs):
                return {"id": "cus_test"}

        class _Session:
            @staticmethod
            def create(**_kwargs):
                return {"id": "cs_test", "url": "https://checkout.example/cs_test"}

        class checkout:
            Session = _Session

        class billing_portal:
            Session = _Session

        self.Webhook = Webhook
        self.Subscription = Subscription
        self.Customer = Customer
        self.checkout = checkout
        self.billing_portal = billing_portal


def _load_module(tmp_path, monkeypatch, secret="sk_test_123"):
    monkeypatch.setenv("CREATIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", secret)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRICE_STARTER", "price_starter_test")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_test")
    monkeypatch.setenv("STRIPE_PRICE_STUDIO", "price_studio_test")
    monkeypatch.delenv("PHOTOGEN_ALLOW_LIVE_STRIPE", raising=False)
    sys.path.insert(0, str(SCRIPT_DIR))
    _load_counter[0] += 1
    name = f"creative_studio_web_webhook_{_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / "creative-studio-web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    # Keep the default AUTH_DB (under the tmp CREATIVE_DATA_DIR): the billing
    # blueprint captures that path at import time.
    mod._RATE_LIMIT = 1000
    mod.fake_stripe = FakeStripe()
    mod._stripe_lib = mod.fake_stripe
    return mod


@pytest.fixture
def cs(tmp_path, monkeypatch):
    return _load_module(tmp_path, monkeypatch)


def _login(mod, email):
    client = mod.app.test_client()
    token = client.post("/signup", json={"email": email}).get_json()["token"]
    session = client.post("/login", json={"token": token}).get_json()["session_token"]
    with mod._auth_db() as db:
        user_id = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    return client, {"X-Session-Token": session}, user_id


def _checkout_completed(event_id, user_id):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "subscription": "sub_123",
            "customer": "cus_test",
            "metadata": {"photogen_user_id": user_id},
        }},
    }


def _post_webhook(client):
    return client.post(
        "/api/billing/webhook", data=b"{}",
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=x"},
    )


def _user(mod, user_id):
    with mod._auth_db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


class TestWebhookIdempotency:
    def test_duplicate_event_processed_once(self, cs):
        client, _headers, user_id = _login(cs, "dup@x.co")
        cs.fake_stripe.next_event = _checkout_completed("evt_dup", user_id)
        assert _post_webhook(client).status_code == 200
        assert _user(cs, user_id)["credits_remaining"] == 100
        # The user spends some credits, then Stripe re-delivers the event.
        with cs._auth_db() as db:
            db.execute("UPDATE users SET credits_remaining = 40 WHERE id = ?", (user_id,))
            db.commit()
        assert _post_webhook(client).status_code == 200
        assert cs.fake_stripe.retrieve_calls == 1
        assert _user(cs, user_id)["credits_remaining"] == 40
        with cs._auth_db() as db:
            rows = db.execute("SELECT id FROM stripe_events").fetchall()
        assert [row["id"] for row in rows] == ["evt_dup"]

    def test_distinct_events_both_processed(self, cs):
        client, _headers, user_id = _login(cs, "distinct@x.co")
        cs.fake_stripe.next_event = _checkout_completed("evt_a", user_id)
        _post_webhook(client)
        cs.fake_stripe.next_event = _checkout_completed("evt_b", user_id)
        _post_webhook(client)
        assert cs.fake_stripe.retrieve_calls == 2


class TestWebhookFailuresAreRetried:
    def test_subscription_lookup_failure_returns_500(self, cs):
        client, _headers, user_id = _login(cs, "lookup-fail@x.co")
        cs.fake_stripe.next_event = _checkout_completed("evt_fail", user_id)
        cs.fake_stripe.retrieve_error = RuntimeError("stripe down")
        r = _post_webhook(client)
        assert r.status_code == 500
        assert _user(cs, user_id)["subscription_tier"] is None
        with cs._auth_db() as db:
            assert db.execute("SELECT COUNT(*) FROM stripe_events").fetchone()[0] == 0

    def test_retry_after_failure_succeeds(self, cs):
        client, _headers, user_id = _login(cs, "retry@x.co")
        cs.fake_stripe.next_event = _checkout_completed("evt_retry", user_id)
        cs.fake_stripe.retrieve_error = RuntimeError("stripe down")
        assert _post_webhook(client).status_code == 500
        cs.fake_stripe.retrieve_error = None
        assert _post_webhook(client).status_code == 200
        user = _user(cs, user_id)
        assert user["subscription_tier"] == "starter"
        assert user["credits_remaining"] == 100

    def test_failure_is_logged(self, cs, caplog):
        client, _headers, user_id = _login(cs, "logged@x.co")
        cs.fake_stripe.next_event = _checkout_completed("evt_log", user_id)
        cs.fake_stripe.retrieve_error = RuntimeError("stripe down")
        with caplog.at_level("ERROR"):
            _post_webhook(client)
        assert any("evt_log" in record.getMessage() for record in caplog.records)


class TestLiveKeyRefused:
    @pytest.mark.parametrize("secret", ["sk_live_abc", "rk_live_abc"])
    def test_live_key_blocked_without_flag(self, monkeypatch, secret):
        monkeypatch.setenv("STRIPE_SECRET_KEY", secret)
        monkeypatch.delenv("PHOTOGEN_ALLOW_LIVE_STRIPE", raising=False)
        assert billing.live_mode_blocked() is True
        with pytest.raises(RuntimeError):
            billing.stripe_api(FakeStripe())

    def test_live_key_allowed_with_flag(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")
        monkeypatch.setenv("PHOTOGEN_ALLOW_LIVE_STRIPE", "1")
        assert billing.live_mode_blocked() is False

    def test_test_key_never_blocked(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        monkeypatch.delenv("PHOTOGEN_ALLOW_LIVE_STRIPE", raising=False)
        assert billing.live_mode_blocked() is False

    def test_checkout_portal_webhook_refuse_live_key(self, tmp_path, monkeypatch):
        mod = _load_module(tmp_path, monkeypatch, secret="sk_live_abc")
        client, headers, user_id = _login(mod, "live@x.co")
        r = client.post("/api/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert r.status_code == 503
        assert "live stripe" in r.get_json()["error"].lower()
        r = client.post("/api/billing/portal", headers=headers)
        assert r.status_code == 503
        mod.fake_stripe.next_event = _checkout_completed("evt_live", user_id)
        assert _post_webhook(client).status_code == 503
        assert mod.fake_stripe.retrieve_calls == 0

    def test_checkout_proceeds_with_flag(self, tmp_path, monkeypatch):
        mod = _load_module(tmp_path, monkeypatch, secret="sk_live_abc")
        monkeypatch.setenv("PHOTOGEN_ALLOW_LIVE_STRIPE", "1")
        client, headers, _user_id = _login(mod, "live-ok@x.co")
        r = client.post("/api/billing/checkout", json={"plan": "pro"}, headers=headers)
        assert r.status_code == 200
        assert r.get_json()["session_id"] == "cs_test"
