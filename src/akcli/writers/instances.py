"""Refdes / ``(instances)`` / sheet-path writer + ``#PWR`` allocation (SPEC §3.5).

A placed KiCad ``(symbol ...)`` instance carries its reference designator in **two
independent places** that KiCad keeps in lock-step:

1. the human-visible ``(property "Reference" "<ref>" ...)`` field, and
2. the machine-authoritative annotation block
   ``(instances (project "<proj>" (path "/<root-sheet-uuid>" (reference "<ref>") (unit <n>))))``.

If the second is missing or out of sync the netlist KiCad generates is *empty* or
shows ``R?`` — the failure mode called out in SPEC risk #8.  :func:`write_instance`
therefore always writes **both**, derived from the same ``ref``/``unit``.

Scope (v1): **flat schematics only**.  A request to write an instance onto a
*sub-sheet* path raises ``HIERARCHICAL_UNSUPPORTED`` (:func:`instances_path`).

Idempotency: writing the same component twice must converge.  The ``(instances)``
block is rebuilt deterministically from its inputs and replaces any prior one, and
:func:`deterministic_uuid` derives a component's instance UUID with UUIDv5 from
``(sheet_uuid, "<designator>:<op_index>")`` so re-running an op-list reuses the
exact same UUID rather than spawning duplicate symbols.

All geometry/formatting goes through :mod:`..readers.sexpr` :class:`SNode`s so the
serializer reproduces untouched nodes byte-for-byte; nodes synthesized here use
single-space separators (KiCad re-pretty-prints on its next save).
"""

from __future__ import annotations

import re
import uuid as _uuid

from ..errors import fail
from ..readers.sexpr import SNode

__all__ = [
    "write_instance",
    "alloc_pwr_ref",
    "instances_path",
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
def _q(value: object) -> str:
    """Quote ``value`` as a KiCad string atom (escaping ``\\``, ``"``, control chars)."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


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
    node = doc.find("uuid")
    val = node.children[1].value if node and len(node.children or []) >= 2 else None
    if not val:
        fail("VERIFY_FAILED", "schematic root sheet has no (uuid ...)")
    return val


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
    maxn = 0
    for ref in _all_references(doc):
        m = _PWR_RE.match(ref)
        if m:
            maxn = max(maxn, int(m.group(1)))
    return f"#PWR0{maxn + 1}"


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


def _build_instances(project: str, path: str, ref: str, unit: int) -> SNode:
    """Construct a complete ``(instances (project ... (path ...)))`` node."""
    reference = _list(_atom("reference"), _atom(_q(ref)))
    unit_node = _list(_atom("unit"), _atom(str(int(unit))))
    path_node = _list(_atom("path"), _atom(_q(path)), reference, unit_node)
    project_node = _list(_atom("project"), _atom(_q(project)), path_node)
    return _list(_atom("instances"), project_node)


def write_instance(
    doc: SNode,
    sym: SNode,
    ref: str,
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

    Idempotent: the instances block is rebuilt deterministically and replaces any
    existing one, so a repeated call leaves ``sym`` byte-identical.
    """
    proj = project if project is not None else project_name(doc)
    unit = _symbol_unit(sym)

    _set_reference_property(sym, ref)

    new_inst = _build_instances(proj, path, ref, unit)
    existing = sym.find("instances")
    if existing is None:
        _append_child(sym, new_inst, _child_indent(sym))
    else:
        idx = (sym.children or []).index(existing)
        sym.children[idx] = new_inst  # type: ignore[index]  # ws slot unchanged


# --------------------------------------------------------------------------- #
# deterministic instance UUID (idempotent op-list replay)
# --------------------------------------------------------------------------- #
def deterministic_uuid(sheet_uuid: str, designator: str, op_index: int) -> str:
    """UUIDv5 from ``(sheet_uuid, "<designator>:<op_index>")`` (SPEC §3.5).

    Replaying the same op-list against the same sheet yields the *same* symbol
    UUID, so re-running ``draw --apply`` updates the existing symbol instead of
    spawning a duplicate.  Falls back to a DNS-namespaced hash when ``sheet_uuid``
    is not a canonical UUID string.
    """
    try:
        ns = _uuid.UUID(str(sheet_uuid))
    except (ValueError, AttributeError):
        ns = _uuid.uuid5(_uuid.NAMESPACE_DNS, str(sheet_uuid))
    return str(_uuid.uuid5(ns, f"{designator}:{op_index}"))
