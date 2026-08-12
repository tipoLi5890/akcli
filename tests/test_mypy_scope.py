"""The mypy beachhead can only grow — and its complement is explicit.

``[tool.mypy].files`` is a whitelist, so a NEW module is silently untyped by
default, and a module MOVED out of a listed path silently drops out of the
check (mypy does not fail on a missing path pattern the way pytest fails on
a missing test).  This census makes both failure modes loud:

* every product module is either typed (covered by ``files``) or listed in
  ``_UNTYPED_PREFIXES`` — no third state;
* the untyped-module count is pinned exactly; typing a module means
  LOWERING the pin (the progress bar of the 0.21 mypy program), adding an
  untyped module means consciously raising it in review.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "akcli"

# Directory/file prefixes (relative to src/akcli) that are known-untyped.
# Shrink as batches B0..B10 land.
_UNTYPED_PREFIXES = (
    "cli.py", "__init__.py", "__main__.py",
    "commands/", "webui/", "drivers/", "adapters/",
    "readers/",          # except sexpr.py (typed, listed in files)
    "writers/",          # except geometry.py / symbol_lib.py (incl. handlers/)
    "checks/",           # except diff.py
    "review/", "sim/",
    "sexpr/writer.py",
    # R7 half 2 split ops.py into the akcli.ops package (base/vocab/lints/
    # template/macros/groups/validate/schema_io); the DECLARATION it now
    # derives from (opspec.py) is typed and lives in [tool.mypy].files.
    "ops/",
    "report.py", "exporters.py", "fab.py", "relink.py",
    "libtable.py", "project.py", "bom_policy.py", "schemas/__init__.py",
)

# 0.21 batch R3b split writers/kicad.py into 13 modules (context, anchors,
# properties, placement, registry, executor + handlers/*): the same untyped
# code, now countable per concern.  sexpr/build.py came out of that split
# TYPED, so it is not in the tally.
# R7 half 2: opspec.py became typed (-1) and ops.py became the 9-module
# akcli.ops package (-1 +9) — the same untyped code, now countable per concern.
_UNTYPED_COUNT = 140          # lower this as modules get typed; never raise


def _product_files() -> set[Path]:
    return {p for p in SRC.rglob("*.py")
            if "_vendor" not in p.parts and "__pycache__" not in p.parts}


def _typed_files() -> set[Path]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    typed: set[Path] = set()
    for entry in cfg["tool"]["mypy"]["files"]:
        p = ROOT / entry
        if p.is_dir():
            typed |= {f for f in p.rglob("*.py") if "__pycache__" not in f.parts}
        elif p.is_file():
            typed.add(p)
        else:
            raise AssertionError(
                f"[tool.mypy].files entry does not exist: {entry} — a moved "
                "module silently dropped out of type checking")
    return typed


def test_every_module_is_typed_or_declared_untyped():
    untyped = _product_files() - _typed_files()
    strays = sorted(
        p.relative_to(SRC).as_posix() for p in untyped
        if not any(p.relative_to(SRC).as_posix().startswith(pre)
                   for pre in _UNTYPED_PREFIXES))
    assert not strays, (
        "module(s) neither in [tool.mypy].files nor declared in "
        f"_UNTYPED_PREFIXES: {strays}"
    )
    assert len(untyped) == _UNTYPED_COUNT, (
        f"untyped-module count changed: {len(untyped)} != {_UNTYPED_COUNT}. "
        "Typed some modules? lower the pin. Added an untyped module? raise "
        "it consciously (and ask whether it should be typed instead)."
    )


def test_no_fully_typed_prefix_lingers():
    untyped_rel = {p.relative_to(SRC).as_posix()
                   for p in _product_files() - _typed_files()}
    stale = [pre for pre in _UNTYPED_PREFIXES
             if not any(u.startswith(pre) for u in untyped_rel)]
    assert not stale, f"fully-typed prefixes — shrink _UNTYPED_PREFIXES: {stale}"
