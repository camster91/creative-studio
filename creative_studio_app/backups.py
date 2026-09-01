"""Consistent, manifest-backed snapshots for Photogen persistent data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_SQLITE_SIDECARS = ("-journal", "-shm", "-wal")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_manifest_path(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"unsafe manifest path: {value!r}")
    return Path(*pure.parts)


def _regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"source directory does not exist: {root}")
    files: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in directory_names:
            path = current / name
            if path.is_symlink():
                raise ValueError(f"snapshot source contains a symlink: {path}")
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"snapshot source contains a non-regular file: {path}")
            files.append(path)
    return sorted(files)


def _sqlite_integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    finally:
        connection.close()
    result = "\n".join(str(row[0]) for row in rows)
    if result != "ok":
        raise ValueError(f"SQLite integrity check failed for {path}: {result}")
    return result


def _copy_sqlite(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        # Online backups inherit WAL mode from a live source. Normalize the
        # isolated copy so read-only integrity checks do not create untracked
        # -wal/-shm files beside the manifest-backed database.
        destination_connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        destination_connection.close()
        source_connection.close()
    _sqlite_integrity(destination)


def create_snapshot(data_dir: Path, outputs_dir: Path, destination: Path) -> dict[str, Any]:
    """Create a new atomic snapshot without copying SQLite WAL sidecars."""

    data_dir = data_dir.resolve()
    outputs_dir = outputs_dir.resolve()
    destination = destination.resolve()
    for source in (data_dir, outputs_dir):
        if destination == source or source in destination.parents:
            raise ValueError("snapshot destination must be outside source directories")
    if destination.exists():
        raise ValueError(f"snapshot destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    entries: list[dict[str, Any]] = []
    sqlite_paths: list[str] = []
    try:
        for source_root, label in ((data_dir, "data"), (outputs_dir, "outputs")):
            (temporary / label).mkdir()
            for source in _regular_files(source_root):
                relative = source.relative_to(source_root)
                relative_text = relative.as_posix()
                if relative_text.endswith(_SQLITE_SIDECARS):
                    continue
                target = temporary / label / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                snapshot_path = f"{label}/{relative_text}"
                if source.suffix.lower() in _SQLITE_SUFFIXES:
                    _copy_sqlite(source, target)
                    sqlite_paths.append(snapshot_path)
                else:
                    shutil.copy2(source, target)
                entries.append(
                    {
                        "path": snapshot_path,
                        "size": target.stat().st_size,
                        "sha256": _sha256(target),
                    }
                )

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": sorted(entries, key=lambda entry: entry["path"]),
            "sqlite_integrity": {path: "ok" for path in sorted(sqlite_paths)},
        }
        (temporary / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.rename(destination)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Verify path safety, completeness, hashes, sizes, and SQLite integrity."""

    snapshot_dir = snapshot_dir.resolve()
    manifest_path = snapshot_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported backup manifest schema")

    expected: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("entries", []):
        relative = _safe_manifest_path(str(entry.get("path", "")))
        normalized = relative.as_posix()
        if normalized in expected:
            raise ValueError(f"duplicate manifest path: {normalized}")
        expected[normalized] = entry

    actual = {
        path.relative_to(snapshot_dir).as_posix(): path
        for path in _regular_files(snapshot_dir)
        if path.name != MANIFEST_NAME
    }
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(f"snapshot file set mismatch; missing={missing}, extra={extra}")

    for relative, entry in expected.items():
        path = actual[relative]
        if path.stat().st_size != entry.get("size"):
            raise ValueError(f"snapshot size mismatch: {relative}")
        if _sha256(path) != entry.get("sha256"):
            raise ValueError(f"snapshot hash mismatch: {relative}")

    sqlite_results: dict[str, str] = {}
    for value in manifest.get("sqlite_integrity", {}):
        relative = _safe_manifest_path(str(value)).as_posix()
        if relative not in actual:
            raise ValueError(f"SQLite manifest entry is missing: {relative}")
        sqlite_results[relative] = _sqlite_integrity(actual[relative])

    return {
        "schema_version": SCHEMA_VERSION,
        "files_verified": len(actual),
        "bytes_verified": sum(path.stat().st_size for path in actual.values()),
        "sqlite_verified": len(sqlite_results),
        "created_at": manifest.get("created_at"),
    }
