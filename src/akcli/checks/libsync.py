"""Embedded ``lib_symbols`` freshness lint (``akcli check --libsync``).

The inline ``(lib_symbols ...)`` cache of a ``.kicad_sch`` is a snapshot: it
never updates when the source ``.kicad_sym`` libraries do, so a schematic can
carry pin definitions the library abandoned years ago. Two findings:

* **LIB_EMBED_STALE** (WARNING) — with symbol sources given (``lib_dirs``; the
  CLI passes ``--symbols`` values, each a directory of ``.kicad_sym`` files or
  a single ``.kicad_sym``): an embedded symbol's *pin signature* (number /
  name / electrical type / position / owning unit, plus unit count) differs
  from the source definition. Graphics-only drift is deliberately NOT flagged
  here — it cannot change connectivity (``akcli relink-symbols`` still lists
  it as ``replace``).
* **LIB_EMBED_OLD_FORMAT** (NOTE) — with no sources given, a heuristic from
  version-token telltales: the document ``(version ...)`` predates the KiCad 8
  format, or embedded symbols lack the ``(exclude_from_sim ...)`` token KiCad 8
  writes on every symbol. Advises ``akcli relink-symbols`` to re-embed fresh
  definitions.

Both passes read the raw document (not the normalized model) because the
findings are about the cache blocks themselves; missing source libraries are
silently skipped here — ``relink-symbols`` reports them as ``missing-lib``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import model, relink
from ..errors import AkcliError
from ..readers import kicad as _krd
from ..readers import kicad_lib, sexpr
from ..readers.kicad_lib import _read_text
from ..report import Finding, Severity, anchor

LIB_EMBED_STALE = "LIB_EMBED_STALE"          # WARNING: pin signature drifted
LIB_EMBED_OLD_FORMAT = "LIB_EMBED_OLD_FORMAT"  # NOTE: cache predates current format

# First `(version ...)` stamp of the KiCad 8 schematic format — the release
# that also introduced the per-symbol `(exclude_from_sim ...)` token.
_V8_FORMAT = 20231120


def run(path: os.PathLike | str, lib_dirs: object = None) -> list[Finding]:
    """Lint one ``.kicad_sch``'s embedded symbol cache; returns findings."""
    p = Path(os.fspath(path))
    if p.suffix.lower() != ".kicad_sch":
        return [Finding(
            LIB_EMBED_STALE, Severity.INFO,
            "libsync check supports .kicad_sch only; skipped", refs=[str(p)],
        )]
    doc = sexpr.parse(_read_text(p))
    libsyms = doc.find("lib_symbols")
    cached = libsyms.find_all("symbol") if libsyms is not None else []
    if not cached:
        return []
    if lib_dirs:
        return _stale_findings(doc, libsyms, cached, relink.resolve_lib_dirs(lib_dirs))
    return _old_format_findings(doc, cached)


def _first_instance_pos(doc: sexpr.SNode, lib_id: str) -> tuple[float, float] | None:
    """World (x_mil, y_mil) of the first placed symbol instance of ``lib_id``."""
    for sym in _krd._placed_symbols(doc):
        if (_krd._av(sym.find("lib_id"), 1) or "") == lib_id:
            at = sym.find("at")
            return (_krd._mm_to_mil(_krd._fnum(at, 1)),
                    _krd._mm_to_mil(_krd._fnum(at, 2)))
    return None


# --------------------------------------------------------------------------- #
# LIB_EMBED_STALE — pin-signature comparison against the sources
# --------------------------------------------------------------------------- #
def _pin_signature(symdef: model.SymbolDef) -> tuple:
    """The connectivity-relevant identity of a symbol definition."""
    pins = frozenset(
        (pp.number, pp.name, pp.electrical_type.value,
         pp.x_mil, pp.y_mil, pp.owner_part_id)
        for pp in symdef.pins
    )
    return (symdef.part_count, pins)


