"""``.kicad_sym`` + inline ``(lib_symbols ...)`` -> :class:`model.Library` (SPEC §3.4).

KiCad keeps every symbol's electrical truth (pin names, numbers and **electrical
types**) in the *library* definition, not on the placed instance: a schematic
``(symbol ... (pin "1" (uuid ...)))`` carries only a pin *number* + uuid. The
reader (:mod:`.kicad`) therefore resolves pin types out of the ``lib_symbols``
cache at read time, and this module is the resolver.

Responsibilities:

* :func:`read` — parse a standalone ``.kicad_sym`` file into a
  :class:`model.Library` (one :class:`model.SymbolDef` per top-level
  ``(symbol ...)``).
* :func:`symbols_from_lib_symbols` / :func:`library_from_lib_symbols` — build the
  same ``SymbolDef`` list from a schematic's inline ``(lib_symbols ...)`` node.
* :func:`resolve` — locate a ``lib_id`` across a list of source libraries and
  resolve ``(extends ...)`` derived symbols (e.g. ``C_Polarized`` extends ``C``)
  by inheriting the base symbol's pins.
* :func:`pin_offsets` — symbol-local pin connection points (mils), used by the
  reader to compute world coordinates and (later) by the writer geometry.

Each :class:`model.SymbolDef` keeps the **raw** ``(symbol ...)`` :class:`SNode`
in ``body_sexpr`` so the future writer ``lib_cache`` can copy it verbatim.

Coordinates: KiCad library pins are in millimetres with the library convention
**+Y up**; we store the pin connection point (``(at x y angle)``) in mils,
unchanged in orientation (still +Y up / symbol-local). The reader applies the
Y flip + instance transform when placing.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import model, units
from ..errors import fail
from ..safety import MAX_FILE_BYTES
from . import sexpr

__all__ = [
    "read",
    "resolve",
    "pin_offsets",
    "unit_pins",
    "symbols_from_lib_symbols",
    "library_from_lib_symbols",
]

# Depth of an ``(extends ...)`` / sub-symbol resolution chain we will follow
# before declaring a malformed/cyclic library (defensive; real chains are 1-2).
_MAX_EXTENDS = 64


def _read_text(path: os.PathLike | str) -> str:
    """Read a UTF-8 text file, bounded by ``MAX_FILE_BYTES``."""
    data = Path(path).read_bytes()
    if len(data) > MAX_FILE_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"file exceeds {MAX_FILE_BYTES} bytes")
    return data.decode("utf-8")


def _mm_to_mil(mm: float) -> float:
    """Convert millimetres to mils via integer nanometres (no float drift)."""
    return units.nm_to_mil(units.mm_to_nm(mm))


def _atom_value(node: sexpr.SNode, idx: int) -> str | None:
    """Value of child ``idx`` when it is an atom, else ``None``."""
    if node.children and 0 <= idx < len(node.children):
        c = node.children[idx]
        if c.is_atom:
            return c.value
    return None


def _first_atom_after_head(node: sexpr.SNode) -> str | None:
    """First atom child after the head symbol (a pin's electrical-type token)."""
    for c in (node.children or [])[1:]:
        if c.is_atom:
            return c.value
    return None


def _parse_pin(node: sexpr.SNode) -> model.Pin:
    """Parse a ``(pin <etype> <style> (at x y a) (length l) (name ..) (number ..))``."""
    etype_tok = _first_atom_after_head(node)
    etype = model.KICAD_PINTYPE.get(etype_tok or "", model.PinType.UNSPECIFIED)

    at = node.find("at")
    x_mm = float(_atom_value(at, 1) or 0.0) if at else 0.0
    y_mm = float(_atom_value(at, 2) or 0.0) if at else 0.0

    name_node = node.find("name")
    name = _atom_value(name_node, 1) if name_node else None

    num_node = node.find("number")
    number = (_atom_value(num_node, 1) if num_node else None) or ""

    return model.Pin(
        number=number,
        name=name,
        x_mil=_mm_to_mil(x_mm),
        y_mil=_mm_to_mil(y_mm),
        electrical_type=etype,
    )


def _sub_unit_style(name: str | None) -> tuple[int | None, int | None]:
    """``(unit, style)`` of a ``Name_<unit>_<style>`` sub-symbol name, else ``(None, None)``."""
    parts = (name or "").rsplit("_", 2)
    if len(parts) == 3:
        try:
            return int(parts[1]), int(parts[2])
        except ValueError:
            pass
    return None, None


def _collect_pins(sym: sexpr.SNode) -> list[model.Pin]:
    """Collect every ``(pin ...)`` of a symbol, including nested unit sub-symbols.

    KiCad nests pins one level deep inside ``(symbol "Name_<unit>_<style>" ...)``
    sub-symbols; some hand-written libs put them directly. We walk descendant
    ``(symbol ...)`` nodes iteratively (no native recursion) and preserve order.

    Only **body style 1** is collected: a ``_<unit>_2`` sub-symbol is the same
    physical unit drawn in its alternate (DeMorgan) representation, so counting
    it would duplicate every pin (each duplicate then collides downstream — the
    writer's per-pin UUIDs are keyed by pin number, so a 74xx placement was
    refused with ``DUPLICATE_UUID``). Pins also record their owning unit in
    ``owner_part_id`` (a ``_0_*`` sub-symbol is common to ALL units → unit 0,
    matched by every unit in :func:`unit_pins`).
    """
    pins: list[model.Pin] = []
    stack: list[tuple] = [(iter(sym.children or []), 1)]
    while stack:
        it, unit = stack[-1]
        try:
            nd = next(it)
        except StopIteration:
            stack.pop()
            continue
        if not nd.is_list:
            continue
        if nd.tag == "pin":
            pin = _parse_pin(nd)
            pin.owner_part_id = unit
            pins.append(pin)
        elif nd.tag == "symbol":
            u, style = _sub_unit_style(_atom_value(nd, 1))
            if style is not None and style >= 2:
                continue  # alternate body style: same pins, skip
            stack.append((iter(nd.children or []), u if u is not None else unit))
    return pins


def unit_pins(symdef: model.SymbolDef, unit: int) -> list[model.Pin]:
    """Pins a placed instance of ``unit`` actually exposes on the canvas.

    A KiCad schematic symbol instance is ONE unit of the part: eeschema draws
    (and connects) only that unit's pins, plus any ``_0_*`` common pins. The
    other units' pins exist only on their own placed instances — treating them
    as present at every instance mapped all four 74xx gates onto one body and
    merged unrelated pins into one net.
    """
    return [p for p in symdef.pins if p.owner_part_id in (0, unit)]


def _part_count(sym: sexpr.SNode) -> int:
    """Largest unit number among ``Name_<unit>_<style>`` sub-symbols (>= 1)."""
    maxu = 1
    for c in sym.children or []:
        if c.is_list and c.tag == "symbol":
            sub = _atom_value(c, 1)
            if not sub:
                continue
            parts = sub.rsplit("_", 2)
            if len(parts) == 3:
                try:
                    u = int(parts[1])
                except ValueError:
                    continue
                if u > maxu:
                    maxu = u
    return maxu


def _parse_symbol(sym: sexpr.SNode) -> model.SymbolDef:
    """Build a :class:`model.SymbolDef` from a ``(symbol "Name" ...)`` node."""
    name = _atom_value(sym, 1) or ""
    ext_node = sym.find("extends")
    extends = _atom_value(ext_node, 1) if ext_node else None
    return model.SymbolDef(
        name=name,
        lib_id=name,
        pins=_collect_pins(sym),
        part_count=_part_count(sym),
        extends=extends,
        body_sexpr=sym,
    )


def symbols_from_lib_symbols(libsym_node: sexpr.SNode) -> list[model.SymbolDef]:
    """Parse every ``(symbol ...)`` of an inline ``(lib_symbols ...)`` node."""
    return [_parse_symbol(s) for s in libsym_node.find_all("symbol")]


def library_from_lib_symbols(
    libsym_node: sexpr.SNode, source_path: str = "<inline>"
) -> model.Library:
    """Wrap an inline ``(lib_symbols ...)`` node as a :class:`model.Library`."""
    return model.Library(
        source_path=source_path,
        source_format="kicad",
        symbols=symbols_from_lib_symbols(libsym_node),
    )


def read(path: os.PathLike | str) -> model.Library:
    """Read a standalone ``.kicad_sym`` file into a :class:`model.Library`."""
    root = sexpr.parse(_read_text(path))
    if root.tag != "kicad_symbol_lib":
        fail("ALTIUM_MALFORMED", f"not a kicad_symbol_lib: root tag {root.tag!r}")
    symbols = [_parse_symbol(s) for s in root.find_all("symbol")]
    return model.Library(
        source_path=str(path),
        source_format="kicad",
        symbols=symbols,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _unqual(name: str | None) -> str:
    """Library-unqualified symbol name (``Device:R`` -> ``R``; ``R`` -> ``R``)."""
    return name.split(":")[-1] if name else (name or "")


def _find_symbol(
    lib_id: str, sources: list[model.Library]
) -> model.SymbolDef | None:
    """Find a symbol by exact name/lib_id, falling back to unqualified match.

    The exact pass matches an inline cache (``Device:R``) or a fully qualified
    request; the fallback lets a bare ``(extends "C")`` base name resolve against
    a qualified ``Device:C`` cache entry (and vice-versa for standalone libs).
    """
    for lib in sources:
        for s in lib.symbols:
            if s.name == lib_id or s.lib_id == lib_id:
                return s
    target = _unqual(lib_id)
    for lib in sources:
        for s in lib.symbols:
            if _unqual(s.name) == target:
                return s
    return None


def _resolve_pins(
    sym: model.SymbolDef, sources: list[model.Library]
) -> tuple[list[model.Pin], int]:
    """Resolve a symbol's effective pins (+ part_count) following ``(extends)``.

    A derived KiCad symbol inherits its base's pins wholesale (it may only add
    graphics/properties), so we walk the extends chain to the first ancestor that
    actually declares pins. Cycles / over-long chains raise ``ALTIUM_MALFORMED``.
    """
    cur: model.SymbolDef | None = sym
    seen: set[str] = set()
    hops = 0
    while cur is not None:
        if cur.pins:
            return cur.pins, cur.part_count
        if not cur.extends:
            return [], cur.part_count
        if cur.name in seen or hops > _MAX_EXTENDS:
            fail("ALTIUM_MALFORMED", f"cyclic/over-long extends chain at {cur.name!r}")
        seen.add(cur.name)
        hops += 1
        base = _find_symbol(cur.extends, sources)
        if base is None:
            fail("SYMBOL_NOT_FOUND", f"extends base {cur.extends!r} of {cur.name!r}")
        cur = base
    return [], sym.part_count


def resolve(lib_id: str, sources: list[model.Library]) -> model.SymbolDef:
    """Resolve ``lib_id`` to a fully populated :class:`model.SymbolDef`.

    Finds the symbol across ``sources`` and, when it is an ``(extends ...)``
    derived part, returns a copy whose ``pins`` are inherited from the base.
    ``body_sexpr`` is preserved from the *requested* symbol (the writer copies the
    derived body and its base separately). Raises ``SYMBOL_NOT_FOUND`` on a miss.
    """
    sym = _find_symbol(lib_id, sources)
    if sym is None:
        fail("SYMBOL_NOT_FOUND", f"symbol {lib_id!r} not in any source library")
    if not sym.extends and sym.pins:
        return sym
    pins, part_count = _resolve_pins(sym, sources)
    return model.SymbolDef(
        name=sym.name,
        lib_id=sym.lib_id,
        pins=pins,
        part_count=part_count,
        extends=sym.extends,
        body_sexpr=sym.body_sexpr,
    )


def pin_offsets(sym: model.SymbolDef) -> list[tuple[str, float, float]]:
    """Symbol-local pin connection points as ``(number, x_mil, y_mil)`` (+Y up).

    Operates on ``sym.pins`` (call :func:`resolve` first if ``sym`` may be an
    ``(extends ...)`` derived symbol).
    """
    return [(p.number, p.x_mil, p.y_mil) for p in sym.pins]
