"""Refdes / ``(instances)`` / sheet-path writer + ``#PWR`` allocation (SPEC §3.5).

A placed KiCad ``(symbol ...)`` instance carries its reference designator in **two
independent places** that KiCad keeps in lock-step:

1. the human-visible ``(property "Reference" "<ref>" ...)`` field, and
2. the machine-authoritative annotation block
   ``(instances (project "<proj>" (path "/<root-sheet-uuid>" (reference "<ref>") (unit <n>))))``.

If the second is missing or out of sync the netlist KiCad generates is *empty* or
shows ``R?`` — the failure mode called out in SPEC risk #8.  :func:`write_instance`
therefore always writes **both**, derived from the same ``ref``/``unit``.

Scope: a file edited as its own top sheet writes the flat ``/<own-root>``
form; a CHILD sheet's new symbols join their siblings' parent-hierarchy path
set (:func:`_effective_paths`), one ``(path ...)`` entry per instantiation —
with a **per-path reference** each (0.20): a multiply-instantiated sheet
refuses a single scalar ref (``INSTANCE_REFS_REQUIRED``) and takes a
``{path: ref}`` map instead. Naming a sub-sheet path from the PARENT's
op-list still raises ``HIERARCHICAL_UNSUPPORTED`` (:func:`instances_path`) —
route the op to the child file itself (per-op ``sheet`` tag).

Idempotency: writing the same component twice must converge.  The ``(instances)``
block is rebuilt deterministically from its inputs and replaces any prior one, and
:func:`writers.identity.node_uuid` derives a component's instance UUID with UUIDv5 from
``(sheet_uuid, "<designator>:<op_index>")`` so re-running an op-list reuses the
exact same UUID rather than spawning duplicate symbols.

All geometry/formatting goes through :mod:`..sexpr.parser` :class:`SNode`s so the
serializer reproduces untouched nodes byte-for-byte; nodes synthesized here use
single-space separators (KiCad re-pretty-prints on its next save).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..errors import fail
from ..sexpr import SNode
from ..sexpr.build import _q  # noqa: F401 - the ONE quoter (coercing form)
from ..sexpr import query as _query
from . import identity

__all__ = [
    "write_instance",
    "alloc_pwr_ref",
    "alloc_pwr_refs",
    "symbol_references",
    "primary_reference",
    "instances_path",
    "sibling_paths",
    "project_name",
    "root_uuid",
    "deterministic_uuid",
    "DEFAULT_PROJECT",
]

# KiCad's project name for an as-yet-unsaved schematic.  Used only when ``doc``
# carries no existing instance from which to copy a real project name.
DEFAULT_PROJECT = "noname"

# A power-flag reference is ``#PWR0`` followed by a 1-based integer (``#PWR01`` ..
# ``#PWR09``, ``#PWR010`` ...).  The ``0`` is a literal, the tail is the counter.
_PWR_RE = re.compile(r"#PWR0(\d+)$")


# --------------------------------------------------------------------------- #
# small SNode construction / quoting helpers
# --------------------------------------------------------------------------- #

def _atom(text: str) -> SNode:
    return SNode.atom(text)


def _list(*children: SNode) -> SNode:
    """A fresh single-line list node (``(a b c)``)."""
    return SNode.make_list(list(children))


def _append_child(parent: SNode, child: SNode, before_ws: str) -> None:
    """Append ``child`` to ``parent`` keeping the ``ws``/``children`` invariant.

    ``parent.ws`` holds ``len(children)+1`` strings (``ws[i]`` precedes
    ``children[i]``; ``ws[-1]`` precedes the closing paren).  We insert the new
    leading-whitespace just before the close-paren slot.
    """
    k = len(parent.children or [])
    parent.children.append(child)  # type: ignore[union-attr]
    parent.ws.insert(k, before_ws)  # type: ignore[union-attr]


def _insert_child(parent: SNode, idx: int, child: SNode, before_ws: str = " ") -> None:
    """Insert ``child`` at ``idx`` keeping the ``ws``/``children`` invariant."""
    parent.children.insert(idx, child)  # type: ignore[union-attr]
    parent.ws.insert(idx, before_ws)  # type: ignore[union-attr]


def _child_indent(parent: SNode) -> str:
    """A sensible leading-whitespace for a newly appended child.

    Reuses the first newline-bearing inter-child whitespace already present (so a
    pretty-printed file stays pretty); falls back to a single space.
    """
    for w in (parent.ws or [])[1:]:
        if "\n" in w:
            return w
    return " "


# --------------------------------------------------------------------------- #
# doc-level accessors
# --------------------------------------------------------------------------- #
def root_uuid(doc: SNode) -> str:
    """Return the schematic's root-sheet UUID (its top-level ``(uuid ...)``)."""
    return _query.require_root_uuid(doc)


