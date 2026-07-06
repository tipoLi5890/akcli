"""``lib_symbols`` cache resolution & copy for the KiCad writer (SPEC §3.5).

A ``.kicad_sch`` is self-contained: every symbol it instantiates is copied,
verbatim and **library-qualified**, into a top-level ``(lib_symbols ...)`` node.
A placed ``(symbol (lib_id "Device:R") ...)`` instance carries only pin numbers;
KiCad (and our ERC) reads the symbol's *electrical truth* — pin names, numbers
and **electrical types** — out of this cache. So before the executor can place a
component, the symbol must be present in ``lib_symbols``.

:func:`ensure_cached` is that step. For a requested ``lib_id`` (e.g.
``"Device:C_Polarized"``) it:

* locates the symbol across the configured *sources* (``.kicad_sym`` libraries
  and/or a template ``.kicad_sch``'s inline ``lib_symbols``) via
  :mod:`..readers.kicad_lib`, raising ``SYMBOL_NOT_FOUND`` on a miss;
* copies the raw ``(symbol ...)`` body (``SymbolDef.body_sexpr``) **deep**, so the
  source library node is never mutated and **full pin electrical types are
  preserved** (ERC needs them);
* **requalifies the parent symbol name** to ``Nick:Name`` (the instance's
  ``lib_id``) while keeping the child unit sub-symbol names unqualified
  (``Name_0_1`` / ``Name_1_1``);
* **flattens an ``(extends ...)`` derived symbol** into a standalone definition:
  the base's units/pins/graphics are copied under the derived name (unit
  sub-symbols renamed ``Base_u_s`` -> ``Derived_u_s``) with the derived symbol's
  own properties/settings overlaid, and the ``(extends)`` clause dropped. This is
  what KiCad itself does when caching a derived symbol into a schematic —
  KiCad's loader does *not* resolve a bare ``(extends "Base")`` against a
  library-qualified cached base, so an unflattened cache entry loses its pins
  (``lib_symbol_mismatch`` + every wire to the part dangling in eeschema);
* **dedups by qualified lib_id**: a symbol already present is left untouched, so
  ``ensure_cached`` is idempotent and safe to call once per placed component.

Geometry-free: this module only shuffles S-expression nodes; no coordinates are
touched. The serializer (``sexpr.dumps`` / the writer's ``sexpr_writer``) renders
the mutated ``doc`` back to text.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .. import model
from ..errors import fail
from ..readers import kicad_lib, sexpr
from ..readers.sexpr import SNode
from ..safety import MAX_FILE_BYTES

__all__ = ["ensure_cached"]

# Guard against a pathological / cyclic ``(extends ...)`` chain. Real chains are
# 1-2 deep; we follow at most this many hops before declaring the library bad.
_MAX_EXTENDS = 64

# Trailing ``_<unit>_<body-style>`` of a unit sub-symbol name (``R_0_1`` -> ``_0_1``).
_UNIT_SUFFIX_RE = re.compile(r"_\d+_\d+$")

# Default whitespace used when synthesizing a fresh ``(lib_symbols ...)`` node or
# when a sibling-derived indent cannot be recovered (KiCad uses tab indentation).
_DOC_CHILD_WS = "\n\t"      # one level deep: a direct child of (kicad_sch ...)
_SYM_CHILD_WS = "\n\t\t"    # two levels deep: a (symbol ...) inside (lib_symbols ...)

# Tags that mark the start of a schematic's *body* (instances/graphics). A freshly
# created ``lib_symbols`` is inserted just before the first of these so it lands in
# its canonical position right after the header block.
_BODY_TAGS = frozenset(
    {
        "junction",
        "no_connect",
        "bus_entry",
        "wire",
        "bus",
        "polyline",
        "text",
        "label",
        "global_label",
        "hierarchical_label",
        "symbol",
        "sheet",
        "sheet_instances",
        "symbol_instances",
    }
)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def ensure_cached(
    doc: SNode,
    lib_id: str,
    sources: object,
) -> None:
    """Ensure ``lib_id`` (and any ``(extends)`` base) is present in ``doc``'s cache.

    ``doc`` is the parsed root :class:`SNode` of a ``.kicad_sch`` document; it is
    mutated in place (its ``(lib_symbols ...)`` node is created if absent and the
    resolved symbol(s) appended). ``sources`` is an iterable whose items are any
    mix of:

    * a :class:`model.Library` (already parsed),
    * a path to a ``.kicad_sym`` library file, or
    * a path to a template ``.kicad_sch`` (its inline ``lib_symbols`` is used).

    Raises ``SYMBOL_NOT_FOUND`` if ``lib_id`` (or an ``extends`` base) resolves in
    none of the sources. Idempotent: a symbol already cached is left untouched.
    """
    libs = _coerce_sources(sources)
    libsyms = _ensure_lib_symbols_node(doc)
    cached = _existing_names(libsyms)
    # The requested symbol keeps the instance's exact qualified id; a derived
    # symbol is flattened into a single standalone entry (no base is cached).
    _cache_symbol(libsyms, request=lib_id, qualified=lib_id, libs=libs, cached=cached)


# --------------------------------------------------------------------------- #
# Caching core
# --------------------------------------------------------------------------- #
def _cache_symbol(
    libsyms: SNode,
    request: str,
    qualified: str,
    libs: list[model.Library],
    cached: set[str],
) -> None:
    """Resolve ``request`` and copy it as ``qualified`` (flattening any base)."""
    if qualified in cached:
        return  # already present (pre-existing or just added) -> dedup

    sym = _find_symbol(request, libs)
    if sym is None:
        fail("SYMBOL_NOT_FOUND", f"symbol {request!r} not found in any source library")
    if not isinstance(sym.body_sexpr, SNode):
        # Only KiCad-sourced SymbolDefs carry a raw body we can copy.
        fail("SYMBOL_NOT_FOUND", f"symbol {request!r} has no copyable KiCad body")

    body = _flattened_body(sym, libs)
    _set_symbol_name(body, qualified)
    _append_symbol(libsyms, body)
    cached.add(qualified)


# --------------------------------------------------------------------------- #
# Derived-symbol flattening (the KiCad-save behavior)
# --------------------------------------------------------------------------- #
def _flattened_body(sym: model.SymbolDef, libs: list[model.Library]) -> SNode:
    """Deep-copied standalone body for ``sym``, its ``(extends)`` chain flattened.

    KiCad never caches a derived symbol as-is into a ``.kicad_sch``: its save
    path flattens (``LIB_SYMBOL::Flatten``), and its loader does not resolve a
    bare ``(extends "Base")`` against a library-qualified cached base — an
    unflattened entry therefore loses its unit sub-symbols and pins, and every
    wire to the placed part dangles in eeschema. We mirror the flatten: start
    from the root base's body, rename its unit sub-symbols to the derived name,
    then overlay each descendant's own children (derived-most last) with
    property-by-name / tag replacement, dropping ``(extends)`` itself.
    """
    if not sym.extends:
        return _clone(sym.body_sexpr)

    # Resolve derived -> ... -> root base (cycle- and depth-guarded).
    chain: list[model.SymbolDef] = [sym]
    seen = {_unqual(sym.name)}
    cur = sym
    while cur.extends:
        if len(chain) > _MAX_EXTENDS:
            fail("SYMBOL_NOT_FOUND", f"extends chain too deep resolving {sym.name!r}")
        base = _find_symbol(cur.extends, libs)
        if base is None:
            fail(
                "SYMBOL_NOT_FOUND",
                f"extends base {cur.extends!r} of {cur.name!r} not found in any source library",
            )
        if not isinstance(base.body_sexpr, SNode):
            fail("SYMBOL_NOT_FOUND", f"extends base {cur.extends!r} has no copyable KiCad body")
        if _unqual(base.name) in seen:
            fail("SYMBOL_NOT_FOUND", f"cyclic extends chain at {base.name!r}")
        seen.add(_unqual(base.name))
        chain.append(base)
        cur = base

    derived_unqual = _unqual(sym.name)
    body = _clone(chain[-1].body_sexpr)
    _rename_child_units(body, derived_unqual)
    # Overlay from the root-most derived layer up to ``sym`` (nearest wins).
    for layer in chain[-2::-1]:
        _overlay_symbol(body, layer.body_sexpr, derived_unqual)
    return body


def _overlay_symbol(body: SNode, overlay: SNode, derived_unqual: str) -> None:
    """Merge ``overlay``'s list children into ``body`` (KiCad flatten semantics).

    ``(property "Name" ...)`` replaces the same-named property (else is inserted
    after the last property); a ``(symbol "..._u_s")`` unit replaces the unit
    with the same ``_u_s`` suffix (else appended), renamed to the derived name;
    ``(extends ...)`` is dropped; any other tag replaces its first same-tag
    sibling (else is inserted before the properties).
    """
    for ch in list(overlay.children or []):
        if not ch.is_list:
            continue
        tag = ch.tag
        if tag == "extends":
            continue
        copy = _clone(ch)
        if tag == "property":
            _merge_property(body, copy)
        elif tag == "symbol":
            _merge_child_unit(body, copy, derived_unqual)
        else:
            _merge_plain(body, copy)


def _merge_property(body: SNode, prop: SNode) -> None:
    name = _property_name(prop)
    last_prop = -1
    for i, ch in enumerate(body.children or []):
        if ch.is_list and ch.tag == "property":
            last_prop = i
            if name is not None and _property_name(ch) == name:
                body.children[i] = prop
                return
    if last_prop >= 0:
        _insert_child(body, last_prop + 1, prop)
    else:
        _insert_child(body, _first_child_unit_index(body), prop)


def _merge_child_unit(body: SNode, unit: SNode, derived_unqual: str) -> None:
    suffix = _unit_suffix(_symbol_name(unit))
    if suffix is not None:
        _set_symbol_name(unit, f"{derived_unqual}{suffix}")
    for i, ch in enumerate(body.children or []):
        if ch.is_list and ch.tag == "symbol" and _unit_suffix(_symbol_name(ch)) == suffix:
            body.children[i] = unit
            return
    _insert_child(body, len(body.children), unit)


def _merge_plain(body: SNode, node: SNode) -> None:
    for i, ch in enumerate(body.children or []):
        if ch.is_list and ch.tag == node.tag:
            body.children[i] = node
            return
    # New setting: keep it in the header region, before the first property/unit.
    idx = _first_child_unit_index(body)
    for i, ch in enumerate(body.children or []):
        if ch.is_list and ch.tag == "property":
            idx = i
            break
    _insert_child(body, idx, node)


def _rename_child_units(body: SNode, derived_unqual: str) -> None:
    """Rename every ``(symbol "Base_u_s")`` child to ``Derived_u_s`` in place."""
    for ch in body.children or []:
        if ch.is_list and ch.tag == "symbol":
            suffix = _unit_suffix(_symbol_name(ch))
            if suffix is not None:
                _set_symbol_name(ch, f"{derived_unqual}{suffix}")


def _unit_suffix(name: str | None) -> str | None:
    """The trailing ``_<unit>_<style>`` of a unit sub-symbol name (or ``None``)."""
    if not name:
        return None
    m = _UNIT_SUFFIX_RE.search(name)
    return m.group(0) if m else None


def _property_name(prop: SNode) -> str | None:
    kids = prop.children or []
    if len(kids) >= 2 and kids[1].is_atom:
        return kids[1].value
    return None


def _first_child_unit_index(body: SNode) -> int:
    for i, ch in enumerate(body.children or []):
        if ch.is_list and ch.tag == "symbol":
            return i
    return len(body.children or [])


def _insert_child(body: SNode, idx: int, node: SNode) -> None:
    """Insert ``node`` at ``idx`` with the body's child indentation."""
    indent = _body_child_indent(body)
    body.children.insert(idx, node)
    # ``ws[i]`` precedes ``children[i]``; the trailing entry belongs to ")".
    body.ws.insert(idx, indent)


