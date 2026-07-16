#!/usr/bin/env python3
"""Regenerate the golden-corpus snapshots (tests/golden/).

Dev-side tool, not shipped. Runs the frozen command set over the corpus
fixtures IN-PROCESS (same code path CI asserts through) and rewrites the
stored snapshots. Run it only when an output change is intentional, review
the diff like source, and commit it with the change that caused it:

    PYTHONPATH=src python3 tools/golden_regen.py

The corpus table lives in tests/test_golden_corpus.py (single source of
truth); this tool imports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_golden_corpus import CASES, capture, snapshot_path  # noqa: E402


def main() -> int:
    for case in CASES:
        out = snapshot_path(case)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(capture(case), encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
