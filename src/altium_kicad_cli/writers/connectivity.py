"""Pure-Python ERC-lite — the **primary** post-write connectivity gate (SPEC §3.5).

After the op-list executor edits a ``.kicad_sch`` it re-parses the result and runs
this module *on the temp file* before ``os.replace``; nothing here shells out, so
the whole gate works **with no KiCad installed** (SPEC risk #6). Two entry points:

* :func:`verify` — read a parsed ``.kicad_sch`` :class:`SNode` tree and return a
  list of :class:`~..report.Finding`:

  - **Dangling wire endpoints.** Every terminal endpoint of every ``(wire)`` must
    be *exactly coincident* (integer-nm) with a pin, a label/global/hierarchical
    label, a junction, a ``(no_connect)``, or another wire (another wire's
    endpoint, or its mid-span — a T). An endpoint touching none of those is the
    failure mode "the wire we just drew didn't connect to anything".
  - **Duplicate UUIDs** anywhere in the document.
  - **Unresolved ``lib_id``** — a placed symbol whose ``lib_id`` is not in the
    inline ``(lib_symbols ...)`` cache (KiCad would draw a "missing symbol" box).
  - **Invalid ``(instances)`` path** — a placed symbol with no ``(instances)``
    block, or whose flat-sheet path does not reference the root sheet UUID (the
    "empty netlist / R?" failure, SPEC risk #8).
  - **No-connect conflict** — a ``(no_connect)`` sitting on a pin that is *also*
    wired (honoring ``(no_connect)`` both ways).

* :func:`auto_junctions` — mutate ``doc`` in place, inserting a ``(junction)`` at
  every genuine 3+-way meet that lacks one: ``>=3`` wire ends coinciding, a wire
  end landing on another wire's mid-span (T), a wire corner (``>=2`` ends) where a
  pin also branches off, or a pin sitting on a wire's mid-span. Pure X crossings
  (two wires passing through, no end) are deliberately **not** auto-joined — that
  would silently change the designer's intent. Junction UUIDs are derived
  deterministically from the coordinate so a re-run is idempotent.

All geometry is **integer nanometres** (SPEC §1.2); millimetre atoms are parsed
via :func:`units.mm_to_nm` and pin world coordinates via
:func:`writers.geometry.pin_world`, so coincidence is an exact integer equality.
"""

from __future__ import annotations

import uuid as _uuid
from collections import Counter

from .. import model, units
from ..errors import AkcliError
from ..model import Component
from ..readers import kicad_lib
from ..readers.sexpr import SNode
from ..report import Finding, Severity
from . import geometry

__all__ = ["verify", "auto_junctions"]

# Finding codes emitted by :func:`verify` (free-form; distinct from the frozen
# ``errors.ERROR_CODES`` exception registry).
DANGLING_ENDPOINT = "DANGLING_ENDPOINT"
DUPLICATE_UUID = "DUPLICATE_UUID"
UNRESOLVED_LIB_ID = "UNRESOLVED_LIB_ID"
INVALID_INSTANCES_PATH = "INVALID_INSTANCES_PATH"
NO_CONNECT_CONFLICT = "NO_CONNECT_CONFLICT"

# A point in the canonical +Y-down frame, integer nanometres.
Point = tuple[int, int]


# --------------------------------------------------------------------------- #
# small SNode accessors / number parsing
# --------------------------------------------------------------------------- #
def _av(node: SNode | None, idx: int) -> str | None:
    """Decoded value of child ``idx`` when present and an atom, else ``None``."""
    if node is not None and node.children and 0 <= idx < len(node.children):
        c = node.children[idx]
        if c.is_atom:
            return c.value
    return None


def _fnum(node: SNode | None, idx: int, default: float = 0.0) -> float:
    v = _av(node, idx)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _pt_nm(node: SNode | None, idx: int) -> Point | None:
    """Integer-nm point from two consecutive mm atoms starting at child ``idx``."""
    if node is None or not node.children or idx + 1 >= len(node.children):
        return None
    return (units.mm_to_nm(_fnum(node, idx)), units.mm_to_nm(_fnum(node, idx + 1)))


def _placed_symbols(doc: SNode) -> list[SNode]:
    """Top-level ``(symbol ...)`` instances (those carrying a ``(lib_id ...)``)."""
    return [s for s in doc.find_all("symbol") if s.find("lib_id") is not None]


def _root_uuid(doc: SNode) -> str | None:
    """The schematic's root-sheet UUID (top-level ``(uuid ...)``), or ``None``."""
    return _av(doc.find("uuid"), 1)


