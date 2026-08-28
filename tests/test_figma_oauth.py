import sqlite3
import time
import urllib.error

from cryptography.fernet import Fernet
from flask import Flask, request

from creative_studio_app.figma_oauth import FigmaOAuthStore, FIGMA_SCOPE
from creative_studio_app.figma_oauth_routes import create_blueprint


def make_store(tmp_path):
    return FigmaOAuthStore(tmp_path / "figma.db", Fernet.generate_key().decode("ascii"))


def test_pkce_state_is_single_use_and_secrets_are_encrypted(tmp_path):
    store = make_store(tmp_path)
    flow = store.begin("user:a")
    assert flow["code_challenge"] != flow["code_verifier"]
    consumed = store.consume_state(flow["state"])
    assert consumed == {"owner_id": "user:a", "code_verifier": flow["code_verifier"]}
    assert store.consume_state(flow["state"]) is None
    store.save_connection("user:a", {
        "access_token": "access-secret", "refresh_token": "refresh-secret",
        "user_id_string": "figma-user-a", "expires_in": 3600, "scope": FIGMA_SCOPE,
    })
    raw = (tmp_path / "figma.db").read_bytes()
    assert b"access-secret" not in raw and b"refresh-secret" not in raw
    assert store.connection("user:a")["access_token"] == "access-secret"
    assert store.connection("user:b") is None


class FakeClient:
    def __init__(self):
        self.exchanges = 0
        self.refreshes = 0
        self.files = []
        self.revoked = False

    def authorization_url(self, *, state, code_challenge):
        return f"https://www.figma.com/oauth?state={state}&code_challenge={code_challenge}"

    def exchange(self, code, verifier):
        self.exchanges += 1
        assert code == "auth-code" and verifier
        return {
            "access_token": "access-a", "refresh_token": "refresh-a",
            "user_id_string": "figma-a", "expires_in": 3600, "scope": FIGMA_SCOPE,
        }

    def refresh(self, refresh_token):
        self.refreshes += 1
        assert refresh_token == "refresh-a"
        return {"access_token": "access-refreshed", "expires_in": 3600}

    def fetch_file(self, access_token, file_key, node_id):
        if self.revoked:
            raise urllib.error.HTTPError("url", 401, "revoked", {}, None)
        self.files.append((access_token, file_key, node_id))
        return {"name": "private design", "document": {"id": node_id or "0:0"}}


def build_app(store, client, configured=True):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(
        current_session=lambda: (
            {"user_id": request.headers["X-User"]}
            if request.headers.get("X-User") else None
        ),
        store=store,
        client=client,
        configured=lambda: configured,
        parse_figma_url=lambda url: (
            ("file-key", "1:2") if url == "https://figma.test/file" else (None, None)
        ),
        rate_limited=lambda function: function,
    ))
    return app


def connect(client, store, user="a"):
    response = client.post("/api/figma/oauth/connect", headers={"X-User": user})
    state = response.get_json()["authorization_url"].split("state=")[1].split("&")[0]
    return client.get(
        f"/api/figma/oauth/callback?state={state}&code=auth-code"
    )


def test_owner_bound_connect_context_refresh_revoke_and_disconnect(tmp_path):
    store = make_store(tmp_path)
    provider = FakeClient()
    http = build_app(store, provider).test_client()
    assert connect(http, store).status_code == 303
    assert http.get("/api/figma/oauth/status", headers={"X-User": "a"}).get_json()["connected"]
    assert not http.get("/api/figma/oauth/status", headers={"X-User": "b"}).get_json()["connected"]
    denied = http.post(
        "/api/figma/context", json={"url": "https://figma.test/file"},
        headers={"X-User": "b"},
    )
    assert denied.status_code == 401

    with store._connect() as database:
        database.execute(
            "UPDATE figma_connections SET expires_at=? WHERE owner_id='user:a'",
            (time.time() - 1,),
        )
        database.commit()
    context = http.post(
        "/api/figma/context", json={"url": "https://figma.test/file"},
        headers={"X-User": "a"},
    )
    assert context.status_code == 200
    assert provider.refreshes == 1
    assert provider.files == [("access-refreshed", "file-key", "1:2")]
    assert b"file-key" not in (tmp_path / "figma.db").read_bytes()

    provider.revoked = True
    revoked = http.post(
        "/api/figma/context", json={"url": "https://figma.test/file"},
        headers={"X-User": "a"},
    )
    assert revoked.status_code == 401
    assert store.connection("user:a") is None
    assert http.post(
        "/api/figma/oauth/disconnect", headers={"X-User": "a"}
    ).get_json()["disconnected"] is False


def test_invalid_state_and_cross_user_tokens_fail_closed(tmp_path):
    store = make_store(tmp_path)
    provider = FakeClient()
    http = build_app(store, provider).test_client()
    assert http.get(
        "/api/figma/oauth/callback?state=wrong&code=auth-code"
    ).status_code == 400
    assert http.post("/api/figma/oauth/connect").status_code == 401
    assert http.post(
        "/api/figma/context", json={"url": "bad"}, headers={"X-User": "a"}
    ).status_code == 400


def test_unconfigured_endpoints_never_touch_missing_store():
    app = build_app(None, None, configured=False)
    http = app.test_client()
    headers = {"X-User": "a"}
    assert http.get("/api/figma/oauth/status", headers=headers).get_json() == {
        "connected": False, "configured": False,
    }
    assert http.post("/api/figma/oauth/connect", headers=headers).status_code == 503
    assert http.get("/api/figma/oauth/callback?state=x&code=y").status_code == 503
    assert http.post("/api/figma/oauth/disconnect", headers=headers).status_code == 503
    assert http.post("/api/figma/context", json={"url": "x"}, headers=headers).status_code == 503
