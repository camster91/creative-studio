"""
Credit metering on the non-/api/generate generation endpoints, plus the
out-of-credits message for trial users vs subscribers.

- /api/variations, /api/scene-set, /api/composite: images x tier weight.
- /api/refine, /api/variations/<key>/refine, /api/chat: one image at the
  request tier.
- Invalid input is rejected before charging; failed images are refunded.

Every provider call is stubbed; nothing here touches the network.

Run:  pytest tests/test_credit_metering_endpoints.py -v
"""
from io import BytesIO

import pytest
from PIL import Image

from creative_studio_app.billing import CREDIT_WEIGHT_BY_TIER as W
from test_credit_metering import _balance, _load_module, _login


def _png():
    buffer = BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 40)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


@pytest.fixture
def cs(tmp_path, monkeypatch):
    mod = _load_module(tmp_path, monkeypatch)
    mod.calls = []
    mod.fail = set()  # indexes (0-based call order) that should fail

    def result(tier=None):
        index = len(mod.calls)
        mod.calls.append(tier)
        if index in mod.fail:
            return {"error": "provider failed", "error_code": "provider_failed"}
        return {"url": f"/image/stub-{index}.png", "cost": 0.05, "model": "stub"}

    mod.run_cli_refine = lambda _path, _changes, _key, tier: [result(tier)]
    mod.run_cli_refine_from_variation = lambda **kw: [result(kw["tier"])]
    mod.run_cli_variations = lambda _key, **kw: (
        [result(kw["tier"]) for _ in range(kw["count"])], "var-abc"
    )
    mod.run_cli_composite = lambda *_a, tier="quality", **_kw: [result(tier)]
    mod.run_cli_chat_turn = lambda _key, **kw: ([result(kw["tier"])], {"turn": 1})
    return mod


# ─── /api/variations ─────────────────────────────────────────────────

class TestVariations:
    def test_charges_count_times_weight(self, cs):
        client, headers = _login(cs, "var@x.co", credits=20)
        r = client.post("/api/variations", headers=headers,
                        json={"prompt": "x", "count": 3, "tier": "quality"})
        assert r.status_code == 200
        assert _balance(cs, "var@x.co") == 20 - 3 * W["quality"]

    def test_failed_variations_refunded(self, cs):
        cs.fail = {1, 2}
        client, headers = _login(cs, "var-fail@x.co", credits=20)
        r = client.post("/api/variations", headers=headers,
                        json={"prompt": "x", "count": 4, "tier": "ultra"})
        assert r.status_code == 200
        assert _balance(cs, "var-fail@x.co") == 20 - 2 * W["ultra"]

    @pytest.mark.parametrize("body", [
        {"prompt": "x", "count": 9},
        {"prompt": "x", "count": "many"},
        {"prompt": "x", "count": 2, "tier": "mega"},
        {"count": 2},
    ])
    def test_invalid_input_is_free(self, cs, body):
        client, headers = _login(cs, "var-bad@x.co")
        r = client.post("/api/variations", headers=headers, json=body)
        assert r.status_code == 400
        assert _balance(cs, "var-bad@x.co") == 5
        assert cs.calls == []

    def test_insufficient_credits_spends_nothing(self, cs):
        client, headers = _login(cs, "var-short@x.co", credits=7)
        r = client.post("/api/variations", headers=headers,
                        json={"prompt": "x", "count": 8, "tier": "fast"})
        assert r.status_code == 402
        assert _balance(cs, "var-short@x.co") == 7
        assert cs.calls == []


# ─── /api/scene-set ──────────────────────────────────────────────────

