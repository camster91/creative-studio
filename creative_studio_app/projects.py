"""User-owned project persistence over the shared SQLite database."""

import json
import secrets
from pathlib import Path

from .auth import connect, now_iso


def parse_generations(raw: str) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def serialize(row, include_generations: bool = True) -> dict:
    result = {
        "id": row["id"], "name": row["name"], "hero_url": row["hero_url"],
        "source_session_id": row["source_session_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if include_generations:
        result["generations"] = parse_generations(row["generations_json"])
    return result


def create(path: Path, user_id: str, name: str, source_session_id: str | None, *, max_projects: int, name_max: int) -> dict:
    name = (name or "Untitled project").strip()[:name_max]
    project_id, now = secrets.token_hex(16), now_iso()
    with connect(path) as database:
        count = database.execute("SELECT COUNT(*) AS n FROM projects WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if count >= max_projects:
            oldest = database.execute("SELECT id FROM projects WHERE user_id = ? ORDER BY created_at ASC LIMIT 1", (user_id,)).fetchone()
            if oldest:
                database.execute("DELETE FROM projects WHERE id = ?", (oldest["id"],))
        database.execute(
            """INSERT INTO projects (id,user_id,name,hero_url,generations_json,source_session_id,created_at,updated_at)
               VALUES (?,?,?,NULL,'[]',?,?,?)""",
            (project_id, user_id, name, source_session_id, now, now),
        )
        database.commit()
    return {"id": project_id, "name": name, "hero_url": None, "source_session_id": source_session_id, "created_at": now, "updated_at": now, "generations": []}


def get(path: Path, project_id: str, user_id: str | None = None) -> dict | None:
    query, params = "SELECT * FROM projects WHERE id = ?", (project_id,)
    if user_id is not None:
        query, params = query + " AND user_id = ?", (project_id, user_id)
    with connect(path) as database:
        row = database.execute(query, params).fetchone()
    return serialize(row) if row else None


def list_for_user(path: Path, user_id: str, include_generations: bool = False) -> list:
    with connect(path) as database:
        rows = database.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 200", (user_id,)).fetchall()
    return [serialize(row, include_generations) for row in rows]


def add_generation(path: Path, project_id: str, user_id: str, *, url: str, prompt: str, cost: float, model: str, ratio: str, url_max: int, prompt_max: int) -> dict | None:
    url, prompt = (url or "").strip()[:url_max], (prompt or "").strip()[:prompt_max]
    if not url:
        return None
    with connect(path) as database:
        row = database.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone()
        if not row:
            return None
        generations = parse_generations(row["generations_json"])
        generations.append({"url": url, "prompt": prompt, "cost": float(cost) if cost else 0, "model": model, "ratio": ratio, "added_at": now_iso()})
        database.execute(
            "UPDATE projects SET generations_json = ?, hero_url = ?, updated_at = ? WHERE id = ?",
            (json.dumps(generations), row["hero_url"] or url, now_iso(), project_id),
        )
        database.commit()
    return get(path, project_id)


def delete(path: Path, project_id: str, user_id: str) -> bool:
    with connect(path) as database:
        deleted = database.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))
        database.commit()
        return deleted.rowcount == 1