# --------------------------------------------------------------------------- #
# geometry helpers (exact integer-nm)
# --------------------------------------------------------------------------- #
def _on_segment_interior(p: Point, a: Point, b: Point) -> bool:
    """True when ``p`` lies strictly between ``a`` and ``b`` (exclusive endpoints)."""
    if p == a or p == b:
        return False
    ax, ay = a
    bx, by = b
    px, py = p
    # collinearity via exact integer cross product
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    # within the segment span (exclusive of endpoints, already handled above)
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < 0:
        return False
    sqlen = (bx - ax) ** 2 + (by - ay) ** 2
    if dot > sqlen:
        return False
    return sqlen != 0  # zero-length segment has no interior


def _hit(p: Point, pts: set[Point], tol: int) -> bool:
    """True when ``p`` coincides with a member of ``pts`` (exact when ``tol==0``)."""
    if tol <= 0:
        return p in pts
    for q in pts:
        if abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol:
            return True
    return False


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def _wires(doc: SNode) -> tuple[list[tuple[Point, Point]], list[Point]]:
    """Return ``(segments, terminals)``.

    ``segments`` is every consecutive vertex pair of every ``(wire)`` (zero-length
    pairs dropped); ``terminals`` is each wire's two end vertices (the points the
    dangling check applies to).
    """
    segs: list[tuple[Point, Point]] = []
    terminals: list[Point] = []
    for w in doc.find_all("wire"):
        pts = w.find("pts")
        if pts is None:
            continue
        verts: list[Point] = []
        for xy in pts.find_all("xy"):
            p = _pt_nm(xy, 1)
            if p is not None:
                verts.append(p)
        if not verts:
            continue
        for a, b in zip(verts, verts[1:]):
            if a != b:
                segs.append((a, b))
        terminals.append(verts[0])
        terminals.append(verts[-1])
    return segs, terminals


def _label_points(doc: SNode) -> set[Point]:
    """All net-label anchor points (local / global / hierarchical / sheet pins)."""
    pts: set[Point] = set()
    for tag in ("label", "global_label", "hierarchical_label"):
        for lb in doc.find_all(tag):
            p = _pt_nm(lb.find("at"), 1)
            if p is not None:
                pts.add(p)
    # hierarchical sheet pins (best-effort; flat v1 still records them as anchors)
    for sh in doc.find_all("sheet"):
        for pin in sh.find_all("pin"):
            p = _pt_nm(pin.find("at"), 1)
            if p is not None:
                pts.add(p)
    return pts


def _junction_points(doc: SNode) -> set[Point]:
    pts: set[Point] = set()
    for j in doc.find_all("junction"):
        p = _pt_nm(j.find("at"), 1)
        if p is not None:
            pts.add(p)
    return pts


def _no_connect_points(doc: SNode) -> set[Point]:
    pts: set[Point] = set()
    for nc in doc.find_all("no_connect"):
        p = _pt_nm(nc.find("at"), 1)
        if p is not None:
            pts.add(p)
    return pts


def _library(doc: SNode) -> model.Library:
    libsym = doc.find("lib_symbols")
    if libsym is not None:
        return kicad_lib.library_from_lib_symbols(libsym)
    return model.Library(source_path="<inline>", source_format="kicad", symbols=[])


def _instance_component(sym: SNode, lib_id: str) -> Component:
    """Minimal :class:`Component` (placement only) for :func:`geometry.pin_world`."""
    at = sym.find("at")
    px = units.nm_to_mil(units.mm_to_nm(_fnum(at, 1)))
    py = units.nm_to_mil(units.mm_to_nm(_fnum(at, 2)))
    rot = int(round(_fnum(at, 3))) % 360
    mnode = sym.find("mirror")
    mirror = (_av(mnode, 1) if mnode is not None else None) or "none"
    return Component(
        designator=_symbol_reference(sym) or "?",
        library_ref=lib_id,
        x_mil=px,
        y_mil=py,
        rotation=rot,
        mirror=mirror,
    )


def _symbol_reference(sym: SNode) -> str | None:
    inst = sym.find("instances")
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                rv = _av(path.find("reference"), 1)
                if rv:
                    return rv
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


def _pin_points(
    doc: SNode, library: model.Library
) -> tuple[set[Point], dict[Point, tuple[str, str]], list[Finding]]:
    """World-coordinate pin points + owner map + unresolved-lib_id findings."""
    pts: set[Point] = set()
    owner: dict[Point, tuple[str, str]] = {}
    findings: list[Finding] = []
    for sym in _placed_symbols(doc):
        lib_id = _av(sym.find("lib_id"), 1) or ""
        ref = _symbol_reference(sym) or "?"
        try:
            symdef = kicad_lib.resolve(lib_id, [library])
        except AkcliError:
            findings.append(
                Finding(
                    UNRESOLVED_LIB_ID,
                    Severity.ERROR,
                    f"placed symbol {ref!r} uses lib_id {lib_id!r} which is not in "
                    f"(lib_symbols ...)",
                    refs=[ref, lib_id],
                )
            )
            continue
        comp = _instance_component(sym, lib_id)
        for pin in symdef.pins:
            wp = geometry.pin_world(symdef, comp, pin)
            pts.add(wp)
            owner.setdefault(wp, (ref, pin.number))
    return pts, owner, findings