def _body_child_indent(body: SNode) -> str:
    for w in (body.ws or [])[1:-1]:
        if "\n" in w:
            return w
    return _SYM_CHILD_WS + "\t"


def _find_symbol(name: str, libs: list[model.Library]) -> model.SymbolDef | None:
    """Find a symbol by exact name/lib_id, falling back to an unqualified match.

    Mirrors :func:`kicad_lib._find_symbol` without depending on a private name:
    an exact pass matches ``Device:R`` or a fully qualified request; the fallback
    lets a bare ``(extends "C")`` resolve against a qualified ``Device:C`` entry
    and a qualified ``Device:R`` request resolve against an unqualified ``R`` lib.
    """
    for lib in libs:
        for s in lib.symbols:
            if s.name == name or s.lib_id == name:
                return s
    target = _unqual(name)
    for lib in libs:
        for s in lib.symbols:
            if _unqual(s.name) == target:
                return s
    return None


# --------------------------------------------------------------------------- #
# ``lib_symbols`` node management
# --------------------------------------------------------------------------- #
def _ensure_lib_symbols_node(doc: SNode) -> SNode:
    """Return ``doc``'s ``(lib_symbols ...)`` node, creating + inserting one if absent."""
    if not isinstance(doc, SNode) or doc.is_atom:
        fail("SYMBOL_NOT_FOUND", "document is not a parsed kicad_sch node")
    existing = doc.find("lib_symbols")
    if existing is not None:
        return existing

    # Fresh node: ``(lib_symbols\n\t)`` — head atom + a closing-paren indent so the
    # first appended symbol nests cleanly.
    node = SNode(False, children=[SNode.atom("lib_symbols")], ws=["", _DOC_CHILD_WS])
    _insert_doc_child(doc, node)
    return node


