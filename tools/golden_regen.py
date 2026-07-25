#!/usr/bin/env python3
"""Regenerate the golden-corpus snapshots (tests/golden/).

Dev-side tool, not shipped. Runs the frozen command set over the corpus
fixtures IN-PROCESS (same code path CI asserts through) and rewrites the
stored snapshots. Run it only when an output change is intentional, review
the diff like source, and commit it with the change that caused it:

    PYTHONPATH=src python3 tools/golden_regen.py           # bless: write + diff
    PYTHONPATH=src python3 tools/golden_regen.py --check   # diff only, exit 1 on drift

Blessing is explicit: write mode prints the unified diff of every snapshot it
changed (so the blessing is visible in the terminal, not just in `git diff`),
and ``--check`` regenerates in memory only — it writes nothing and exits 1 if
any snapshot would change.

The corpus table lives in tests/test_golden_corpus.py (single source of
truth); this tool imports it.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_golden_corpus import CASES, capture, snapshot_path  # noqa: E402


def _diff(rel: str, old: str, new: str) -> list[str]:
    """Deterministic unified diff, no timestamps (byte-stable output)."""
    return list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="do not write; print the diff and exit 1 on drift")
    args = ap.parse_args(argv)

    changed = 0
    for case in CASES:
        out = snapshot_path(case)
        rel = out.relative_to(ROOT).as_posix()
        new = capture(case)
        old = out.read_text(encoding="utf-8") if out.is_file() else ""
        if old == new:
            continue
        changed += 1
        sys.stdout.writelines(_diff(rel, old, new))
        if args.check:
            print(f"DRIFT {rel}")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(new, encoding="utf-8", newline="\n")
            print(f"wrote {rel}")

    if args.check:
        if changed:
            print(f"{changed} golden snapshot(s) would change — "
                  "re-run without --check to bless them deliberately.")
            return 1
        print(f"goldens up to date ({len(CASES)} case(s))")
        return 0
    print(f"{changed} golden snapshot(s) rewritten "
          f"({len(CASES)} case(s) checked)" if changed
          else f"no changes ({len(CASES)} case(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
