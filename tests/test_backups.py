from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from creative_studio_app.backups import create_snapshot, verify_snapshot


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO records (value) VALUES ('approved claim')")
    connection.commit()
    connection.close()


def test_snapshot_copies_files_and_consistent_sqlite_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    _database(data / "campaigns.db")
    (data / "session.json").write_text('{"owner":"user:1"}', encoding="utf-8")
    (outputs / "asset.png").write_bytes(b"approved-output")

    snapshot = tmp_path / "snapshot"
    manifest = create_snapshot(data, outputs, snapshot)
    report = verify_snapshot(snapshot)

    assert len(manifest["entries"]) == 3
    assert report == {
        "schema_version": 1,
        "files_verified": 3,
        "bytes_verified": sum(entry["size"] for entry in manifest["entries"]),
        "sqlite_verified": 1,
        "created_at": manifest["created_at"],
    }
    connection = sqlite3.connect(snapshot / "data" / "campaigns.db")
    assert connection.execute("SELECT value FROM records").fetchone() == ("approved claim",)
    connection.close()


def test_snapshot_ignores_sqlite_sidecars(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    _database(data / "jobs.db")
    (data / "jobs.db-wal").write_bytes(b"not-a-real-wal")
    (data / "jobs.db-shm").write_bytes(b"not-a-real-shm")

    snapshot = tmp_path / "snapshot"
    create_snapshot(data, outputs, snapshot)

    assert not (snapshot / "data" / "jobs.db-wal").exists()
    assert not (snapshot / "data" / "jobs.db-shm").exists()
    assert verify_snapshot(snapshot)["sqlite_verified"] == 1


def test_snapshot_normalizes_a_live_wal_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    source = data / "provider-ledger.db"
    connection = sqlite3.connect(source)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, outcome TEXT NOT NULL)")
    connection.execute("INSERT INTO calls (outcome) VALUES ('success')")
    connection.commit()

    snapshot = tmp_path / "snapshot"
    create_snapshot(data, outputs, snapshot)
    connection.close()

    copied = snapshot / "data" / "provider-ledger.db"
    copied_connection = sqlite3.connect(copied)
    assert copied_connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert copied_connection.execute("SELECT outcome FROM calls").fetchone() == ("success",)
    copied_connection.close()
    assert not (snapshot / "data" / "provider-ledger.db-wal").exists()
    assert not (snapshot / "data" / "provider-ledger.db-shm").exists()
    assert verify_snapshot(snapshot)["sqlite_verified"] == 1


def test_verify_rejects_modified_or_extra_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    (data / "record.json").write_text("original", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    create_snapshot(data, outputs, snapshot)

    (snapshot / "data" / "record.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="size mismatch|hash mismatch"):
        verify_snapshot(snapshot)

    (snapshot / "data" / "record.json").write_text("original", encoding="utf-8")
    (snapshot / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="file set mismatch"):
        verify_snapshot(snapshot)


def test_verify_rejects_unsafe_manifest_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    (snapshot / "data").mkdir(parents=True)
    (snapshot / "outputs").mkdir()
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [{"path": "../secret", "size": 0, "sha256": "x"}],
                "sqlite_integrity": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsafe manifest path"):
        verify_snapshot(snapshot)


def test_snapshot_rejects_symlinks(tmp_path: Path) -> None:
    data = tmp_path / "data"
    outputs = tmp_path / "outputs"
    data.mkdir()
    outputs.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (data / "leak").symlink_to(target)

    with pytest.raises(ValueError, match="non-regular file"):
        create_snapshot(data, outputs, tmp_path / "snapshot")
