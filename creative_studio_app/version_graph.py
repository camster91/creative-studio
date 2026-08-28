"""Durable owner-scoped creative version graphs."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path


def _node_id() -> str:
    return "ver_" + uuid.uuid4().hex[:16]


class VersionGraphStore:
    """SQLite graph store with transactional ownership and lineage checks."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS version_graphs (
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    current_node_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (session_id, owner_id)
                );
                CREATE TABLE IF NOT EXISTS version_nodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    parent_id TEXT,
                    operation TEXT NOT NULL,
                    asset_url TEXT,
                    prompt TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    cost REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'completed',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    deleted_at REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(parent_id) REFERENCES version_nodes(id)
                );
                CREATE INDEX IF NOT EXISTS version_nodes_graph
                    ON version_nodes(session_id, owner_id, created_at);
                CREATE INDEX IF NOT EXISTS version_nodes_parent
                    ON version_nodes(parent_id);
                """
            )

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA journal_mode=WAL")
        return database

    @staticmethod
    def _serialize(row) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["favorite"] = bool(item["favorite"])
        item["deleted"] = item.pop("deleted_at") is not None
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def add_node(
        self,
        session_id: str,
        owner_id: str,
        *,
        operation: str,
        asset_url: str | None,
        parent_id: str | None = None,
        prompt: str = "",
        model: str = "",
        cost: float = 0,
        status: str = "completed",
        metadata: dict | None = None,
        identifier: str | None = None,
    ) -> dict:
        if not session_id or not owner_id:
            raise ValueError("Session and owner are required")
        if operation not in {"generate", "variation", "refine", "composite", "chat", "legacy"}:
            raise ValueError("Unsupported version operation")
        if status not in {"partial", "completed", "failed"}:
            raise ValueError("Unsupported version status")
        identifier = identifier or _node_id()
        created_at = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if parent_id:
                parent = database.execute(
                    "SELECT id FROM version_nodes WHERE id=? AND session_id=? AND owner_id=? AND deleted_at IS NULL",
                    (parent_id, session_id, owner_id),
                ).fetchone()
                if parent is None:
                    database.rollback()
                    raise ValueError("Parent version not found")
            database.execute(
                "INSERT OR IGNORE INTO version_graphs(session_id,owner_id,created_at,updated_at) VALUES(?,?,?,?)",
                (session_id, owner_id, created_at, created_at),
            )
            database.execute(
                """INSERT INTO version_nodes
                   (id,session_id,owner_id,parent_id,operation,asset_url,prompt,model,cost,status,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier, session_id, owner_id, parent_id, operation,
                    asset_url, str(prompt)[:4000], str(model)[:200], max(0, float(cost)),
                    status, json.dumps(metadata or {}, separators=(",", ":")), created_at,
                ),
            )
            database.execute(
                "UPDATE version_graphs SET current_node_id=?,updated_at=? WHERE session_id=? AND owner_id=?",
                (identifier, created_at, session_id, owner_id),
            )
            database.commit()
            row = database.execute("SELECT * FROM version_nodes WHERE id=?", (identifier,)).fetchone()
        return self._serialize(row)

    def graph(self, session_id: str, owner_id: str, *, include_deleted: bool = False) -> dict | None:
        with self._connect() as database:
            graph = database.execute(
                "SELECT * FROM version_graphs WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
            if graph is None:
                return None
            clause = "" if include_deleted else " AND deleted_at IS NULL"
            rows = database.execute(
                "SELECT * FROM version_nodes WHERE session_id=? AND owner_id=?" + clause + " ORDER BY created_at,id",
                (session_id, owner_id),
            ).fetchall()
        nodes = [self._serialize(row) for row in rows]
        by_id = {node["id"]: node for node in nodes}
        current_id = graph["current_node_id"] if graph["current_node_id"] in by_id else None
        branch_cost = 0.0
        cursor, seen = current_id, set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            node = by_id.get(cursor)
            if not node:
                break
            branch_cost += node["cost"]
            cursor = node["parent_id"]
        return {
            "session_id": session_id,
            "current_node_id": current_id,
            "nodes": nodes,
            "cumulative_cost": round(sum(node["cost"] for node in nodes), 6),
            "branch_cost": round(branch_cost, 6),
        }

    def owns_node(self, session_id: str, owner_id: str, node_id: str | None) -> bool:
        if not node_id:
            return True
        with self._connect() as database:
            row = database.execute(
                "SELECT 1 FROM version_nodes WHERE id=? AND session_id=? AND owner_id=? AND deleted_at IS NULL",
                (node_id, session_id, owner_id),
            ).fetchone()
        return row is not None

    def select(self, session_id: str, owner_id: str, node_id: str, *, favorite: bool = False) -> dict:
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            node = database.execute(
                "SELECT * FROM version_nodes WHERE id=? AND session_id=? AND owner_id=? AND deleted_at IS NULL",
                (node_id, session_id, owner_id),
            ).fetchone()
            if node is None:
                database.rollback()
                raise ValueError("Version not found")
            if favorite:
                database.execute(
                    "UPDATE version_nodes SET favorite=0 WHERE session_id=? AND owner_id=?",
                    (session_id, owner_id),
                )
                database.execute("UPDATE version_nodes SET favorite=1 WHERE id=?", (node_id,))
            database.execute(
                "UPDATE version_graphs SET current_node_id=?,updated_at=? WHERE session_id=? AND owner_id=?",
                (node_id, now, session_id, owner_id),
            )
            database.commit()
        return self.graph(session_id, owner_id)

    def undo(self, session_id: str, owner_id: str) -> dict:
        graph = self.graph(session_id, owner_id)
        if graph is None or graph["current_node_id"] is None:
            raise ValueError("Version graph not found")
        current = next(node for node in graph["nodes"] if node["id"] == graph["current_node_id"])
        if current["parent_id"] is None:
            return graph
        return self.select(session_id, owner_id, current["parent_id"])

    def delete_branch(self, session_id: str, owner_id: str, node_id: str) -> dict:
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            node = database.execute(
                "SELECT parent_id FROM version_nodes WHERE id=? AND session_id=? AND owner_id=? AND deleted_at IS NULL",
                (node_id, session_id, owner_id),
            ).fetchone()
            if node is None:
                database.rollback()
                raise ValueError("Version not found")
            descendants = database.execute(
                """WITH RECURSIVE branch(id) AS (
                       SELECT id FROM version_nodes WHERE id=? AND session_id=? AND owner_id=?
                       UNION ALL
                       SELECT child.id FROM version_nodes child JOIN branch ON child.parent_id=branch.id
                       WHERE child.session_id=? AND child.owner_id=?
                   ) SELECT id FROM branch""",
                (node_id, session_id, owner_id, session_id, owner_id),
            ).fetchall()
            ids = [row["id"] for row in descendants]
            database.executemany(
                "UPDATE version_nodes SET deleted_at=?,favorite=0 WHERE id=?",
                [(now, identifier) for identifier in ids],
            )
            graph = database.execute(
                "SELECT current_node_id FROM version_graphs WHERE session_id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
            if graph and graph["current_node_id"] in ids:
                database.execute(
                    "UPDATE version_graphs SET current_node_id=?,updated_at=? WHERE session_id=? AND owner_id=?",
                    (node["parent_id"], now, session_id, owner_id),
                )
            database.commit()
        return self.graph(session_id, owner_id)

    def migrate_legacy(self, session: dict, owner_id: str) -> int:
        """Import old flat entries once, retaining source data unchanged."""
        session_id = session.get("id")
        if not session_id or session.get("owner_id") != owner_id:
            return 0
        if self.graph(session_id, owner_id) is not None:
            return 0
        parent_id = None
        imported = 0
        for index, entry in enumerate(session.get("entries", [])):
            if not isinstance(entry, dict):
                continue
            identifier = "legacy_" + uuid.uuid5(
                uuid.NAMESPACE_URL, f"{owner_id}:{session_id}:{index}"
            ).hex[:16]
            node = self.add_node(
                session_id,
                owner_id,
                operation="legacy",
                asset_url=entry.get("image_url") or None,
                parent_id=parent_id,
                prompt=entry.get("prompt") or entry.get("note") or "",
                model=entry.get("model") or "",
                cost=entry.get("cost") or 0,
                status="completed" if entry.get("image_url") else "partial",
                metadata={"legacy_type": entry.get("type", "unknown")},
                identifier=identifier,
            )
            parent_id = node["id"]
            imported += 1
        return imported
