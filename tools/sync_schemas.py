#!/usr/bin/env python3
"""Sync the canonical ``schemas/*.json`` into the packaged mirror.

The repo root ``schemas/`` directory is CANONICAL; ``src/akcli/schemas/``
ships in wheels and must stay byte-identical (test_schema_exports enforces
it). Editing the canonical file and forgetting the copy leaves the CLI
serving a stale schema in a ``PYTHONPATH=src`` checkout — run this instead
of copying by hand.

Usage:
    python3 tools/sync_schemas.py          # copy canonical -> mirror
    python3 tools/sync_schemas.py --check  # exit 1 if any mirror drifted
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "schemas"
MIRROR = ROOT / "src" / "akcli" / "schemas"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    drift: list[str] = []
    for src in sorted(CANONICAL.glob("*.json")):
        dst = MIRROR / src.name
        want = src.read_bytes()
        have = dst.read_bytes() if dst.exists() else None
        if have == want:
            continue
        drift.append(src.name)
        if not check:
            dst.write_bytes(want)
            print(f"synced {src.name}")
    if check and drift:
        print("mirror drift: " + ", ".join(drift))
        return 1
    if not drift:
        print(f"mirrors up to date ({len(list(CANONICAL.glob('*.json')))} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
