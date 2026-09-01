"""SQLite-backed users, one-time magic links, and credit-safe sessions."""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def init_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with connect(path) as database:
        database.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL,
            credits_remaining INTEGER NOT NULL DEFAULT 0,
            credits_used_today INTEGER NOT NULL DEFAULT 0,
            last_trial_date TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            user_agent TEXT,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_magic_email ON magic_link_tokens(email);
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT 'Untitled project',
            hero_url TEXT,
            generations_json TEXT NOT NULL DEFAULT '[]',
            source_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
        CREATE TABLE IF NOT EXISTS brand_passports (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            voice TEXT NOT NULL DEFAULT '',
            visual_rules_json TEXT NOT NULL DEFAULT '[]',
            forbidden_content_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_brand_passports_user
            ON brand_passports(user_id);
        CREATE TABLE IF NOT EXISTS product_truth (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            brand_id TEXT NOT NULL REFERENCES brand_passports(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sku TEXT NOT NULL,
            facts_json TEXT NOT NULL DEFAULT '[]',
            approved_claims_json TEXT NOT NULL DEFAULT '[]',
            required_disclosures_json TEXT NOT NULL DEFAULT '[]',
            pack_asset_name TEXT,
            pack_asset_sha256 TEXT,
            pack_asset_waived INTEGER NOT NULL DEFAULT 0,
            pack_asset_waiver_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_product_truth_user
            ON product_truth(user_id);
        CREATE TABLE IF NOT EXISTS campaign_work_orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            brand_id TEXT NOT NULL REFERENCES brand_passports(id) ON DELETE CASCADE,
            product_id TEXT NOT NULL REFERENCES product_truth(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            objective TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT '',
            offer TEXT NOT NULL DEFAULT '',
            channels_json TEXT NOT NULL DEFAULT '[]',
            creative_direction TEXT NOT NULL DEFAULT '',
            aspect_ratio TEXT NOT NULL DEFAULT '1:1',
            tier TEXT NOT NULL DEFAULT 'balanced',
            variations INTEGER NOT NULL DEFAULT 4,
            status TEXT NOT NULL DEFAULT 'draft',
            last_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_work_orders_user
            ON campaign_work_orders(user_id);
        CREATE TABLE IF NOT EXISTS campaign_bundles (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            campaign_id TEXT NOT NULL REFERENCES campaign_work_orders(id) ON DELETE CASCADE,
            zip_relpath TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            source_session_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id,campaign_id,id)
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_bundles_owner
            ON campaign_bundles(user_id,campaign_id,created_at);
        """)
        existing = {row[1] for row in database.execute("PRAGMA table_info(users)")}
        for column, declaration in [
            ("stripe_customer_id", "TEXT"),
            ("stripe_subscription_id", "TEXT"),
            ("subscription_tier", "TEXT"),
            ("subscription_status", "TEXT"),
            ("subscription_renews_at", "TEXT"),
            ("credits_remaining", "INTEGER NOT NULL DEFAULT 0"),
            ("credits_used_today", "INTEGER NOT NULL DEFAULT 0"),
            ("last_trial_date", "TEXT"),
        ]:
            if column not in existing:
                database.execute(f"ALTER TABLE users ADD COLUMN {column} {declaration}")
        product_columns = {row[1] for row in database.execute("PRAGMA table_info(product_truth)")}
        for column, declaration in [
            ("pack_asset_name", "TEXT"),
            ("pack_asset_sha256", "TEXT"),
            ("pack_asset_waived", "INTEGER NOT NULL DEFAULT 0"),
            ("pack_asset_waiver_reason", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if column not in product_columns:
                database.execute(f"ALTER TABLE product_truth ADD COLUMN {column} {declaration}")
        database.commit()


def create_magic_link(path: Path, email: str, expiry_minutes: int) -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = now_iso()
    expires = (datetime.now() + timedelta(minutes=expiry_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as database:
        database.execute(
            "INSERT INTO magic_link_tokens (token, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, email.lower().strip(), now, expires),
        )
        database.commit()
    return raw


def consume_magic_link(
    path: Path,
    token: str,
    *,
    session_days: int,
    free_trial_credits: int,
) -> dict | None:
    """Atomically consume a one-time token and create its user session."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = now_iso()
    with connect(path) as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT * FROM magic_link_tokens WHERE token = ? AND expires_at > ? AND used = 0",
            (token_hash, now),
        ).fetchone()
        if not row:
            database.rollback()
            return None
        consumed = database.execute(
            "UPDATE magic_link_tokens SET used = 1 WHERE token = ? AND used = 0",
            (token_hash,),
        )
        if consumed.rowcount != 1:
            database.rollback()
            return None
        email = row["email"]
        user = database.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            user_id = secrets.token_hex(16)
            database.execute(
                "INSERT INTO users (id, email, created_at, credits_remaining) VALUES (?, ?, ?, ?)",
                (user_id, email, now, free_trial_credits),
            )
            user = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        session_id = secrets.token_hex(32)
        expires = (datetime.now() + timedelta(days=session_days)).strftime("%Y-%m-%d %H:%M:%S")
        database.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, user["id"], now, expires, now),
        )
        database.commit()
        return {
            "id": session_id,
            "user_id": user["id"],
            "email": user["email"],
            "credits_remaining": user["credits_remaining"],
            "expires_at": expires,
        }


def session_from_token(path: Path, token: str) -> dict | None:
    now = now_iso()
    with connect(path) as database:
        row = database.execute(
            "SELECT * FROM sessions WHERE id = ? AND expires_at > ?", (token, now)
        ).fetchone()
        if not row:
            return None
        database.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, token))
        user = database.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        database.commit()
        if not user:
            return None
        return {
            "id": row["id"],
            "user_id": user["id"],
            "email": user["email"],
            "credits_remaining": user["credits_remaining"],
            "expires_at": row["expires_at"],
        }


def use_trial_credit(path: Path, user_id: str) -> tuple[bool, int]:
    """Atomically decrement a positive balance, preventing concurrent overspend."""
    with connect(path) as database:
        database.execute("BEGIN IMMEDIATE")
        updated = database.execute(
            """UPDATE users
               SET credits_remaining = credits_remaining - 1,
                   credits_used_today = credits_used_today + 1
               WHERE id = ? AND credits_remaining > 0""",
            (user_id,),
        )
        if updated.rowcount != 1:
            database.rollback()
            return False, 0
        remaining = database.execute(
            "SELECT credits_remaining FROM users WHERE id = ?", (user_id,)
        ).fetchone()["credits_remaining"]
        database.commit()
        return True, int(remaining)