def project_name(doc: SNode, default: str = DEFAULT_PROJECT) -> str:
    """Project name to stamp into ``(instances (project NAME ...))``.

    Copies the project name from the first existing instance block in ``doc`` (so
    every symbol on a sheet agrees), else returns ``default``.
    """
    for sym in doc.find_all("symbol"):
        inst = sym.find("instances")
        if inst is None:
            continue
        proj = inst.find("project")
        if proj is not None and len(proj.children or []) >= 2:
            name = proj.children[1].value
            if name:
                return name
    return default


def instances_path(doc: SNode, sheet: str | None = "") -> str:
    """Return the flat-schematic instance path ``"/<root-sheet-uuid>"``.

    ``sheet`` is the target sheet identifier.  Flat v1 only accepts the root sheet
    (``""``, ``"/"``, the root UUID, or ``"/<root-uuid>"``); any deeper hierarchy
    raises ``HIERARCHICAL_UNSUPPORTED``.

    This is the path for a file edited as its own top sheet.  A file edited as a
    CHILD of a hierarchy needs its parents' paths instead — those cannot be
    derived from the child alone, so :func:`sibling_paths` copies them off a
    symbol already placed in the same file (see :func:`write_instance`).
    """
    ru = root_uuid(doc)
    segs = [s for s in (sheet or "").strip().strip("/").split("/") if s]
    flat = len(segs) == 0 or (len(segs) == 1 and segs[0] == ru)
    if not flat:
        fail(
            "HIERARCHICAL_UNSUPPORTED",
            f"flat-only v1: cannot write instance onto sub-sheet path {sheet!r}",
        )
    return "/" + ru


def _symbol_instance_paths(sym: SNode) -> list[str]:
    """The ``(path "...")`` strings of one placed symbol's ``(instances)``."""
    out: list[str] = []
    inst = sym.find("instances")
    if inst is None:
        return out
    for proj in inst.find_all("project"):
        for path in proj.find_all("path"):
            kids = path.children or []
            if len(kids) >= 2 and kids[1].is_atom and kids[1].value:
                out.append(kids[1].value)
    return out


def sibling_paths(doc: SNode, exclude: SNode | None = None) -> list[str]:
    """Instance paths shared by the symbols ALREADY placed in ``doc``.

    A child ``.kicad_sch`` opened on its own has no idea what its parents are:
    the hierarchy lives in the ROOT file's ``(sheet ...)`` nodes, and each of a
    child's symbols records one ``(path "/<top-root>/<sheet-uuid>…")`` per
    instantiation of that child (two entries when the sheet is instantiated
    twice).  A newly placed symbol must join the SAME set — writing the
    single-segment ``/<own-root>`` form instead left the new part on a sheet
    path KiCad does not know, which annotated as ``R?`` (and broke outright
    when the sheet was instantiated twice).

    Returned in document order for the first symbol that carries a full set;
    an empty list means "no siblings to copy" (a genuinely flat file).
    """
    for sym in doc.find_all("symbol"):
        if sym.find("lib_id") is None or sym is exclude:
            continue
        paths = _symbol_instance_paths(sym)
        if paths:
            # Preserve order, drop duplicates.
            seen: dict[str, None] = {}
            for p in paths:
                seen.setdefault(p, None)
            return list(seen)
    return []


# --------------------------------------------------------------------------- #
# reference allocation
# --------------------------------------------------------------------------- #
def _all_references(doc: SNode) -> list[str]:
    """Every reference designator currently used in ``doc`` (property + instances)."""
    refs: list[str] = []
    for sym in doc.find_all("symbol"):
        for prop in sym.find_all("property"):
            kids = prop.children or []
            if len(kids) >= 3 and kids[1].value == "Reference":
                v = kids[2].value
                if v:
                    refs.append(v)
        inst = sym.find("instances")
        if inst is None:
            continue
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                r = path.find("reference")
                if r is not None and len(r.children or []) >= 2:
                    v = r.children[1].value
                    if v:
                        refs.append(v)
    return refs


def alloc_pwr_ref(doc: SNode) -> str:
    """Allocate the next unused ``#PWR0<n>`` reference for a power symbol.

    Scans every reference in ``doc`` for the ``#PWR0<n>`` pattern and returns one
    past the current maximum (``#PWR01`` on an empty sheet).  Deterministic given
    the document, so re-running an op-list keeps allocations stable.
    """
    return alloc_pwr_refs(doc, 1)[0]