class TestSceneSet:
    def test_charges_every_scene_at_generator_tier(self, cs):
        scenes = len(cs._SCENE_PROMPTS)
        client, headers = _login(cs, "scene@x.co", credits=30)
        r = client.post("/api/scene-set", headers=headers,
                        data={"product": (_png(), "p.png"), "tier": "fast"},
                        content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        assert cs.calls == ["quality"] * scenes
        assert _balance(cs, "scene@x.co") == 30 - scenes * W["quality"]

    def test_failed_scenes_refunded(self, cs):
        scenes = len(cs._SCENE_PROMPTS)
        cs.fail = {0, 1}
        client, headers = _login(cs, "scene-fail@x.co", credits=30)
        r = client.post("/api/scene-set", headers=headers,
                        data={"product": (_png(), "p.png")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
        assert _balance(cs, "scene-fail@x.co") == 30 - (scenes - 2) * W["quality"]

    def test_missing_product_is_free(self, cs):
        client, headers = _login(cs, "scene-bad@x.co", credits=30)
        r = client.post("/api/scene-set", headers=headers, data={},
                        content_type="multipart/form-data")
        assert r.status_code == 400
        assert _balance(cs, "scene-bad@x.co") == 30

    def test_insufficient_credits_spends_nothing(self, cs):
        client, headers = _login(cs, "scene-short@x.co")  # 5 credits
        r = client.post("/api/scene-set", headers=headers,
                        data={"product": (_png(), "p.png")},
                        content_type="multipart/form-data")
        assert r.status_code == 402
        assert _balance(cs, "scene-short@x.co") == 5
        assert cs.calls == []


# ─── /api/composite ──────────────────────────────────────────────────

class TestComposite:
    def test_charges_variations_times_weight(self, cs):
        client, headers = _login(cs, "comp@x.co", credits=20)
        r = client.post("/api/composite", headers=headers,
                        data={"product": (_png(), "p.png"), "prompt": "x",
                              "tier": "quality", "variations": "3"},
                        content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        assert _balance(cs, "comp@x.co") == 20 - 3 * W["quality"]

    def test_failure_refunded(self, cs):
        cs.fail = {0}
        client, headers = _login(cs, "comp-fail@x.co", credits=20)
        client.post("/api/composite", headers=headers,
                    data={"product": (_png(), "p.png"), "prompt": "x", "tier": "ultra"},
                    content_type="multipart/form-data")
        assert _balance(cs, "comp-fail@x.co") == 20

    def test_unknown_tier_is_free(self, cs):
        client, headers = _login(cs, "comp-bad@x.co")
        r = client.post("/api/composite", headers=headers,
                        data={"product": (_png(), "p.png"), "prompt": "x", "tier": "mega"},
                        content_type="multipart/form-data")
        assert r.status_code == 400
        assert _balance(cs, "comp-bad@x.co") == 5


# ─── Single-image endpoints ──────────────────────────────────────────

class TestSingleImageEndpoints:
    def test_refine_charges_tier_weight(self, cs):
        client, headers = _login(cs, "refine@x.co", credits=10)
        r = client.post("/api/refine", headers=headers,
                        json={"changes": "brighter", "tier": "ultra"})
        assert r.status_code == 200
        assert _balance(cs, "refine@x.co") == 10 - W["ultra"]

    def test_refine_default_tier_is_quality(self, cs):
        client, headers = _login(cs, "refine-default@x.co", credits=10)
        client.post("/api/refine", headers=headers, json={"changes": "brighter"})
        assert cs.calls == ["quality"]
        assert _balance(cs, "refine-default@x.co") == 10 - W["quality"]

    def test_refine_failure_refunded(self, cs):
        cs.fail = {0}
        client, headers = _login(cs, "refine-fail@x.co", credits=10)
        client.post("/api/refine", headers=headers, json={"changes": "x", "tier": "ultra"})
        assert _balance(cs, "refine-fail@x.co") == 10

    def test_refine_invalid_input_is_free(self, cs):
        client, headers = _login(cs, "refine-bad@x.co")
        assert client.post("/api/refine", headers=headers, json={}).status_code == 400
        assert client.post("/api/refine", headers=headers,
                           json={"changes": "x", "tier": "mega"}).status_code == 400
        assert _balance(cs, "refine-bad@x.co") == 5

    def test_refine_variation_charges_tier_weight(self, cs):
        client, headers = _login(cs, "refvar@x.co", credits=10)
        r = client.post("/api/variations/var-abc/refine", headers=headers,
                        json={"pick": 1, "changes": "x", "tier": "quality"})
        assert r.status_code == 200
        assert _balance(cs, "refvar@x.co") == 10 - W["quality"]

    def test_chat_turn_charges_tier_weight(self, cs):
        client, headers = _login(cs, "chat@x.co", credits=10)
        r = client.post("/api/chat", headers=headers, json={"prompt": "x", "tier": "ultra"})
        assert r.status_code == 200
        assert _balance(cs, "chat@x.co") == 10 - W["ultra"]

    def test_chat_failure_refunded_and_bad_input_free(self, cs):
        cs.fail = {0}
        client, headers = _login(cs, "chat-fail@x.co", credits=10)
        client.post("/api/chat", headers=headers, json={"prompt": "x", "tier": "quality"})
        assert client.post("/api/chat", headers=headers, json={}).status_code == 400
        assert client.post("/api/chat", headers=headers,
                           json={"prompt": "x", "tier": "mega"}).status_code == 400
        assert _balance(cs, "chat-fail@x.co") == 10


# ─── Out-of-credits wording ──────────────────────────────────────────

class TestOutOfCreditsMessage:
    def _exhausted(self, cs, email, tier=None):
        client, headers = _login(cs, email, credits=0)
        if tier:
            with cs._auth_db() as db:
                db.execute("UPDATE users SET subscription_tier = ? WHERE email = ?", (tier, email))
                db.commit()
        r = client.post("/api/generate", headers=headers, json={"prompt": "x", "tier": "fast"})
        assert r.status_code == 402
        return r.get_json()

    def test_trial_user_sees_trial_wording(self, cs):
        body = self._exhausted(cs, "trial-out@x.co")
        assert body["error"] == "Out of credits"
        assert "trial" in body["message"].lower()

    def test_subscriber_does_not_see_trial_wording(self, cs):
        body = self._exhausted(cs, "sub-out@x.co", tier="pro")
        assert body["error"] == "Out of credits"
        assert "trial" not in body["message"].lower()
        assert "renews" in body["message"]