def _all_uuids(doc: SNode) -> list[str]:
    """Every ``(uuid "...")`` value in the tree (iterative, no native recursion)."""
    out: list[str] = []
    stack: list[SNode] = [doc]
    while stack:
        nd = stack.pop()
        if nd.is_atom:
            continue
        if nd.tag == "uuid":
            v = _av(nd, 1)
            if v:
                out.append(v)
        for c in nd.children or ():
            if c.is_list:
                stack.append(c)
    return out


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def verify(doc: SNode, *, tol_nm: int = 0) -> list[Finding]:
    """Run the pure-Python connectivity gate over a parsed ``.kicad_sch`` ``doc``.

    Returns a list of :class:`~..report.Finding`; an empty list means the document
    is electrically self-consistent (no dangling wires, duplicate UUIDs, unresolved
    symbols or broken instance paths). Never shells out — safe with no KiCad.
    """
    findings: list[Finding] = []

    # --- duplicate UUIDs ---------------------------------------------------- #
    for value, count in Counter(_all_uuids(doc)).items():
        if count > 1:
            findings.append(
                Finding(
                    DUPLICATE_UUID,
                    Severity.ERROR,
                    f"uuid {value!r} appears {count} times (must be unique)",
                    refs=[value],
                )
            )

    library = _library(doc)
    pin_points, pin_owner, lib_findings = _pin_points(doc, library)
    findings.extend(lib_findings)

    # --- invalid instances path -------------------------------------------- #
    root_uuid = _root_uuid(doc)
    for sym in _placed_symbols(doc):
        ref = _symbol_reference(sym) or "?"
        inst = sym.find("instances")
        if inst is None:
            findings.append(
                Finding(
                    INVALID_INSTANCES_PATH,
                    Severity.ERROR,
                    f"placed symbol {ref!r} has no (instances ...) block "
                    f"(its netlist reference would be empty)",
                    refs=[ref],
                )
            )
            continue
        if root_uuid is None:
            continue
        ok = False
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                raw = _av(path, 1) or ""
                first = raw.strip("/").split("/")[0] if raw.strip("/") else ""
                if first == root_uuid:
                    ok = True
        if not ok:
            findings.append(
                Finding(
                    INVALID_INSTANCES_PATH,
                    Severity.ERROR,
                    f"placed symbol {ref!r} instances path does not reference the "
                    f"root sheet uuid {root_uuid!r}",
                    refs=[ref, root_uuid],
                )
            )

    # --- connectivity sets -------------------------------------------------- #
    segs, terminals = _wires(doc)
    label_points = _label_points(doc)
    junction_points = _junction_points(doc)
    nc_points = _no_connect_points(doc)

    end_counts: Counter[Point] = Counter()
    for a, b in segs:
        end_counts[a] += 1
        end_counts[b] += 1

    def _on_other_wire(p: Point) -> bool:
        """``p`` connects to a wire other than (just) terminating itself."""
        if end_counts.get(p, 0) >= 2:
            return True  # another wire endpoint also lands here
        for a, b in segs:
            if _on_segment_interior(p, a, b):
                return True  # T-junction into a wire's mid-span
        return False

    # --- dangling wire endpoints ------------------------------------------- #
    reported: set[Point] = set()
    for p in terminals:
        if p in reported:
            continue
        connected = (
            _hit(p, pin_points, tol_nm)
            or _hit(p, label_points, tol_nm)
            or _hit(p, junction_points, tol_nm)
            or _hit(p, nc_points, tol_nm)
            or _on_other_wire(p)
        )
        if not connected:
            reported.add(p)
            findings.append(
                Finding(
                    DANGLING_ENDPOINT,
                    Severity.ERROR,
                    f"wire endpoint at {_fmt(p)} is not connected to any pin, "
                    f"label, junction or other wire",
                    refs=[_fmt(p)],
                )
            )

    # --- no-connect honoring (conflict: NC on a wired pin) ----------------- #
    for p in nc_points:
        wired = end_counts.get(p, 0) >= 1 or any(
            _on_segment_interior(p, a, b) for a, b in segs
        )
        if wired and _hit(p, pin_points, tol_nm):
            ref, num = pin_owner.get(p, ("?", "?"))
            findings.append(
                Finding(
                    NO_CONNECT_CONFLICT,
                    Severity.WARNING,
                    f"pin {ref}.{num} at {_fmt(p)} has a (no_connect) but is also "
                    f"wired",
                    refs=[f"{ref}.{num}", _fmt(p)],
                )
            )

    return findings


