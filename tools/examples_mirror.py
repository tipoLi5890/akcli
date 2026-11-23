#!/usr/bin/env python3
"""Mirror the repo-root example payloads the CLI must ship inside a wheel.

Two canonical corpora live under ``examples/`` and are read from there by the
repo's own tests; an installed wheel has no repo root, so the half of each that
the CLI hands a user is mirrored into a package and shipped as package data:

==========================  =============================  ====================
canonical (edit these)      packaged mirror                consumer
==========================  =============================  ====================
``examples/circuits/*/``    ``src/akcli/examples/<name>/``  ``project new
``ops.json``                ``ops.json``                    --example NAME``
``examples/fab/*.toml``     ``src/akcli/fab_profiles/``     ``fab check
(fab profiles only)         ``*.toml``                      --profile ID``
==========================  =============================  ====================

``examples/circuits/`` is the CI-applied corpus (``ops.json`` +
``expected_nets.json`` + ``README.md``; ``tests/test_examples.py`` applies each
op-list to a blank sheet and compares the resulting nets) — only ``ops.json``
is payload. ``examples/fab/`` holds fab profiles AND an example order manifest;
only the profiles are mirrored, and a profile is recognised by its CONTENT (a
top-level ``id`` plus a ``[source]`` table — the two keys ``fab.load_profile``
refuses to do without), never by a file-name allow/deny list that a new
example would silently fall through.

Same two-mode shape as ``tools/schemas.py`` (and the same failure it prevents:
a stale mirror ships yesterday's payload out of the wheel while the repo copy
looks fine)::

    python3 tools/examples_mirror.py build   # refresh both packaged mirrors
    python3 tools/examples_mirror.py check   # exit 1 on drift (CI gate)

Each comparison is a two-way SET comparison over names, not a one-way copy: a
mirrored file whose canonical source was renamed or deleted is an ORPHAN — the
wheel would keep offering a name the repo no longer knows — so orphans are
pruned (or reported under ``check``).

Bytes are compared and copied verbatim: the payloads are authored by hand and
never reformatted here, so ``build`` is a no-op whenever the two agree (and a
bundled profile's sha256 is the sha256 of its ``examples/fab`` original).
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "circuits"                 # canonical
MIRROR = ROOT / "src" / "akcli" / "examples"            # packaged in wheels
PROFILE_SOURCE = ROOT / "examples" / "fab"              # canonical
PROFILE_MIRROR = ROOT / "src" / "akcli" / "fab_profiles"  # packaged in wheels

_USAGE = "usage: python3 tools/examples_mirror.py {build|check}"


# --------------------------------------------------------------------------- #
# example op-lists
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# fab profiles
# --------------------------------------------------------------------------- #
def _is_profile(path: Path) -> bool:
    """Is this ``examples/fab`` TOML a fab profile (vs. an order manifest)?"""
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A file we cannot classify must stop the build, not be skipped: a
        # profile with a typo would otherwise vanish from the wheel quietly.
        raise SystemExit(f"{path.relative_to(ROOT).as_posix()}: {exc}")
    return bool(doc.get("id")) and isinstance(doc.get("source"), dict)


def profile_source_names() -> list[str]:
    """File names of the fab profiles under ``examples/fab``."""
    if not PROFILE_SOURCE.is_dir():
        return []
    return sorted(p.name for p in PROFILE_SOURCE.glob("*.toml")
                  if p.is_file() and _is_profile(p))


def profile_mirror_names() -> list[str]:
    """Profile file names currently packaged (so orphans are visible)."""
    if not PROFILE_MIRROR.is_dir():
        return []
    return sorted(p.name for p in PROFILE_MIRROR.glob("*.toml") if p.is_file())


# --------------------------------------------------------------------------- #
# the one sync loop both mirrors share
# --------------------------------------------------------------------------- #
class _Mirror(NamedTuple):
    """One canonical-corpus -> packaged-mirror relationship."""

    label: str                               # noun used in the report lines
    wanted: Callable[[], list[str]]          # names the canonical corpus has
    have: Callable[[], list[str]]            # names the mirror currently has
    src: Callable[[str], Path]               # name -> canonical file
    dst: Callable[[str], Path]               # name -> mirrored file
    shown: str                               # how a synced file is printed
    prune_parent: bool                       # mirror keeps one dir per name


_MIRRORS = (
    _Mirror(label="example",
            wanted=source_names, have=mirror_names,
            src=lambda n: SOURCE / n / "ops.json",
            dst=lambda n: MIRROR / n / "ops.json",
            shown="{name}/ops.json", prune_parent=True),
    _Mirror(label="fab profile",
            wanted=profile_source_names, have=profile_mirror_names,
            src=lambda n: PROFILE_SOURCE / n,
            dst=lambda n: PROFILE_MIRROR / n,
            shown="fab profile {name}", prune_parent=False),
)


def _sync_one(m: _Mirror, *, check: bool) -> tuple[list[str], int]:
    """Bring one mirror in line with its corpus; returns (drift, corpus size).

    ``drift`` lists what differed, whether or not it was repaired.
    """
    drift: list[str] = []
    wanted = m.wanted()
    for name in sorted(set(wanted) | set(m.have())):
        src, dst = m.src(name), m.dst(name)
        if name not in wanted:                          # orphaned mirror
            drift.append(
                f"{name} (orphan: no {src.relative_to(ROOT).as_posix()})")
            if not check:
                dst.unlink()
                if m.prune_parent:
                    try:
                        dst.parent.rmdir()
                    except OSError:                     # not empty: leave it
                        pass
                print(f"removed orphan {name}")
            continue
        want = src.read_bytes()
        got = dst.read_bytes() if dst.exists() else None
        if got == want:
            continue
        drift.append(name)
        if not check:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(want)
            print("synced " + m.shown.format(name=name))
    return drift, len(wanted)


def sync(*, check: bool) -> int:
    """Refresh (or, with ``check``, only compare) BOTH packaged mirrors."""
    rc = 0
    for m in _MIRRORS:
        drift, count = _sync_one(m, check=check)
        if check and drift:
            print(f"{m.label} mirror drift (refresh with `python3 "
                  "tools/examples_mirror.py build`): " + ", ".join(drift))
            rc = 1
        elif not drift:
            print(f"{m.label} mirror up to date ({count} {m.label}(s))")
    return rc


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