def _insert_doc_child(doc: SNode, node: SNode) -> None:
    """Insert ``node`` as a child of ``doc`` just before its body (instances/graphics)."""
    kids = doc.children or []
    indent = _doc_child_indent(doc)
    idx = len(kids)  # default: append before the closing paren
    for i, child in enumerate(kids):
        if child.is_list and child.tag in _BODY_TAGS:
            idx = i
            break
    kids.insert(idx, node)
    # ``ws[i]`` is the whitespace before ``children[i]``; inserting at ``idx`` pushes
    # the old ``ws[idx]`` (which belonged to the node now at ``idx+1``) along.
    doc.ws.insert(idx, indent)


def _append_symbol(libsyms: SNode, body: SNode) -> None:
    """Append a ``(symbol ...)`` ``body`` as the last child of ``libsyms``."""
    indent = _sym_child_indent(libsyms)
    libsyms.children.append(body)
    # Insert the new child's leading whitespace just before the closing-paren ws.
    libsyms.ws.insert(len(libsyms.ws) - 1, indent)


def _existing_names(libsyms: SNode) -> set[str]:
    """Set of qualified symbol names already cached in ``libsyms``."""
    out: set[str] = set()
    for s in libsyms.find_all("symbol"):
        nm = _symbol_name(s)
        if nm is not None:
            out.add(nm)
    return out


