"""Loading the published JSON schemas (packaged mirror first, repo fallback).

``schemas/ops.schema.json`` and ``schemas/ops.capabilities.json`` are
GENERATED from :mod:`akcli.opspec` (``python3 tools/schemas.py build``); this
module only reads whichever copy the running install ships.
"""

from __future__ import annotations

import json
from pathlib import Path


def _schemas_dir() -> Path:
    # repo_root/schemas, anchored on the PACKAGE location (src/akcli) rather
    # than this module's own depth: this module lives one level deeper than
    # the old ops.py, and the source-tree fallback must not care (wheels never
    # get here — they hit the packaged mirror first).
    import akcli
    pkg_dir = Path(akcli.__file__).resolve().parent      # src/akcli
    return pkg_dir.parent.parent / "schemas"             # src -> repo root


def _schema_text(filename: str) -> str:
    """Schema JSON text: packaged copy first, repo-root ``schemas/`` fallback.

    The packaged mirror (:mod:`akcli.schemas`) ships in wheels where the repo
    root does not exist; the repo-root copy stays CANONICAL
    (test_schema_exports asserts the two are byte-identical).
    """
    try:
        from importlib import resources
        node = resources.files("akcli.schemas") / filename
        if node.is_file():
            return node.read_text(encoding="utf-8")
    except (ImportError, ModuleNotFoundError, OSError):
        pass
    return (_schemas_dir() / filename).read_text(encoding="utf-8")


def load_capabilities() -> dict:
    """Load ``schemas/ops.capabilities.json`` (per-executor support matrix)."""
    return json.loads(_schema_text("ops.capabilities.json"))


def load_schema() -> dict:
    """Load ``schemas/ops.schema.json`` (the op-list JSON schema)."""
    return json.loads(_schema_text("ops.schema.json"))
