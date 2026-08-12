#!/usr/bin/env python3
"""Compatibility entry point for the MIRROR half of ``tools/schemas.py``.

The repo root ``schemas/`` directory is CANONICAL; ``src/akcli/schemas/``
ships in wheels and must stay byte-identical (test_schema_exports enforces
it). Both the generation and the mirroring now live in :mod:`tools.schemas`,
because running one without the other leaves the wheel serving a stale
contract. Prefer::

    python3 tools/schemas.py build     # generate + mirror
    python3 tools/schemas.py check     # exit 1 on either drift

This script stays so the older invocation keeps working:

    python3 tools/sync_schemas.py          # canonical -> mirror (+ prune)
    python3 tools/sync_schemas.py --check  # exit 1 on any drift or orphan
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import schema_names, sync  # noqa: E402,F401  (re-exported)


def main(argv: list[str]) -> int:
    return sync(check="--check" in argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
