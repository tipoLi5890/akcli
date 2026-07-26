#!/usr/bin/env python3
"""Sync the canonical ``schemas/*.json`` into the packaged mirror.

The repo root ``schemas/`` directory is CANONICAL; ``src/akcli/schemas/``
ships in wheels and must stay byte-identical (test_schema_exports enforces
it). Editing the canonical file and forgetting the copy leaves the CLI
serving a stale schema in a ``PYTHONPATH=src`` checkout — run this instead
of copying by hand.

The sync is a two-way SET comparison, not a one-way copy: ``ops._schema_text``
loads the packaged mirror FIRST, so a mirror file whose canonical original was
deleted or renamed is not dead weight — the wheel would keep serving that
retired schema forever. Orphaned mirrors are therefore deleted (or reported
under ``--check``).

Usage:
    python3 tools/sync_schemas.py          # canonical -> mirror (+ prune)
    python3 tools/sync_schemas.py --check  # exit 1 on any drift or orphan
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "schemas"
MIRROR = ROOT / "src" / "akcli" / "schemas"


def schema_names() -> list[str]:
    """Every schema basename on EITHER side (so orphan mirrors are seen)."""
    return sorted({p.name for p in CANONICAL.glob("*.json")}
                  | {p.name for p in MIRROR.glob("*.json")})


def main(argv: list[str]) -> int:
    check = "--check" in argv
    drift: list[str] = []
    for name in schema_names():
        src, dst = CANONICAL / name, MIRROR / name
        if not src.exists():                       # orphaned mirror
            drift.append(f"{name} (orphan: no schemas/{name})")
            if not check:
                dst.unlink()
                print(f"removed orphan {name}")
            continue
        want = src.read_bytes()
        have = dst.read_bytes() if dst.exists() else None
        if have == want:
            continue
        drift.append(name)
        if not check:
            dst.write_bytes(want)
            print(f"synced {name}")
    if check and drift:
        print("mirror drift: " + ", ".join(drift))
        return 1
    if not drift:
        print(f"mirrors up to date ({len(schema_names())} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
