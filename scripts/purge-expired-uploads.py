#!/usr/bin/env python3
"""Dry-run or execute canonical upload retention cleanup."""

import argparse
import os
from pathlib import Path

from creative_studio_app.uploads import purge_expired_uploads


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--upload-dir",
        type=Path,
        default=Path(os.environ.get("CREATIVE_DATA_DIR", "data")) / "uploads",
    )
    parser.add_argument("--execute", action="store_true", help="Delete instead of dry-run")
    args = parser.parse_args()
    matches = purge_expired_uploads(args.upload_dir, dry_run=not args.execute)
    action = "removed" if args.execute else "would remove"
    for path in matches:
        print(f"{action}: {path}")
    print(f"{action} {len(matches)} expired upload(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
