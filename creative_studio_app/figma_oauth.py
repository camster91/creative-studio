"""Owner-bound Figma OAuth with PKCE and encrypted token persistence."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


FIGMA_SCOPE = "file_content:read"


class FigmaOAuthStore:
    def __init__(self, path: Path, encryption_key: str):
        if not encryption_key:
            raise ValueError("FIGMA_TOKEN_ENCRYPTION_KEY is required")
        try:
            self.cipher = Fernet(encryption_key.encode("ascii"))
        except (ValueError, TypeError) as error:
            raise ValueError("FIGMA_TOKEN_ENCRYPTION_KEY must be a Fernet key") from error
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS figma_oauth_states (
                    state_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    verifier_encrypted BLOB NOT NULL, expires_at REAL NOT NULL,
                    used_at REAL
                );
                CREATE TABLE IF NOT EXISTS figma_connections (
                    owner_id TEXT PRIMARY KEY, figma_user_id TEXT NOT NULL,
                    access_encrypted BLOB NOT NULL, refresh_encrypted BLOB NOT NULL,
                    expires_at REAL NOT NULL, scope TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS figma_file_access (
                    owner_id TEXT NOT NULL, file_key_hash TEXT NOT NULL,
                    last_used_at REAL NOT NULL,
                    PRIMARY KEY(owner_id,file_key_hash)
                );
                CREATE TABLE IF NOT EXISTS figma_oauth_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
                    event TEXT NOT NULL, outcome TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA secure_delete=ON")
        return database

    def _encrypt(self, value: str) -> bytes:
        return self.cipher.encrypt(value.encode("utf-8"))

    def _decrypt(self, value: bytes) -> str:
        try:
            return self.cipher.decrypt(value).decode("utf-8")
        except InvalidToken as error:
            raise ValueError("Stored Figma token cannot be decrypted") from error

    def audit(self, owner_id: str, event: str, outcome: str) -> None:
        with self._connect() as database:
            database.execute(
                "INSERT INTO figma_oauth_audit(owner_id,event,outcome,created_at) VALUES(?,?,?,?)",
                (owner_id, event[:40], outcome[:40], time.time()),
            )
            database.commit()

    def begin(self, owner_id: str, *, ttl_seconds: int = 600) -> dict:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
        with self._connect() as database:
            database.execute(
                "INSERT INTO figma_oauth_states(state_hash,owner_id,verifier_encrypted,expires_at) VALUES(?,?,?,?)",
                (state_hash, owner_id, self._encrypt(verifier), time.time() + ttl_seconds),
            )
            database.commit()
        self.audit(owner_id, "connect_started", "ok")
        return {"state": state, "code_verifier": verifier, "code_challenge": challenge}

    def consume_state(self, state: str) -> dict | None:
        if not state:
            return None
        state_hash = hashlib.sha256(state.encode("ascii", "ignore")).hexdigest()
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM figma_oauth_states WHERE state_hash=? AND used_at IS NULL AND expires_at>?",
                (state_hash, now),
            ).fetchone()
            if row is None:
                database.rollback()
                return None
            database.execute(
                "UPDATE figma_oauth_states SET used_at=? WHERE state_hash=?",
                (now, state_hash),
            )
            database.commit()
        return {"owner_id": row["owner_id"], "code_verifier": self._decrypt(row["verifier_encrypted"])}

    def save_connection(self, owner_id: str, tokens: dict) -> None:
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        figma_user_id = str(tokens.get("user_id_string") or tokens.get("user_id") or "")
        if not access or not refresh or not figma_user_id:
            raise ValueError("Incomplete Figma token response")
        scope = str(tokens.get("scope") or FIGMA_SCOPE)
        if FIGMA_SCOPE not in scope.replace(",", " ").split():
            raise ValueError("Figma token is missing file_content:read")
        now = time.time()
        expires_at = now + max(1, int(tokens.get("expires_in") or 3600))
        with self._connect() as database:
            database.execute(
                """INSERT INTO figma_connections
                   (owner_id,figma_user_id,access_encrypted,refresh_encrypted,expires_at,scope,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(owner_id) DO UPDATE SET
                     figma_user_id=excluded.figma_user_id,
                     access_encrypted=excluded.access_encrypted,
                     refresh_encrypted=excluded.refresh_encrypted,
                     expires_at=excluded.expires_at,scope=excluded.scope,updated_at=excluded.updated_at""",
                (
                    owner_id, figma_user_id, self._encrypt(access), self._encrypt(refresh),
                    expires_at, scope, now, now,
                ),
            )
            database.commit()
        self.audit(owner_id, "connected", "ok")

    def connection(self, owner_id: str) -> dict | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM figma_connections WHERE owner_id=?", (owner_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_id": owner_id,
            "figma_user_id": row["figma_user_id"],
            "access_token": self._decrypt(row["access_encrypted"]),
            "refresh_token": self._decrypt(row["refresh_encrypted"]),
            "expires_at": row["expires_at"],
            "scope": row["scope"],
        }

    def disconnect(self, owner_id: str) -> bool:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            cursor = database.execute(
                "DELETE FROM figma_connections WHERE owner_id=?", (owner_id,)
            )
            database.execute("DELETE FROM figma_file_access WHERE owner_id=?", (owner_id,))
            database.commit()
        self.audit(owner_id, "disconnected", "ok" if cursor.rowcount else "not_connected")
        return bool(cursor.rowcount)

    def note_file_use(self, owner_id: str, file_key: str) -> None:
        digest = hashlib.sha256(file_key.encode("utf-8")).hexdigest()
        with self._connect() as database:
            database.execute(
                """INSERT INTO figma_file_access(owner_id,file_key_hash,last_used_at)
                   VALUES(?,?,?) ON CONFLICT(owner_id,file_key_hash)
                   DO UPDATE SET last_used_at=excluded.last_used_at""",
                (owner_id, digest, time.time()),
            )
            database.commit()


class FigmaOAuthClient:
    AUTH_URL = "https://www.figma.com/oauth"
    TOKEN_URL = "https://api.figma.com/v1/oauth/token"
    REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
    API_URL = "https://api.figma.com/v1"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return self.AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": FIGMA_SCOPE,
            "state": state,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })

    def _token_request(self, url: str, form: dict) -> dict:
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form).encode("ascii"),
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def exchange(self, code: str, code_verifier: str) -> dict:
        return self._token_request(self.TOKEN_URL, {
            "redirect_uri": self.redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        })

    def refresh(self, refresh_token: str) -> dict:
        return self._token_request(self.REFRESH_URL, {"refresh_token": refresh_token})

    def fetch_file(self, access_token: str, file_key: str, node_id: str | None) -> dict:
        query = urllib.parse.urlencode({"ids": node_id}) if node_id else ""
        url = f"{self.API_URL}/files/{urllib.parse.quote(file_key, safe='')}"
        if query:
            url += "?" + query
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
