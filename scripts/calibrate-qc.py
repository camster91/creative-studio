#!/usr/bin/env python3
"""Aggregate a consent-safe QC calibration JSON file."""

import argparse
import json
from pathlib import Path

from creative_studio_app.qc_calibration import calibrate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="JSON array of fixture reviews and provider assessments")
    parser.add_argument("--output", type=Path, help="Optional aggregate report path")
    args = parser.parse_args()
    records = json.loads(args.input.read_text(encoding="utf-8"))
    report = calibrate(records)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
