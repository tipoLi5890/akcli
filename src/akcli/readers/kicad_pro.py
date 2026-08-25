"""``.kicad_pro`` reader — the project file, finally parsed instead of globbed.

Until this module existed the project file was a *marker*: :func:`akcli.project
.resolve_root` matched its stem, :func:`akcli.libtable.discover` stored its
path, and nothing ever opened it. Yet it is the file that says which schematic
KiCad opens (``sheets[0]`` is ``[<root .kicad_sch uuid>, "Root"]``), which
libraries are pinned, and which text variables the title block expands.

**Degrade-only, never raise.** Every consumer of this module is on a path that
must keep working when the project file is truncated, hand-edited or written by
a newer KiCad: ``resolve_root`` uses it only as a *tie-break* (a bad file must
fall back to the historical stem rule, not turn `akcli read` into a parse
error), and ``project check`` exists precisely to REPORT the damage. So
:func:`read` catches everything and returns ``ok=False`` with the reason in
``error``; there is no exception path and no ``fail()`` call in this file.

Unknown keys are ignored rather than rejected: a ``.kicad_pro`` accumulates
per-KiCad-version sections (``schematic``, ``pcbnew``, ``cvpcb`` …) that akcli
neither reads nor writes, and a reader that refused them would break on every
KiCad release. Only the handful of fields akcli reasons about are extracted,
each independently type-guarded — a project whose ``text_variables`` is
somehow a list still yields usable ``sheets``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import safety

__all__ = ["KicadPro", "find", "read", "root_sheet_uuid"]


@dataclass(frozen=True)
class KicadPro:
    """One ``.kicad_pro``, as much of it as could be read.

    ``ok`` is False for every failure mode (missing, oversized, undecodable,
    invalid JSON, not a JSON object) with a human-readable ``error``; the
    remaining fields then hold their empty defaults, so callers can read them
    unconditionally and only branch on ``ok`` when they want to *report*.
    """

    path: Path
    ok: bool = False
    error: str | None = None
    meta_filename: str | None = None
    version: int | None = None
    #: ``[(uuid, name), …]`` — index 0 is the root sheet KiCad opens.
    sheets: list[tuple[str, str]] = field(default_factory=list)
    text_variables: dict[str, str] = field(default_factory=dict)
    #: Net-class names declared under ``net_settings.classes``.
    net_classes: list[str] = field(default_factory=list)
    pinned_symbol_libs: list[str] = field(default_factory=list)
    pinned_footprint_libs: list[str] = field(default_factory=list)


def _fail(path: Path, reason: str) -> KicadPro:
    return KicadPro(path=path, ok=False, error=reason)


def _str_list(value: object) -> list[str]:
    """Every string in ``value`` when it is a list; ``[]`` for anything else."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _sheets(value: object) -> list[tuple[str, str]]:
    """``[[uuid, name], …]`` -> ``[(uuid, name), …]``, skipping bad rows.

    KiCad writes two-element lists; a row that is not a ``[str, str]`` pair
    cannot name a sheet, so it is dropped rather than poisoning the list a
    caller indexes into.
    """
    out: list[tuple[str, str]] = []
    if not isinstance(value, list):
        return out
    for row in value:
        if (isinstance(row, (list, tuple)) and len(row) >= 2
                and isinstance(row[0], str) and isinstance(row[1], str)):
            out.append((row[0], row[1]))
    return out


def _net_classes(value: object) -> list[str]:
    """Net-class names from ``net_settings.classes`` (KiCad 7+ shape)."""
    if not isinstance(value, dict):
        return []
    out: list[str] = []
    classes = value.get("classes")
    if isinstance(classes, list):
        for entry in classes:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                out.append(entry["name"])
    return out


def read(path: os.PathLike | str) -> KicadPro:
    """Parse ``path`` as a ``.kicad_pro``. Never raises.

    The size guard is the same allocation cap the S-expression readers use
    (:data:`akcli.safety.MAX_FILE_BYTES`), applied by ``stat`` *before* the
    read so a hostile file is refused rather than loaded — but reported as
    ``ok=False`` instead of the ``KICAD_SEXPR_TOOBIG`` refusal
    ``safety.read_bytes_guarded`` would raise, because every caller here is a
    degrade path.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        return _fail(p, f"{p.name}: {getattr(exc, 'strerror', None) or exc}")
    if size > safety.MAX_FILE_BYTES:
        return _fail(p, f"{p.name}: {size} bytes exceeds the "
                        f"{safety.MAX_FILE_BYTES}-byte read cap")
    try:
        text = p.read_bytes().decode("utf-8")
    except OSError as exc:
        return _fail(p, f"{p.name}: {getattr(exc, 'strerror', None) or exc}")
    except UnicodeDecodeError as exc:
        return _fail(p, f"{p.name} is not valid UTF-8 text (byte offset "
                        f"{exc.start}); KiCad project files are UTF-8")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return _fail(p, f"{p.name} is not valid JSON ({exc})")
    if not isinstance(doc, dict):
        return _fail(p, f"{p.name} is not a KiCad project object "
                        f"(top level is {type(doc).__name__})")

    meta = doc.get("meta")
    meta_filename: str | None = None
    version: int | None = None
    if isinstance(meta, dict):
        if isinstance(meta.get("filename"), str):
            meta_filename = meta["filename"]
        # bool is an int subclass — a JSON `true` is not a schema version
        if isinstance(meta.get("version"), int) and not isinstance(
                meta.get("version"), bool):
            version = meta["version"]

    libraries = doc.get("libraries")
    libraries = libraries if isinstance(libraries, dict) else {}

    text_vars = doc.get("text_variables")
    text_variables = ({k: v for k, v in text_vars.items()
                       if isinstance(k, str) and isinstance(v, str)}
                      if isinstance(text_vars, dict) else {})

    return KicadPro(
        path=p,
        ok=True,
        error=None,
        meta_filename=meta_filename,
        version=version,
        sheets=_sheets(doc.get("sheets")),
        text_variables=text_variables,
        net_classes=_net_classes(doc.get("net_settings")),
        pinned_symbol_libs=_str_list(libraries.get("pinned_symbol_libs")),
        pinned_footprint_libs=_str_list(libraries.get("pinned_footprint_libs")),
    )


def root_sheet_uuid(pro: KicadPro) -> str | None:
    """The UUID of the root schematic ``pro`` binds to, or ``None``.

    ``None`` covers both "the file could not be read" and "it names no sheets"
    — callers that must tell those apart look at ``pro.ok`` first.
    """
    if not pro.sheets:
        return None
    return pro.sheets[0][0] or None


def find(project: os.PathLike | str) -> Path | None:
    """The ``.kicad_pro`` for a directory (or the file itself), else ``None``.

    Same rule :func:`akcli.libtable.discover` applies, deliberately: a path
    that IS a ``.kicad_pro`` is returned as-is (any other file yields ``None``
    — a ``.kicad_sch`` is not a project), and a directory yields the first
    ``.kicad_pro`` in sorted order. Sorted, not arbitrary, so two runs over a
    two-project directory agree with each other and with ``libtable``.
    """
    p = Path(project)
    if p.is_file():
        return p if p.suffix == ".kicad_pro" else None
    try:
        return next(iter(sorted(p.glob("*.kicad_pro"))), None)
    except OSError:  # pragma: no cover - unreadable directory
        return None
