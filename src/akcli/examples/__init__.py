"""Packaged mirror of the repo-root ``examples/circuits/*/ops.json``.

The repo-root copies are CANONICAL — they are the ones CI applies to a blank
sheet and compares against ``expected_nets.json`` (``tests/test_examples.py``),
so an example that drifts from working op-list JSON fails there first. This
package is the *shipping* copy: ``akcli project new --example NAME`` must work
from an installed wheel, where no repo root exists, so the JSON has to live
inside the package and be listed in ``[tool.setuptools.package-data]``.

``python3 tools/examples_mirror.py build`` refreshes this directory and
``check`` is the drift gate (also asserted by ``tests/test_examples.py``), the
same mirror+check shape ``tools/schemas.py`` uses for ``akcli.schemas``.

Only ``ops.json`` is mirrored: ``README.md`` and ``expected_nets.json`` are
documentation and test fixtures, not payload the CLI ever hands a user.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["names", "ops_path", "ops_text"]

# ``parent``, never ``parents[N]``: this anchor must survive a repackaging
# (see tests/test_gate_coverage.py's parents-whitelist gate).
_DIR = Path(__file__).parent


def names() -> list[str]:
    """Example names available in THIS install, sorted.

    Read from the packaged directory rather than a hand-kept constant so a
    freshly mirrored example is offered by ``--example`` with no second edit —
    and so a wheel built without the package data offers nothing instead of
    advertising a name it cannot deliver.
    """
    if not _DIR.is_dir():  # pragma: no cover - broken install
        return []
    return sorted(d.name for d in _DIR.iterdir()
                  if d.is_dir() and (d / "ops.json").is_file())


def ops_path(name: str) -> Path | None:
    """The packaged ``ops.json`` for ``name``, or ``None`` if unknown.

    ``name`` is matched against :func:`names` rather than joined onto the
    directory: an example name reaches here straight from the command line, and
    ``--example ../../etc/passwd`` must be an unknown name, not a path.
    """
    if name not in names():
        return None
    return _DIR / name / "ops.json"


def ops_text(name: str) -> str:
    """The packaged example op-list as text (``ValueError`` if unknown).

    The CLI validates ``--example`` against :func:`names` before it gets here,
    so the raise is an engine-level assertion, not a user-facing error.
    """
    path = ops_path(name)
    if path is None:
        raise ValueError(f"unknown example {name!r} (have: {', '.join(names())})")
    return path.read_text(encoding="utf-8")
