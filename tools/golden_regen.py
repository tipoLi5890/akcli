#!/usr/bin/env python3
"""Regenerate the golden-corpus snapshots (tests/golden/).

Dev-side tool, not shipped. Runs the frozen command set over the corpus
fixtures IN-PROCESS (same code path CI asserts through) and rewrites the
stored snapshots. Run it only when an output change is intentional, review
the diff like source, and commit it with the change that caused it:

    PYTHONPATH=src python3 tools/golden_regen.py             # bless: write + diff
    PYTHONPATH=src python3 tools/golden_regen.py --check     # diff only, exit 1 on drift
    PYTHONPATH=src python3 tools/golden_regen.py --bless-all # allow a wide sweep

Blessing is explicit: write mode prints the unified diff of every snapshot it
changed (so the blessing is visible in the terminal, not just in `git diff`),
and ``--check`` regenerates in memory only — it writes nothing and exits 1 if
any snapshot would change.

Blessing is also *bounded*: a run that would rewrite more than
``BLESS_LIMIT`` snapshots refuses to write anything and exits 1 unless
``--bless-all`` is passed. A deliberate output change touches a handful of
snapshots; a whole-corpus rewrite is the signature of an accidental global
change (a formatting tweak, a stale checkout, a half-finished refactor) being
laundered into "the goldens said so".

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

# Max snapshots a single un-flagged bless may rewrite (see module docstring).
BLESS_LIMIT = 5


def _diff(rel: str, old: str, new: str) -> list[str]:
    """Deterministic unified diff, no timestamps (byte-stable output)."""
    return list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="do not write; print the diff and exit 1 on drift")
    ap.add_argument("--bless-all", action="store_true",
                    help=f"permit rewriting more than {BLESS_LIMIT} snapshots in one run")
    args = ap.parse_args(argv)

    # Pass 1: regenerate in memory so the write decision sees the full blast radius.
    pending: list[tuple[Path, str, str, str]] = []  # (path, rel, old, new)
    for case in CASES:
        out = snapshot_path(case)
        rel = out.relative_to(ROOT).as_posix()
        new = capture(case)
        old = out.read_text(encoding="utf-8") if out.is_file() else ""
        if old != new:
            pending.append((out, rel, old, new))

    changed = len(pending)

    if args.check:
        for _out, rel, old, new in pending:
            sys.stdout.writelines(_diff(rel, old, new))
            print(f"DRIFT {rel}")
        if changed:
            print(f"{changed} golden snapshot(s) would change — "
                  "re-run without --check to bless them deliberately.")
            return 1
        print(f"goldens up to date ({len(CASES)} case(s))")
        return 0

    if changed > BLESS_LIMIT and not args.bless_all:
        print(f"REFUSING to bless {changed} snapshots in one run "
              f"(limit {BLESS_LIMIT}); nothing was written.")
        for _out, rel, _old, _new in pending:
            print(f"  would change {rel}")
        print("A change this wide is usually accidental. Review it "
              "(--check prints the diffs), and if every snapshot really is "
              "meant to move, re-run with --bless-all.")
        return 1

    for out, rel, old, new in pending:
        sys.stdout.writelines(_diff(rel, old, new))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(new, encoding="utf-8", newline="\n")
        print(f"wrote {rel}")

    print(f"{changed} golden snapshot(s) rewritten "
          f"({len(CASES)} case(s) checked)" if changed
          else f"no changes ({len(CASES)} case(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
