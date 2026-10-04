"""
One-button photoshoot pack (/shoot, /api/shoot*).

- Charges outputs x tier weight once, after validation, all-or-nothing.
- Refunds each failed output; polling and downloads are free.
- The Higgsfield adapter's HTTP flow, with a fake transport.

Every provider and HTTP call is stubbed; nothing here touches the network.

Run:  pytest tests/test_photoshoot.py -v
"""
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from creative_studio_app import photoshoot
from creative_studio_app.billing import CREDIT_WEIGHT_BY_TIER as W
from creative_studio_app.providers import GeminiCompositeProvider, ShotRequest, select_provider
from creative_studio_app.providers.higgsfield import HiggsfieldProvider
from test_credit_metering import _balance, _load_module, _login

ALL = len(photoshoot.SHOTS)


def _product_png(transparent=True):
    image = Image.new("RGBA", (200, 300), (0, 0, 0, 0) if transparent else (40, 90, 160, 255))
    for x in range(60, 140):
        for y in range(40, 260):
            image.putpixel((x, y), (220, 60, 30, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


class FakeProvider:
    """Stands in for Higgsfield: server-paid, writes a real PNG per shot."""

    name = "fake"
    accepts_user_key = False

    def __init__(self):
        self.calls = []
        self.fail = set()

    def render(self, request: ShotRequest, api_key: str) -> dict:
        shot = request.output_path.stem.split("-")[1]
        self.calls.append((shot, request.aspect, request.tier, api_key))
        if shot in self.fail:
            return {"error": "nope", "error_code": "provider_failed"}
        width, height = (int(part) * 40 for part in request.aspect.split(":"))
        Image.new("RGB", (width, height), (90, 90, 90)).save(request.output_path)
        return {"path": str(request.output_path), "model": "fake"}


@pytest.fixture
def cs(tmp_path, monkeypatch):
    mod = _load_module(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "outputs")
    mod.provider = FakeProvider()
    mod._select_photoshoot_provider = lambda: mod.provider
    mod._spawn_photoshoot = lambda work: work()  # run the pack inline
    return mod


def _shoot(client, headers, **form):
    data = {"image": (_product_png(form.pop("transparent", True)), "product.png"), **form}
    if form.get("no_image"):
        data.pop("image")
        data.pop("no_image")
    return client.post("/api/shoot", headers=headers, data=data, content_type="multipart/form-data")


# ─── Metering ─────────────────────────────────────────────────────────

class TestMetering:
    def test_default_pack_charges_every_output_at_tier_weight(self, cs):
        client, headers = _login(cs, "pack@x.co", credits=20)
        r = _shoot(client, headers)
        assert r.status_code == 202, r.get_json()
        body = r.get_json()
        assert body["credits_charged"] == ALL * W[photoshoot.DEFAULT_TIER]
        assert _balance(cs, "pack@x.co") == 20 - ALL * W[photoshoot.DEFAULT_TIER]

    @pytest.mark.parametrize("tier", photoshoot.PACK_TIERS)
    def test_tier_weight_applies_per_output(self, cs, tier):
        client, headers = _login(cs, f"{tier}@x.co", credits=100)
        assert _shoot(client, headers, tier=tier).status_code == 202
        assert _balance(cs, f"{tier}@x.co") == 100 - ALL * W[tier]

    def test_shot_subset_charges_only_those_outputs(self, cs):
        client, headers = _login(cs, "subset@x.co", credits=20)
        r = _shoot(client, headers, shots="hero,story", tier="quality")
        assert r.status_code == 202
        assert [o["id"] for o in r.get_json()["outputs"]] == ["hero", "story"]
        assert _balance(cs, "subset@x.co") == 20 - 2 * W["quality"]

    def test_failed_outputs_are_refunded_one_by_one(self, cs):
        cs.provider.fail = {"lifestyle", "story"}
        client, headers = _login(cs, "partial@x.co", credits=50)
        pack = _shoot(client, headers, tier="ultra").get_json()
        status = client.get(f"/api/shoot/{pack['pack_id']}", headers=headers).get_json()
        assert status["status"] == "partial"
        assert status["completed"] == ALL - 2
        assert status["credits_refunded"] == 2 * W["ultra"]
        assert _balance(cs, "partial@x.co") == 50 - (ALL - 2) * W["ultra"]

    def test_everything_failing_refunds_everything(self, cs):
        cs.provider.fail = set(photoshoot.SHOT_IDS)
        client, headers = _login(cs, "allfail@x.co", credits=10)
        pack = _shoot(client, headers, transparent=False).get_json()
        status = client.get(f"/api/shoot/{pack['pack_id']}", headers=headers).get_json()
        assert status["status"] == "failed"
        assert status["download_url"] is None
        assert _balance(cs, "allfail@x.co") == 10

    def test_exact_balance_is_enough(self, cs):
        client, headers = _login(cs, "exact@x.co", credits=ALL)
        assert _shoot(client, headers).status_code == 202
        assert _balance(cs, "exact@x.co") == 0

    def test_short_balance_spends_nothing(self, cs):
        client, headers = _login(cs, "short@x.co", credits=ALL - 1)
        r = _shoot(client, headers)
        assert r.status_code == 402
        assert r.get_json()["credits_required"] == ALL
        assert r.get_json()["credits_remaining"] == ALL - 1
        assert _balance(cs, "short@x.co") == ALL - 1
        assert cs.provider.calls == []

    def test_polling_and_download_are_free(self, cs):
        client, headers = _login(cs, "poll@x.co", credits=20)
        pack = _shoot(client, headers).get_json()
        after_charge = _balance(cs, "poll@x.co")
        for _ in range(5):
            assert client.get(f"/api/shoot/{pack['pack_id']}", headers=headers).status_code == 200
        assert client.get(f"/api/shoot/{pack['pack_id']}/download", headers=headers).status_code == 200
        assert _balance(cs, "poll@x.co") == after_charge


# ─── Validation happens before any charge ─────────────────────────────

class TestValidation:
    @pytest.mark.parametrize("form", [
        {"vibe": "vaporwave"},
        {"tier": "fast"},
        {"tier": "mega"},
        {"shots": "hero,billboard"},
        {"shots": " , "},
        {"no_image": True},
    ])
    def test_bad_input_is_free(self, cs, form):
        client, headers = _login(cs, "bad@x.co", credits=20)
        r = _shoot(client, headers, **form)
        assert r.status_code == 400, r.get_json()
        assert _balance(cs, "bad@x.co") == 20
        assert cs.provider.calls == []

    def test_unreadable_photo_is_free(self, cs):
        client, headers = _login(cs, "junk@x.co", credits=20)
        r = client.post("/api/shoot", headers=headers, content_type="multipart/form-data",
                        data={"image": (BytesIO(b"not an image"), "x.png")})
        assert r.status_code == 400
        assert _balance(cs, "junk@x.co") == 20

    def test_unconfigured_provider_is_free(self, cs):
        def broken():
            raise ValueError("Higgsfield key ID and secret are required")
        cs._select_photoshoot_provider = broken
        client, headers = _login(cs, "nocfg@x.co", credits=20)
        r = _shoot(client, headers)
        assert r.status_code == 503
        assert _balance(cs, "nocfg@x.co") == 20

    def test_signed_out_without_key_is_rejected(self, cs):
        r = _shoot(cs.app.test_client(), {})
        assert r.status_code == 402

    def test_server_paid_provider_needs_an_account(self, cs):
        r = _shoot(cs.app.test_client(), {"X-API-Key": "user-own-key"})
        assert r.status_code == 401
        assert cs.provider.calls == []

    def test_one_running_shoot_per_owner(self, cs):
        held = []
        cs._spawn_photoshoot = held.append  # leave the first pack running
        client, headers = _login(cs, "busy@x.co", credits=50)
        assert _shoot(client, headers).status_code == 202
        r = _shoot(client, headers)
        assert r.status_code == 409
        assert _balance(cs, "busy@x.co") == 50 - ALL


# ─── Pack contents, ownership, download ──────────────────────────────

class TestPack:
    def test_outputs_have_exact_sizes_and_local_cutout(self, cs):
        client, headers = _login(cs, "sizes@x.co", credits=20)
        pack = _shoot(client, headers, vibe="luxe").get_json()
        status = client.get(f"/api/shoot/{pack['pack_id']}", headers=headers).get_json()
        assert status["status"] == "done"
        assert {o["id"]: o["aspect"] for o in status["outputs"]} == {
            "hero": "16:9", "lifestyle": "4:5", "square": "1:1",
            "portrait": "4:5", "story": "9:16", "cutout": "1:1",
        }
        # Transparent product -> cutout made locally, provider not called for it.
        assert "cutout" not in [call[0] for call in cs.provider.calls]
        assert all(call[3] == "" for call in cs.provider.calls)  # server account, no user key
        cutout = next(o for o in status["outputs"] if o["id"] == "cutout")
        path = cs.OUTPUT_DIR / cutout["url"][len("/image/"):]
        with Image.open(path) as image:
            assert image.size == (2000, 2000)
            assert image.getpixel((5, 5)) == (255, 255, 255)
        assert "path" not in json.dumps(status)  # no server paths leak to the browser

    def test_opaque_photo_cutout_falls_back_to_provider(self, cs):
        client, headers = _login(cs, "opaque@x.co", credits=20)
        _shoot(client, headers, shots="cutout", transparent=False)
        assert [call[0] for call in cs.provider.calls] == ["cutout"]

    def test_other_users_cannot_see_or_download(self, cs):
        client, headers = _login(cs, "owner@x.co", credits=20)
        pack_id = _shoot(client, headers).get_json()["pack_id"]
        other, other_headers = _login(cs, "other@x.co", credits=20)
        assert other.get(f"/api/shoot/{pack_id}", headers=other_headers).status_code == 404
        assert other.get(f"/api/shoot/{pack_id}/download", headers=other_headers).status_code == 404

    def test_zip_has_every_delivered_image(self, cs):
        cs.provider.fail = {"hero"}
        client, headers = _login(cs, "zip@x.co", credits=20)
        pack_id = _shoot(client, headers).get_json()["pack_id"]
        r = client.get(f"/api/shoot/{pack_id}/download", headers=headers)
        assert r.status_code == 200
        names = zipfile.ZipFile(BytesIO(r.data)).namelist()
        assert len(names) == ALL - 1
        assert not any("hero" in name for name in names)

    def test_interrupted_pack_refunds_unfinished_outputs_once(self, cs):
        cs._spawn_photoshoot = lambda work: None  # simulate a restart: runner never starts
        client, headers = _login(cs, "restart@x.co", credits=20)
        pack_id = _shoot(client, headers, tier="quality").get_json()["pack_id"]
        assert _balance(cs, "restart@x.co") == 20 - ALL * W["quality"]
        cs._pack_store.live.clear()
        for _ in range(2):
            status = client.get(f"/api/shoot/{pack_id}", headers=headers).get_json()
        assert status["status"] == "failed"
        assert status["credits_refunded"] == ALL * W["quality"]
        assert _balance(cs, "restart@x.co") == 20

    def test_shoot_page_renders_vibes_and_default(self, cs):
        html = cs.app.test_client().get("/shoot").get_data(as_text=True)
        assert "Create my photoshoot" in html
        for vibe in photoshoot.VIBES.values():
            assert vibe.label in html
        assert f'value="{photoshoot.DEFAULT_VIBE}" checked' in html


# ─── Gemini path keeps BYOK behaviour ────────────────────────────────

class TestGeminiPath:
    @pytest.fixture
    def gemini(self, cs):
        cs.composite_calls = []

        def fake_composite(prompt, product_path, api_key, aspect, tier="quality", name_suffix=""):
            cs.composite_calls.append((api_key, aspect, tier, prompt))
            out = cs.OUTPUT_DIR / f"raw-{name_suffix}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1600, 900), (10, 10, 10)).save(out)
            return [{"path": str(out), "model": "gemini-stub"}]

        cs._select_photoshoot_provider = lambda: GeminiCompositeProvider(fake_composite)
        return cs

    def test_own_key_is_free_and_used(self, gemini):
        client = gemini.app.test_client()
        r = _shoot(client, {"X-API-Key": "user-own-key"}, shots="hero,story")
        assert r.status_code == 202
        assert r.get_json()["credits_charged"] == 0
        assert [call[0] for call in gemini.composite_calls] == ["user-own-key", "user-own-key"]

    def test_signed_in_without_key_pays_credits(self, gemini):
        client, headers = _login(gemini, "gem@x.co", credits=20)
        r = _shoot(client, headers, shots="hero,story,portrait")
        assert r.status_code == 202
        assert _balance(gemini, "gem@x.co") == 20 - 3 * W[photoshoot.DEFAULT_TIER]
        assert all(call[0] == "test-server-key" for call in gemini.composite_calls)
        # Gemini paints an empty set; the real product is pasted on top.
        assert all("Empty set" in call[3] for call in gemini.composite_calls)

    def test_output_is_cropped_to_exact_ratio(self, gemini, tmp_path):
        client, headers = _login(gemini, "crop@x.co", credits=20)
        pack_id = _shoot(client, headers, shots="story").get_json()["pack_id"]
        story = client.get(f"/api/shoot/{pack_id}", headers=headers).get_json()["outputs"][0]
        with Image.open(gemini.OUTPUT_DIR / story["url"][len("/image/"):]) as image:
            assert abs(image.width / image.height - 9 / 16) < 0.01


# ─── Higgsfield adapter ──────────────────────────────────────────────

class FakeHiggsfield:
    """Fake transport following docs.higgsfield.ai request shapes."""

    def __init__(self, final_status="completed", submit_errors=(), polls_before_done=1):
        self.log = []
        self.final_status = final_status
        self.submit_errors = list(submit_errors)
        self.polls_left = polls_before_done

    def __call__(self, method, url, headers, body, timeout):
        self.log.append({"method": method, "url": url, "headers": dict(headers),
                         "body": json.loads(body) if body and method == "POST" else body})
        if url.endswith("/files/generate-upload-url"):
            return 200, json.dumps({
                "public_url": "https://cdn.test/in.png",
                "upload_url": "https://storage.test/presigned",
                "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"},
            }).encode()
        if url == "https://storage.test/presigned":
            return 200, b""
        if url.endswith("/marketing-studio/image"):
            if self.submit_errors:
                return self.submit_errors.pop(0), b'{"detail": "x"}'
            return 200, json.dumps({
                "status": "queued", "request_id": "req-1",
                "status_url": "https://api.test/requests/req-1/status",
            }).encode()
        if url.endswith("/requests/req-1/status"):
            if self.polls_left:
                self.polls_left -= 1
                return 200, b'{"status": "in_progress"}'
            payload = {"status": self.final_status, "request_id": "req-1"}
            if self.final_status == "completed":
                payload["images"] = [{"url": "https://cdn.test/out.png"}]
            return 200, json.dumps(payload).encode()
        if url == "https://cdn.test/out.png":
            buffer = BytesIO()
            Image.new("RGB", (1536, 2048), (200, 200, 200)).save(buffer, format="PNG")  # 3:4
            return 200, buffer.getvalue()
        return 404, b"{}"


def _higgsfield(transport, **kwargs):
    return HiggsfieldProvider("kid", "ksecret", base_url="https://api.test", transport=transport,
                              sleep=lambda _s: None, **kwargs)


def _request(tmp_path, aspect="4:5", tier="quality"):
    product = tmp_path / "product.png"
    product.write_bytes(_product_png().getvalue())
    return ShotRequest(prompt="a can", aspect=aspect, tier=tier,
                       product_path=product, output_path=tmp_path / "out" / "01-portrait-4x5.png")


class TestHiggsfieldAdapter:
    def test_full_flow_and_request_shape(self, tmp_path):
        fake = FakeHiggsfield()
        result = _higgsfield(fake).render(_request(tmp_path))
        assert "error" not in result, result
        with Image.open(result["path"]) as image:
            assert abs(image.width / image.height - 4 / 5) < 0.01  # 3:4 render cropped to 4:5

        upload_ticket, put, submit = fake.log[0], fake.log[1], fake.log[2]
        assert upload_ticket["headers"]["Authorization"] == "Key kid:ksecret"
        assert upload_ticket["body"] == {"content_type": "image/png"}
        assert put["method"] == "PUT" and "Authorization" not in put["headers"]
        assert put["headers"]["x-amz-tagging"] == "retention=temporary"
        assert submit["headers"]["Idempotency-Key"]
        assert submit["body"] == {
            "prompt": "a can", "image_urls": ["https://cdn.test/in.png"],
            "aspect_ratio": "3:4", "resolution": "2k", "quality": "high",
            "enhance_prompt": False,
        }
        download = fake.log[-1]
        assert download["url"] == "https://cdn.test/out.png"
        assert "Authorization" not in download["headers"]

    def test_product_uploaded_once_per_pack(self, tmp_path):
        fake = FakeHiggsfield(polls_before_done=0)
        provider = _higgsfield(fake)
        provider.render(_request(tmp_path))
        provider.render(_request(tmp_path, aspect="1:1"))
        assert sum(1 for entry in fake.log if entry["url"].endswith("generate-upload-url")) == 1

    @pytest.mark.parametrize("status,code", [("nsfw", "moderated"), ("failed", "provider_failed")])
    def test_terminal_failures(self, tmp_path, status, code):
        result = _higgsfield(FakeHiggsfield(final_status=status)).render(_request(tmp_path))
        assert result["error_code"] == code

    def test_server_error_retried_with_same_idempotency_key(self, tmp_path):
        fake = FakeHiggsfield(submit_errors=[500])
        assert "error" not in _higgsfield(fake).render(_request(tmp_path))
        submits = [e for e in fake.log if e["url"].endswith("/marketing-studio/image")]
        assert len(submits) == 2
        assert submits[0]["headers"]["Idempotency-Key"] == submits[1]["headers"]["Idempotency-Key"]

    @pytest.mark.parametrize("http,code", [(401, "auth_failed"), (403, "provider_out_of_credits"),
                                           (422, "rejected")])
    def test_client_errors_not_retried(self, tmp_path, http, code):
        fake = FakeHiggsfield(submit_errors=[http, http])
        assert _higgsfield(fake).render(_request(tmp_path))["error_code"] == code
        assert sum(1 for e in fake.log if e["url"].endswith("/marketing-studio/image")) == 1

    def test_times_out(self, tmp_path):
        ticks = iter(range(0, 10_000, 100))
        provider = _higgsfield(FakeHiggsfield(polls_before_done=10_000),
                               clock=lambda: next(ticks), timeout_seconds=300)
        assert provider.render(_request(tmp_path))["error_code"] == "timeout"

    def test_secret_never_in_result(self, tmp_path):
        result = _higgsfield(FakeHiggsfield(final_status="failed")).render(_request(tmp_path))
        assert "ksecret" not in json.dumps(result)


class TestProviderSelection:
    def test_auto_prefers_higgsfield_when_configured(self, monkeypatch):
        monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "kid")
        monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "ksecret")
        monkeypatch.delenv("PHOTOGEN_IMAGE_PROVIDER", raising=False)
        assert select_provider(lambda *a, **k: []).name == "higgsfield"

    def test_auto_falls_back_to_gemini(self, monkeypatch):
        monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
        monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
        monkeypatch.delenv("PHOTOGEN_IMAGE_PROVIDER", raising=False)
        assert select_provider(lambda *a, **k: []).name == "gemini"

    def test_forced_higgsfield_without_keys_raises(self, monkeypatch):
        monkeypatch.setenv("PHOTOGEN_IMAGE_PROVIDER", "higgsfield")
        monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
        with pytest.raises(ValueError):
            select_provider(lambda *a, **k: [])

    def test_forced_gemini(self, monkeypatch):
        monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "kid")
        monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "ksecret")
        monkeypatch.setenv("PHOTOGEN_IMAGE_PROVIDER", "gemini")
        assert select_provider(lambda *a, **k: []).name == "gemini"


def test_prompts_never_ask_for_user_writing():
    vibe = photoshoot.VIBES["sunlit"]
    for shot in photoshoot.SHOTS:
        scene = photoshoot.build_prompt(shot, vibe, "", product_in_scene=True)
        assert "reference" in scene and "No added text" in scene
        empty_set = photoshoot.build_prompt(shot, vibe, "citrus props", product_in_scene=False)
        assert "citrus props" in empty_set