def alloc_pwr_refs(doc: SNode, count: int) -> list[str]:
    """``count`` consecutive unused ``#PWR0<n>`` references (doc-deterministic).

    The batch form serves a power port placed on a sheet file that is
    instantiated more than once: each instance needs its own reference, and
    the ops interface stays unchanged (the writer allocates one per path).
    """
    maxn = 0
    for ref in _all_references(doc):
        m = _PWR_RE.match(ref)
        if m:
            maxn = max(maxn, int(m.group(1)))
    return [f"#PWR0{maxn + 1 + i}" for i in range(max(1, int(count)))]


# --------------------------------------------------------------------------- #
# reading a symbol's per-path references
# --------------------------------------------------------------------------- #
def symbol_references(sym: SNode) -> dict[str, str]:
    """``instance path -> reference`` for every ``(path ...)`` entry on ``sym``.

    Empty when the symbol carries no ``(instances)`` block. The SINGLE source
    of truth for "what refdes does this symbol answer to" — a sheet file
    instantiated twice carries one reference PER path, and any code matching
    a symbol by refdes must accept every entry, not just the first.
    """
    out: dict[str, str] = {}
    inst = sym.find("instances")
    if inst is None:
        return out
    for proj in inst.find_all("project"):
        for path in proj.find_all("path"):
            kids = path.children or []
            pval = kids[1].value if len(kids) >= 2 and kids[1].is_atom else None
            ref = path.find("reference")
            rval = (ref.children[1].value
                    if ref is not None and len(ref.children or []) >= 2 else None)
            if pval and rval and pval not in out:
                out[pval] = rval
    return out


def primary_reference(sym: SNode) -> str | None:
    """The first path entry's reference, else the ``Reference`` property.

    The historical single-answer form — display/property semantics. Callers
    that LOCATE symbols by refdes must use :func:`symbol_references` instead.
    """
    for ref in symbol_references(sym).values():
        return ref
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


# --------------------------------------------------------------------------- #
# instance writing
# --------------------------------------------------------------------------- #
def _symbol_unit(sym: SNode) -> int:
    """The placed symbol's unit number (``(unit N)``), defaulting to 1."""
    u = sym.find("unit")
    if u is not None and len(u.children or []) >= 2:
        try:
            return int(u.children[1].value or "1")
        except (ValueError, TypeError):
            return 1
    return 1


def _set_reference_property(sym: SNode, ref: str) -> None:
    """Set (or create) ``(property "Reference" "<ref>" ...)`` on ``sym``."""
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 2 and kids[1].value == "Reference":
            ref_atom = _atom(_q(ref))
            if len(kids) >= 3:
                kids[2] = ref_atom  # replace value atom in place (ws unchanged)
            else:
                _insert_child(prop, 2, ref_atom)
            return
    # No Reference property yet: synthesize a minimal, render-valid one.
    prop = _list(
        _atom("property"),
        _atom(_q("Reference")),
        _atom(_q(ref)),
        _list(_atom("at"), _atom("0"), _atom("0"), _atom("0")),
        _list(
            _atom("effects"),
            _list(_atom("font"), _list(_atom("size"), _atom("1.27"), _atom("1.27"))),
        ),
    )
    _append_child(sym, prop, _child_indent(sym))


def _build_instances(project: str, path_refs: list[tuple[str, str]],
                     unit: int) -> SNode:
    """Construct a complete ``(instances (project ... (path ...)+))`` node.

    One ``(path ...)`` entry per ``(sheet path, reference)`` pair — a child
    sheet instantiated twice contributes two entries, and each carries its
    OWN reference (identical refs on two paths is a duplicate-refdes design;
    the caller gates that, this builder just writes what it is given).
    """
    path_nodes = [
        _list(_atom("path"), _atom(_q(path)),
              _list(_atom("reference"), _atom(_q(ref))),
              _list(_atom("unit"), _atom(str(int(unit)))))
        for path, ref in path_refs
    ]
    project_node = _list(_atom("project"), _atom(_q(project)), *path_nodes)
    return _list(_atom("instances"), project_node)


