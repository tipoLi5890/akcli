"""Packaged mirror of the fab profiles under the repo-root ``examples/fab/``.

The repo-root copies are CANONICAL — a profile is ONE vendor capability
revision with its evidence (``[source] urls`` + ``retrieved_at``), and when
the vendor page changes a NEW file with a new ``id`` is cut there, never an old
revision mutated. This package is the *shipping* copy: ``akcli fab check
--profile <id>`` must work from an installed wheel, where no repo root exists,
so the TOML has to live inside the package and be listed in
``[tool.setuptools.package-data]``.

``python3 tools/examples_mirror.py build`` refreshes this directory and
``check`` is the drift gate (also asserted by ``tests/test_fab_profiles.py``),
the same mirror+check shape ``akcli.examples`` and ``akcli.schemas`` use. Bytes
are copied verbatim, so the sha256 recorded for a bundled profile is the
sha256 of its ``examples/fab`` original.

Data only: this module reads bytes and nothing else. Parsing, validation and
the id lookup live in :mod:`akcli.fab` (``bundled_profiles`` /
``resolve_profile``), which is the one place that knows what a profile is.
"""

from __future__ import annotations

from importlib import resources

__all__ = ["names", "read_bytes"]


def names() -> list[str]:
    """File names of the profiles shipped in THIS install, sorted.

    Listed from the package itself rather than a hand-kept constant, so a
    freshly mirrored profile is selectable with no second edit — and a wheel
    built without the package data offers nothing instead of advertising an id
    it cannot deliver.
    """
    root = resources.files(__name__)
    return sorted(entry.name for entry in root.iterdir()
                  if entry.is_file() and entry.name.endswith(".toml"))


def read_bytes(name: str) -> bytes:
    """The packaged bytes of profile file ``name`` (``ValueError`` if unknown).

    ``name`` is matched against :func:`names` rather than joined onto the
    package: it can originate on the command line, and a traversal-shaped
    argument must be an unknown name, never a path.
    """
    if name not in names():
        raise ValueError(f"unknown bundled fab profile file {name!r}")
    return resources.files(__name__).joinpath(name).read_bytes()