def _stale_findings(
    doc: sexpr.SNode, libsyms: sexpr.SNode, cached: list[sexpr.SNode], dirs: list[Path]
) -> list[Finding]:
    findings: list[Finding] = []
    emb_lib = kicad_lib.library_from_lib_symbols(libsyms)
    libs: dict[Path, model.Library] = {}
    for sym in cached:
        lib_id = _name(sym) or ""
        nick, colon, name = lib_id.partition(":")
        if not colon or not name:
            continue  # unqualified legacy entry: no source mapping possible
        lib_file = relink.lib_file_for(nick, dirs)
        if lib_file is None:
            continue  # relink-symbols reports missing libs
        lib = libs.get(lib_file)
        if lib is None:
            try:
                lib = libs[lib_file] = kicad_lib.read(lib_file)
            except AkcliError:
                continue  # unreadable source: not this check's finding
        try:
            # resolve() follows (extends ...) on both sides, so an unflattened
            # legacy cache entry still yields its inherited pins.
            emb = kicad_lib.resolve(lib_id, [emb_lib])
            src = kicad_lib.resolve(name, [lib])
        except AkcliError:
            continue
        emb_sig, src_sig = _pin_signature(emb), _pin_signature(src)
        if emb_sig == src_sig:
            continue
        diffs: list[str] = []
        if emb.part_count != src.part_count:
            diffs.append(f"unit count {emb.part_count} -> {src.part_count}")
        if len(emb.pins) != len(src.pins):
            diffs.append(f"pin count {len(emb.pins)} -> {len(src.pins)}")
        nums = sorted({t[0] for t in emb_sig[1] ^ src_sig[1]})
        if nums:
            more = " ..." if len(nums) > 6 else ""
            diffs.append("pins " + ", ".join(nums[:6]) + more)
        pos = _first_instance_pos(doc, lib_id)
        findings.append(Finding(
            LIB_EMBED_STALE, Severity.WARNING,
            f"embedded symbol {lib_id!r} pin signature differs from "
            f"{lib_file.name} ({'; '.join(diffs)}) — re-embed with "
            "`akcli relink-symbols`",
            refs=[lib_id, str(lib_file)],
            pos=pos,
            anchors=[anchor("component", lib_id, pos)] if pos is not None else [],
        ))
    return findings


# --------------------------------------------------------------------------- #
# LIB_EMBED_OLD_FORMAT — version-token heuristic (no sources given)
# --------------------------------------------------------------------------- #
def _old_format_findings(
    doc: sexpr.SNode, cached: list[sexpr.SNode]
) -> list[Finding]:
    ver: int | None = None
    vnode = doc.find("version")
    if vnode is not None and len(vnode) >= 2 and vnode[1].is_atom:
        try:
            ver = int(vnode[1].value or "")
        except ValueError:
            ver = None

    missing = [
        _name(sym) or "?" for sym in cached
        if sym.find("exclude_from_sim") is None
    ]
    telltales: list[str] = []
    if ver is not None and ver < _V8_FORMAT:
        telltales.append(f"document version {ver} predates {_V8_FORMAT}")
    if missing:
        more = " ..." if len(missing) > 4 else ""
        telltales.append(
            f"{len(missing)} embedded symbol(s) lack (exclude_from_sim ...): "
            + ", ".join(missing[:4]) + more
        )
    if not telltales:
        return []
    shown = missing[:8]
    old_anchors = []
    old_pos = None
    for lib_id in shown:
        p = _first_instance_pos(doc, lib_id)
        if p is None:
            continue
        old_anchors.append(anchor("component", lib_id, p))
        if old_pos is None:
            old_pos = p
    return [Finding(
        LIB_EMBED_OLD_FORMAT, Severity.NOTE,
        "embedded lib_symbols look older than the current KiCad format ("
        + "; ".join(telltales) + ") — refresh with "
        "`akcli relink-symbols <sch> --libs <symbol dir>`",
        refs=shown,
        pos=old_pos,
        anchors=old_anchors,
    )]


def _name(sym: sexpr.SNode) -> str | None:
    """Decoded name atom of a ``(symbol "Name" ...)`` node."""
    kids = sym.children or []
    if len(kids) >= 2 and kids[1].is_atom:
        return kids[1].value
    return None