# --------------------------------------------------------------------------- #
# Node helpers
# --------------------------------------------------------------------------- #
def _symbol_name(sym: SNode) -> str | None:
    """Decoded name of a ``(symbol "Name" ...)`` node (its second child), or ``None``."""
    kids = sym.children or []
    if len(kids) >= 2 and kids[1].is_atom:
        return kids[1].value
    return None


def _set_symbol_name(sym: SNode, name: str) -> None:
    """Requalify a ``(symbol "Old" ...)`` node's name atom in place to ``name``."""
    kids = sym.children or []
    if len(kids) < 2 or not kids[1].is_atom:
        fail("SYMBOL_NOT_FOUND", "malformed symbol body (missing name atom)")
    kids[1].text = _quote(name)


def _doc_child_indent(doc: SNode) -> str:
    """Indentation of a direct child of the document (e.g. before ``(version ...)``)."""
    for w in (doc.ws or [])[:-1]:
        if "\n" in w:
            return w
    return _DOC_CHILD_WS


def _sym_child_indent(libsyms: SNode) -> str:
    """Indentation of a ``(symbol ...)`` child inside ``lib_symbols``.

    Recovered from an existing newline-bearing child whitespace (never the final
    closing-paren ws, which is one level shallower); falls back to two tabs.
    """
    for w in (libsyms.ws or [])[:-1]:
        if "\n" in w:
            return w
    return _SYM_CHILD_WS