def _fmt(p: Point) -> str:
    """Render an integer-nm point as a KiCad-style ``(x y)`` mm string."""
    return f"({units.nm_to_mm_str(p[0])} {units.nm_to_mm_str(p[1])})"


# --------------------------------------------------------------------------- #
# auto_junctions
# --------------------------------------------------------------------------- #
def _needs_junction(ends: int, through: int, pins: int) -> bool:
    """KiCad-faithful "3+-way meet" rule (see module docstring).

    A junction is required when:
      * ``>=3`` wire endpoints coincide, or
      * a wire endpoint lands where another wire passes through (T), or
      * a wire corner (``>=2`` ends) has a pin branching off, or
      * a pin sits on a wire's mid-span.
    Pure X crossings (two pass-throughs, no end) are intentionally excluded.
    """
    if ends >= 3:
        return True
    if ends >= 1 and through >= 1:
        return True
    if ends >= 2 and pins >= 1:
        return True
    if through >= 1 and pins >= 1:
        return True
    return False


def auto_junctions(doc: SNode, *, tol_nm: int = 0) -> None:
    """Insert missing ``(junction)`` nodes at genuine 3+-way meets, in place.

    Idempotent: junction UUIDs are derived deterministically from the coordinate,
    and a point that already carries a junction is skipped, so re-running converges.
    """
    segs, _terminals = _wires(doc)
    if not segs:
        return

    pin_points, _owner, _lf = _pin_points(doc, _library(doc))
    existing = _junction_points(doc)

    end_counts: Counter[Point] = Counter()
    for a, b in segs:
        end_counts[a] += 1
        end_counts[b] += 1

    # Candidate points: every wire-segment endpoint (a T point is the arm wire's
    # endpoint; an X-only crossing is never an endpoint, so excluded) PLUS every
    # pin lying on a segment's mid-span — eeschema only connects a pin tap at a
    # wire endpoint or a junction, so the mid-span-pin rule below never fired
    # while candidates were endpoints alone (the placed part read as connected
    # by akcli but dangled in KiCad).
    cand_set = set(end_counts.keys())
    for p in pin_points:
        if p not in cand_set and any(_on_segment_interior(p, a, b) for a, b in segs):
            cand_set.add(p)
    candidates = sorted(cand_set)
    root_uuid = _root_uuid(doc)

    for p in candidates:
        if _hit(p, existing, tol_nm):
            continue
        ends = end_counts.get(p, 0)
        through = sum(1 for a, b in segs if _on_segment_interior(p, a, b))
        pins = 1 if _hit(p, pin_points, tol_nm) else 0
        if not _needs_junction(ends, through, pins):
            continue
        node = _make_junction(p, _junction_uuid(root_uuid, p))
        _append_top(doc, node)
        existing.add(p)


# --------------------------------------------------------------------------- #
# SNode construction helpers
# --------------------------------------------------------------------------- #
def _q(value: object) -> str:
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _atom(text: str) -> SNode:
    return SNode.atom(text)


def _list(*children: SNode) -> SNode:
    return SNode.make_list(list(children))


def _make_junction(p: Point, uuid_str: str) -> SNode:
    at = _list(_atom("at"), _atom(units.nm_to_mm_str(p[0])), _atom(units.nm_to_mm_str(p[1])))
    diameter = _list(_atom("diameter"), _atom("0"))
    color = _list(_atom("color"), _atom("0"), _atom("0"), _atom("0"), _atom("0"))
    uuidn = _list(_atom("uuid"), _atom(_q(uuid_str)))
    return _list(_atom("junction"), at, diameter, color, uuidn)


def _junction_uuid(root_uuid: str | None, p: Point) -> str:
    """Deterministic UUIDv5 for an auto-inserted junction (idempotent replay)."""
    try:
        ns = _uuid.UUID(str(root_uuid))
    except (ValueError, AttributeError, TypeError):
        ns = _uuid.uuid5(_uuid.NAMESPACE_DNS, str(root_uuid))
    return str(_uuid.uuid5(ns, f"junction:{p[0]}:{p[1]}"))


def _child_indent(parent: SNode) -> str:
    """Leading whitespace for a newly appended child (reuse a pretty newline run)."""
    for w in (parent.ws or [])[1:]:
        if "\n" in w:
            return w
    return " "


def _append_top(doc: SNode, child: SNode) -> None:
    """Append ``child`` as a top-level node, preserving the ``ws``/``children`` rule."""
    k = len(doc.children or [])
    doc.children.append(child)  # type: ignore[union-attr]
    doc.ws.insert(k, _child_indent(doc))  # type: ignore[union-attr]
