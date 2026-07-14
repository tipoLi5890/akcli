"""Altium ``.PrjPcb`` project reader (INI-style text).

Extracts the pieces net inference cares about:

* the **document list** (``[DocumentN] DocumentPath=...``, backslash paths
  resolved relative to the project file), and
* ``PowerPortNamesTakePriority`` (any section) — feeds
  :data:`model.NetPrimitives.power_priority` so power-port names outrank net
  labels when the project says so.

``read()`` then loads the project's **top schematic** — the ``.SchDoc`` that no
other project schematic instantiates via a sheet symbol — through the
hierarchical :func:`readers.altium_sch.read`. Zero third-party dependencies.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from ..errors import fail
from ..model import Schematic

__all__ = ["read", "read_project"]

_TRUE = {"1", "T", "TRUE", "YES", "Y"}


def read_project(path: os.PathLike | str) -> dict:
    """Parse the project file into ``{documents, schematics, power_priority, options}``."""
    p = Path(os.fspath(path))
    text = p.read_bytes().decode("utf-8-sig", "replace")
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error as exc:
        fail("ALTIUM_MALFORMED", f"unparsable .PrjPcb: {exc}")

    documents: list[Path] = []
    options: dict[str, str] = {}
    for section in cp.sections():
        for key, value in cp.items(section):
            if key.lower() == "documentpath" and value:
                documents.append(p.parent / value.replace("\\", "/"))
            else:
                options.setdefault(key, value)

    power_priority = options.get("powerportnamestakepriority", "0").strip().upper() in _TRUE
    return {
        "documents": documents,
        "schematics": [d for d in documents if d.suffix.lower() == ".schdoc"],
        "power_priority": power_priority,
        "options": options,
    }


def _referenced_children(schematic: Path) -> set[str]:
    """Lower-case child file names this schematic instantiates via sheet symbols."""
    from . import altium_sch

    try:
        recs = altium_sch._read_fileheader(schematic)
    except Exception:
        return set()
    out = set()
    for child in altium_sch._sheet_children(recs):
        if child["file"]:
            out.add(Path(child["file"].replace("\\", "/")).name.lower())
    return out


def read(path: os.PathLike | str) -> Schematic:
    """Read a ``.PrjPcb`` project: hierarchical read of its top schematic."""
    from . import altium_sch

    proj = read_project(path)
    schematics = proj["schematics"]
    if not schematics:
        fail("ALTIUM_MALFORMED", f"no .SchDoc documents listed in {path}")

    referenced: set[str] = set()
    for sch in schematics:
        if sch.exists():
            referenced |= _referenced_children(sch)
    tops = [s for s in schematics if s.name.lower() not in referenced]
    top = tops[0] if tops else schematics[0]
    if not top.exists():
        raise FileNotFoundError(f"{top} (listed in {path})")

    sch = altium_sch.read(top, power_priority=proj["power_priority"])
    sch.source_path = str(path)
    return sch