# --------------------------------------------------------------------------- #
# Deep clone (iterative — no native recursion on attacker-influenced trees)
# --------------------------------------------------------------------------- #
def _clone(node: SNode) -> SNode:
    """Deep-copy an :class:`SNode` so the source library tree is never mutated."""
    if node.is_atom:
        return _clone_atom(node)
    root = _new_list(node)
    # Each stack frame pairs a source list node with its fresh copy; children are
    # appended in source order within each frame, so ordering is preserved.
    stack: list[tuple[SNode, SNode]] = [(node, root)]
    while stack:
        src, dst = stack.pop()
        for ch in src.children or []:
            if ch.is_atom:
                dst.children.append(_clone_atom(ch))
            else:
                copy = _new_list(ch)
                dst.children.append(copy)
                stack.append((ch, copy))
    return root


def _clone_atom(node: SNode) -> SNode:
    a = SNode.atom(node.text)
    a.prefix = node.prefix
    a.suffix = node.suffix
    return a


def _new_list(node: SNode) -> SNode:
    nl = SNode(False, children=[], ws=list(node.ws or []))
    nl.prefix = node.prefix
    nl.suffix = node.suffix
    return nl


# --------------------------------------------------------------------------- #
# Name / quoting helpers
# --------------------------------------------------------------------------- #
def _unqual(name: str | None) -> str:
    """Library-unqualified symbol name (``Device:R`` -> ``R``)."""
    return name.split(":")[-1] if name else ""


def _quote(s: str) -> str:
    """Quote a symbol name the KiCad way (escape ``\\`` then ``"`` and newlines)."""
    body = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{body}"'


# --------------------------------------------------------------------------- #
# Source coercion
# --------------------------------------------------------------------------- #
def _coerce_sources(sources: object) -> list[model.Library]:
    """Normalize ``sources`` into a list of :class:`model.Library`.

    Accepts a single source or an iterable of sources; each item is a
    :class:`model.Library`, a ``.kicad_sym`` path, or a template ``.kicad_sch``
    path (its inline ``lib_symbols`` is harvested).
    """
    if sources is None:
        return []
    if isinstance(sources, (model.Library, str, bytes, os.PathLike)):
        items: list = [sources]
    else:
        items = list(sources)  # type: ignore[arg-type]
    return [_coerce_one(item) for item in items]


def _coerce_one(src: object) -> model.Library:
    if isinstance(src, model.Library):
        return src
    if not isinstance(src, (str, bytes, os.PathLike)):
        fail("SYMBOL_NOT_FOUND", f"unsupported symbol source: {type(src).__name__}")
    p = Path(os.fspath(src))
    if p.suffix.lower() == ".kicad_sym":
        return kicad_lib.read(p)
    root = sexpr.parse(_read_text(p))
    if root.tag == "kicad_symbol_lib":
        return kicad_lib.read(p)
    libnode = root.find("lib_symbols")
    if libnode is None:
        fail("SYMBOL_NOT_FOUND", f"no lib_symbols in template source {p}")
    return kicad_lib.library_from_lib_symbols(libnode, str(p))


def _read_text(path: os.PathLike | str) -> str:
    """Read a UTF-8 text source bounded by ``MAX_FILE_BYTES``."""
    data = Path(path).read_bytes()
    if len(data) > MAX_FILE_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"file exceeds {MAX_FILE_BYTES} bytes")
    return data.decode("utf-8")
