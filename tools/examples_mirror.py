#!/usr/bin/env python3
"""Mirror ``examples/circuits/*/ops.json`` into the packaged ``akcli.examples``.

``examples/circuits/`` is the canonical, CI-applied corpus (``ops.json`` +
``expected_nets.json`` + ``README.md``; ``tests/test_examples.py`` applies each
op-list to a blank sheet and compares the resulting nets). But
``akcli project new --example NAME`` has to work from an installed wheel, where
no repo root exists — so the ``ops.json`` half is mirrored into
``src/akcli/examples/<name>/ops.json`` and shipped as package data.

Same two-mode shape as ``tools/schemas.py`` (and the same failure it prevents:
a stale mirror ships yesterday's payload out of the wheel while the repo copy
looks fine)::

    python3 tools/examples_mirror.py build   # refresh the packaged mirror
    python3 tools/examples_mirror.py check   # exit 1 on drift (CI gate)

The comparison is a two-way SET comparison over example names, not a one-way
copy: a mirrored example whose canonical source was renamed or deleted is an
ORPHAN — the wheel would keep offering ``--example`` a name the repo no longer
knows — so orphans are pruned (or reported under ``check``).

Bytes are compared and copied verbatim: the example JSON is authored by hand
and never reformatted here, so ``build`` is a no-op whenever the two agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "circuits"                 # canonical
MIRROR = ROOT / "src" / "akcli" / "examples"            # packaged in wheels

_USAGE = "usage: python3 tools/examples_mirror.py {build|check}"


def source_names() -> list[str]:
    """Example names in the canonical corpus (those that have an ops.json)."""
    if not SOURCE.is_dir():
        return []
    return sorted(d.name for d in SOURCE.iterdir()
                  if d.is_dir() and (d / "ops.json").is_file())


def mirror_names() -> list[str]:
    """Example names currently packaged (so orphans are visible)."""
    if not MIRROR.is_dir():
        return []
    return sorted(d.name for d in MIRROR.iterdir()
                  if d.is_dir() and (d / "ops.json").is_file())


def sync(*, check: bool) -> int:
    """Copy every canonical ``ops.json`` into the mirror, pruning orphans."""
    drift: list[str] = []
    wanted = source_names()
    for name in sorted(set(wanted) | set(mirror_names())):
        src = SOURCE / name / "ops.json"
        dst = MIRROR / name / "ops.json"
        if name not in wanted:                          # orphaned mirror
            drift.append(f"{name} (orphan: no examples/circuits/{name}/ops.json)")
            if not check:
                dst.unlink()
                try:
                    dst.parent.rmdir()
                except OSError:                         # not empty: leave it
                    pass
                print(f"removed orphan {name}")
            continue
        want = src.read_bytes()
        have = dst.read_bytes() if dst.exists() else None
        if have == want:
            continue
        drift.append(name)
        if not check:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(want)
            print(f"synced {name}/ops.json")
    if check and drift:
        print("example mirror drift (refresh with `python3 "
              "tools/examples_mirror.py build`): " + ", ".join(drift))
        return 1
    if not drift:
        print(f"example mirror up to date ({len(wanted)} example(s))")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "build":
        return sync(check=False)
    if cmd == "check":
        return sync(check=True)
    print(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
