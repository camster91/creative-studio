#!/usr/bin/env python3
"""Create or verify a Photogen production snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from creative_studio_app.backups import create_snapshot, verify_snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--outputs-dir", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--snapshot-dir", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "create":
        result = create_snapshot(arguments.data_dir, arguments.outputs_dir, arguments.destination)
        summary = {
            "created_at": result["created_at"],
            "files": len(result["entries"]),
            "sqlite_files": len(result["sqlite_integrity"]),
        }
    else:
        summary = verify_snapshot(arguments.snapshot_dir)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