def _effective_paths(doc: SNode, sym: SNode, path: str) -> list[str]:
    """Sheet paths to record for ``sym``: its siblings' set, else ``[path]``.

    ``path`` is the flat ``/<own-root>`` form :func:`instances_path` derives.
    It is right for a file edited as its own top sheet and WRONG for a child
    sheet, where the parents' uuids are unknowable from inside the child — so
    when the file's other symbols all carry multi-segment hierarchy paths that
    ``path`` is not among, the new symbol joins them instead.  Single-segment
    sibling paths are never copied: on a flat file they either already equal
    ``path`` or are corrupt, and ``INVALID_INSTANCES_PATH`` must keep catching
    the corrupt case rather than have the writer spread it.
    """
    sibs = sibling_paths(doc, exclude=sym)
    if not sibs or path in sibs:
        return [path]
    if all(len([s for s in p.strip("/").split("/") if s]) >= 2 for p in sibs):
        return sibs
    return [path]


def write_instance(
    doc: SNode,
    sym: SNode,
    ref: str | Mapping[str, str],
    path: str,
    *,
    project: str | None = None,
) -> None:
    """Write the reference designator into ``sym`` in **both** required places.

    Writes/updates ``(property "Reference" "<ref>")`` and a fully-synced
    ``(instances (project "<proj>" (path "<path>" (reference "<ref>") (unit <n>))))``
    block.  ``path`` is the instance path from :func:`instances_path`; ``project``
    defaults to :func:`project_name` of ``doc``; ``unit`` is read from the symbol's
    own ``(unit N)``.

    On a CHILD sheet of a hierarchy the flat ``path`` is not the truth — the
    symbol has to sit on the same sheet path(s) as the parts already in the file
    (one per instantiation of that child).  :func:`_effective_paths` copies them
    off a sibling; see its docstring for when the flat form is kept.

    ``ref`` may be:

    * a **string** — legal only when the symbol sits on exactly ONE sheet
      path. On a multiply-instantiated sheet a single string would annotate
      every instance with the same reference (a duplicate-refdes design), so
      the call refuses with ``INSTANCE_REFS_REQUIRED`` and the error message
      lists the instance paths a per-path map must cover.
    * a **mapping** ``{instance path: reference}`` — authoritative: the map's
      pairs (insertion order preserved) become the ``(path ...)`` entries.
      Values must be non-empty and pairwise distinct. The ``Reference``
      property displays the FIRST pair's reference.

    Idempotent: the instances block is rebuilt deterministically and replaces any
    existing one, so a repeated call leaves ``sym`` byte-identical.
    """
    proj = project if project is not None else project_name(doc)
    unit = _symbol_unit(sym)

    if isinstance(ref, Mapping):
        if not ref:
            fail("INSTANCE_REFS_REQUIRED",
                 "an empty per-instance reference map names no instance")
        refs = list(ref.values())
        if any(not isinstance(r, str) or not r for r in refs):
            fail("INSTANCE_REFS_REQUIRED",
                 "every per-instance reference must be a non-empty string")
        if len(set(refs)) != len(refs):
            fail("INSTANCE_REFS_REQUIRED",
                 "per-instance references must be pairwise distinct — the "
                 "same reference on two instance paths is exactly the "
                 "duplicate-refdes design the map exists to prevent")
        path_refs = [(p, r) for p, r in ref.items()]
        display = path_refs[0][1]
    else:
        # An EXISTING symbol's own path set is the truth (a rename must not
        # re-derive paths from siblings); a fresh symbol has no instances yet
        # and derives its set from them.
        paths = list(symbol_references(sym)) or _effective_paths(doc, sym, path)
        if len(paths) > 1:
            listing = ", ".join(f'"{p}"' for p in paths)
            fail("INSTANCE_REFS_REQUIRED",
                 "this sheet file is instantiated more than once; a single "
                 f"designator would annotate every instance {ref!r} — pass a "
                 "per-instance map covering these instance paths: "
                 f"{{{listing}}}")
        path_refs = [(paths[0], ref)]
        display = ref

    _set_reference_property(sym, display)

    new_inst = _build_instances(proj, path_refs, unit)
    existing = sym.find("instances")
    if existing is None:
        _append_child(sym, new_inst, _child_indent(sym))
    else:
        idx = (sym.children or []).index(existing)
        sym.children[idx] = new_inst  # type: ignore[index]  # ws slot unchanged


# --------------------------------------------------------------------------- #
# deterministic instance UUID (idempotent op-list replay)
# --------------------------------------------------------------------------- #
# deterministic instance UUID (idempotent op-list replay)
# --------------------------------------------------------------------------- #
# The implementation (and every seed format) lives in writers/identity.py —
# the ONE home of node identity.  Alias kept: this module is where callers
# historically found it, and the instance-block writer is its main consumer.
deterministic_uuid = identity.node_uuid
