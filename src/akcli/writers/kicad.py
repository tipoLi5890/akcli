"""Op-list executor → surgical ``.kicad_sch`` edits (SPEC §3.5).

This is the **executor**: it consumes a validated op-list (SPEC §2) and applies
its ops to a ``.kicad_sch`` document by *surgically* synthesizing the handful of
S-expression subtrees each op needs — never re-pretty-printing the rest of the
file (the byte-identity GATE in :mod:`..readers.sexpr` / :mod:`.sexpr_writer`
keeps untouched nodes verbatim).

Per-op behaviour (SPEC §2.2):

* ``place_component`` — :func:`lib_cache.ensure_cached` copies the symbol (any
  ``(extends)`` chain flattened, KiCad-save style) into ``(lib_symbols ...)``,
  then a placed ``(symbol ...)``
  instance is emitted with **per-pin** ``(pin "N" (uuid ...))`` nodes and a
  reference written into BOTH the ``Reference`` property and the ``(instances)``
  block (via :mod:`.instances`).
* ``set_component_transform`` — rotate/mirror an existing placed component.
* ``set_component_parameters`` — set reference/value/footprint/custom params.
* ``add_wire`` / ``add_bus`` — emit one orthogonal segment per consecutive vertex
  pair; a ``"REF.PIN"`` endpoint is **snapped to the pin's world coordinate** via
  :func:`geometry.pin_world`, a raw ``[x,y]`` mil point is snapped to grid.
* ``add_junction`` / ``add_no_connect`` / ``add_net_label`` / ``add_text`` /
  ``add_bus_entry`` — single-node primitives.
* ``add_sheet`` — a hierarchical ``(sheet ...)`` node (Sheetname/Sheetfile,
  stroke/fill, deterministic uuid, sheet pins at computed edge coordinates, the
  ``(instances)`` page block). The referenced child ``.kicad_sch`` is authored
  separately; wires attach to a sheet pin by its ``at`` + ``offset_mil``
  coordinate (a label anchor, so the endpoint does not dangle).
* ``place_power_port`` (+ sugar ``place_gnd`` / ``place_vcc``) — a power symbol with
  an auto-allocated ``#PWR0<n>`` reference.
* ``add_net_label`` / ``place_power_port`` ``at`` also accepts
  ``"mid(REF.PIN,REF.PIN)"`` — the midpoint of two axis-aligned pins, snapped to
  the 50-mil grid along the wire axis (labels auto-orient along that axis).
* ``delete_component`` with ``"cascade": true`` also removes wires ending on a
  deleted pin plus labels/no_connects/junctions anchored there;
  ``delete_object`` alternatively takes a ``match`` selector (exactly-one).
* ``rename_net`` — rewrite matching label texts + power-port net Values.

**Atomic write with backup + verify (SPEC §3.5, risk #13).** Nothing is written in
``--dry-run`` (the default). On ``apply=True`` the sequence is: snapshot the
original (mtime + sha256 optimistic lock) → write a temp file in the SAME directory
→ ``fsync`` → **re-parse the temp and run** :func:`connectivity.verify` on it →
``os.replace`` ONLY when verify is error-free → otherwise the temp is unlinked and
the original is untouched. An op-list whose ``protocol_version`` major exceeds ours
is rejected up front with ``PROTOCOL_MISMATCH``.

All geometry is integer **nanometres** (SPEC §1.2); millimetre strings are produced
only at serialize time through :func:`units.nm_to_mm_str`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import model, units
from ..errors import AkcliError, fail
from ..errors import remediation_for as _remediation_for
from ..kicad_escape import escape_lib_id, unescape_string
from ..ops import PROTOCOL_VERSION, parse_mid_anchor, validate_oplist
from ..readers import kicad_lib, sexpr
from ..readers.sexpr import SNode
from ..report import Severity
from ..safety import MAX_FILE_BYTES
from . import connectivity, geometry, instances, lib_cache

__all__ = ["OpResult", "apply"]

# Default schematic grid (50 mil) in integer nm — raw [x,y] endpoints snap here.
_GRID_NM = geometry.DEFAULT_GRID_NM

# How many rotated backups to keep on --apply: <name>.bak (newest) plus
# .bak2..bak{depth}. Overridable via config [project] backup_depth; `akcli undo`
# walks this stack. 1 == the historical single-.bak behaviour.
_DEFAULT_BACKUP_DEPTH = 3

# Sugar power-port presets (SPEC §2.2): documented sugar over place_power_port.
_SUGAR_POWER = {
    "place_gnd": ("power:GND", "GND"),
    "place_vcc": ("power:VCC", "VCC"),
}

# Default bus-entry size: 2.54 mm @ 45° (SPEC §2.2).
_BUS_ENTRY_NM = units.mm_to_nm(2.54)


@dataclass
class OpResult:
    """Per-op result object (SPEC §2.4)."""

    op_index: int
    op: str | None
    status: str = "ok"                      # "ok" | "error"
    created_uuids: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str = ""
    remediation: str | None = None          # actionable next step, machine-readable

    def to_dict(self) -> dict:
        return {
            "op_index": self.op_index,
            "op": self.op,
            "status": self.status,
            "created_uuids": list(self.created_uuids),
            "error_code": self.error_code,
            "message": self.message,
            "remediation": self.remediation,
        }


# Per-op remediation hints live in ``errors.REMEDIATION`` (one registry for
# every surface: per-op results, CLI top level, findings) — imported above.


# --------------------------------------------------------------------------- #
# small SNode construction helpers
# --------------------------------------------------------------------------- #
def _q(value: object) -> str:
    """Quote ``value`` KiCad-style (escape ``\\``, ``"``, and control chars).

    Newline/CR/tab MUST be escaped: akcli's own lexer tolerates a raw newline
    inside a quoted atom, but eeschema does not — an unescaped multi-line text
    op produced a file KiCad refused to open while every akcli gate passed.
    """
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def _atom(text: str) -> SNode:
    return SNode.atom(text)


def _list(*children: SNode) -> SNode:
    return SNode.make_list(list(children))


def _mm(nm: int) -> SNode:
    """A bare numeric atom rendering integer nm as a KiCad mm string."""
    return _atom(units.nm_to_mm_str(int(nm)))


def _xy(pt: tuple[int, int]) -> SNode:
    return _list(_atom("xy"), _mm(pt[0]), _mm(pt[1]))


def _pts(points: list[tuple[int, int]]) -> SNode:
    return _list(_atom("pts"), *[_xy(p) for p in points])


def _stroke(width_nm: int = 0) -> SNode:
    # width 0 keeps the historical bare "0" atom so existing files replay
    # byte-identically; a non-zero width renders as a KiCad mm string.
    width = _atom("0") if not width_nm else _mm(width_nm)
    return _list(
        _atom("stroke"),
        _list(_atom("width"), width),
        _list(_atom("type"), _atom("default")),
    )


def _fill(type_: str = "none") -> SNode:
    return _list(_atom("fill"), _list(_atom("type"), _atom(type_)))


def _effects(hide: bool = False, justify: str | None = None) -> SNode:
    kids = [_atom("effects"),
            _list(_atom("font"), _list(_atom("size"), _atom("1.27"), _atom("1.27")))]
    if justify:
        kids.append(_list(_atom("justify"), *[_atom(t) for t in justify.split()]))
    if hide:
        kids.append(_list(_atom("hide"), _atom("yes")))
    return _list(*kids)


def _label_justify(tag: str, orientation: int) -> str:
    """The ``(justify ...)`` eeschema pairs with a label's angle.

    KiCad renders label text horizontal-or-vertical only (never upside-down):
    the side it extends to comes from the JUSTIFICATION, not the angle — a
    180° label without ``(justify right)`` still runs +X, straight over the
    symbol it names. eeschema's four spin styles are (0,left) (90,left)
    (180,right) (270,right); local labels additionally sit on the wire, so
    they carry ``bottom`` to lift the text above it.
    """
    j = "left" if orientation in (0, 90) else "right"
    return f"{j} bottom" if tag == "label" else j


def _uuid_node(value: str) -> SNode:
    return _list(_atom("uuid"), _atom(_q(value)))


def _at(nm_pt: tuple[int, int], angle: float = 0.0) -> SNode:
    a = float(angle)
    angle_atom = _atom(str(int(a)) if a == int(a) else repr(a))
    return _list(_atom("at"), _mm(nm_pt[0]), _mm(nm_pt[1]), angle_atom)


def _append_top(doc: SNode, child: SNode) -> None:
    """Append ``child`` as a top-level node (preserving ws/children invariant)."""
    indent = _doc_child_indent(doc)
    k = len(doc.children or [])
    doc.children.append(child)            # type: ignore[union-attr]
    doc.ws.insert(k, indent)              # type: ignore[union-attr]


def _append_top_idempotent(doc: SNode, child: SNode, uid: str) -> None:
    """Idempotent top-level append: replace any prior node with ``uid`` IN PLACE.

    Re-running an op-list yields the same deterministic ``uid`` per created node,
    so replacing makes ``draw --apply`` converge (SPEC risk #7) instead of
    accumulating duplicate wires/labels/junctions on every run. Replacement is
    in place (same child index, same leading whitespace) rather than
    remove-then-append: appending would migrate every replayed node to the end
    of the document while non-op nodes (auto-junctions) stayed put, so the first
    re-apply reordered the file and byte-idempotency only converged on the
    SECOND apply.
    """
    for i, c in enumerate(doc.children or []):
        if not c.is_list:
            continue
        u = c.find("uuid")
        if u is not None and len(u.children or []) >= 2 and u.children[1].value == uid:
            doc.children[i] = child            # type: ignore[index]
            return
    _append_top(doc, child)


def _doc_child_indent(doc: SNode) -> str:
    for w in (doc.ws or [])[1:]:
        if "\n" in w:
            return w
    return "\n\t"


# --------------------------------------------------------------------------- #
# document / instance lookups
# --------------------------------------------------------------------------- #
def _root_uuid(doc: SNode) -> str | None:
    node = doc.find("uuid")
    if node is not None and len(node.children or []) >= 2:
        return node.children[1].value
    return None


def _placed_symbols(doc: SNode) -> list[SNode]:
    return [s for s in doc.find_all("symbol") if s.find("lib_id") is not None]


def _inst_lib_id(sym: SNode) -> str:
    """A placed instance's ``(lib_id ...)`` value, **unescaped** (``{slash}`` → ``/``).

    Normalizes to the same representation the lib_symbols cache is keyed by, so
    ``find_cached``/``resolve`` agree whether the doc was written by KiCad (escaped)
    or by an older akcli (raw)."""
    node = sym.find("lib_id")
    if node is None or len(node.children or ()) < 2:
        return ""
    return unescape_string(node.children[1].value) or ""


def _symbol_reference(sym: SNode) -> str | None:
    inst = sym.find("instances")
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                ref = path.find("reference")
                if ref is not None and len(ref.children or []) >= 2:
                    v = ref.children[1].value
                    if v:
                        return v
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


def _symbol_by_ref(doc: SNode, ref: str) -> SNode | None:
    for sym in _placed_symbols(doc):
        if _symbol_reference(sym) == ref:
            return sym
    return None


def _symbols_by_ref(doc: SNode, ref: str) -> list[SNode]:
    """ALL placed instances of ``ref`` (a multi-unit part is several)."""
    return [s for s in _placed_symbols(doc) if _symbol_reference(s) == ref]


def _symbol_by_uuid(doc: SNode, uid: str) -> SNode | None:
    for sym in _placed_symbols(doc):
        u = sym.find("uuid")
        if u is not None and len(u.children or []) >= 2 and u.children[1].value == uid:
            return sym
    return None


def _symbol_unit(sym: SNode) -> int:
    node = sym.find("unit")
    if node is not None and len(node.children or []) >= 2:
        try:
            return int(float(node.children[1].value or "1"))
        except (TypeError, ValueError):
            pass
    return 1


def _inline_library(doc: SNode) -> model.Library:
    """A :class:`model.Library` view of the document's current ``(lib_symbols)``."""
    node = doc.find("lib_symbols")
    if node is not None:
        return kicad_lib.library_from_lib_symbols(node, "<inline>")
    return model.Library(source_path="<inline>", source_format="kicad", symbols=[])


# --------------------------------------------------------------------------- #
# per-apply symbol-resolution context (perf: SPEC §3.5)
# --------------------------------------------------------------------------- #
def _ctx_new() -> dict:
    """Per-``apply`` memo: ``lib_id -> SymbolDef`` resolved from ONE cache body.

    Re-parsing the whole (growing) inline ``lib_symbols`` per placed op made a
    large op-list quadratic (a 478-placement sheet took minutes); resolving each
    ``lib_id`` once, from just its own cached body, is O(1) per op. Safe because
    a cache entry never changes within a run (``ensure_cached`` dedups by id).
    """
    return {"symdefs": {}, "text_anchors": []}


def _symdef_from_body(body: SNode, lib_id: str) -> model.SymbolDef:
    """Resolve a :class:`SymbolDef` from a single cached ``(symbol ...)`` body."""
    wrapper = SNode.make_list([SNode.atom("lib_symbols"), body])
    lib = kicad_lib.library_from_lib_symbols(wrapper, "<inline>")
    return kicad_lib.resolve(lib_id, [lib])


def _ctx_symdef(doc: SNode, ctx: dict, lib_id: str) -> model.SymbolDef:
    """Memoized symbol resolution against the document's ``lib_symbols`` cache."""
    sd = ctx["symdefs"].get(lib_id)
    if sd is None:
        body = lib_cache.find_cached(doc.find("lib_symbols"), lib_id)
        if body is None:
            fail("SYMBOL_NOT_FOUND", f"lib_id {lib_id!r} not in (lib_symbols ...) cache")
        sd = _symdef_from_body(body, lib_id)
        ctx["symdefs"][lib_id] = sd
    return sd


def _instance_component(sym: SNode, lib_id: str):
    """Minimal placement-only Component for :func:`geometry.pin_world`."""
    at = sym.find("at")

    def _f(idx: int, default: float = 0.0) -> float:
        if at is not None and at.children and idx < len(at.children):
            v = at.children[idx].value
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        return default

    px = units.nm_to_mil(units.mm_to_nm(_f(1)))
    py = units.nm_to_mil(units.mm_to_nm(_f(2)))
    rot = int(round(_f(3))) % 360
    mnode = sym.find("mirror")
    mirror = (mnode.children[1].value if mnode and len(mnode.children or []) >= 2 else None) or "none"
    return model.Component(
        designator=_symbol_reference(sym) or "?",
        library_ref=lib_id,
        x_mil=px,
        y_mil=py,
        rotation=rot,
        mirror=mirror,
    )


def _resolve_pin_inst(
    doc: SNode, ctx: dict, ref: str, pin_number: str,
) -> tuple[model.Component, model.Pin, tuple[int, int]]:
    """``(instance, pin, world_nm)`` of ``ref.pin_number`` in the current document.

    A multi-unit part is several placed instances sharing ``ref``; the pin is
    looked up on the instance whose UNIT owns it (eeschema exposes only that
    unit's pins there). A pin living on an unplaced unit fails loudly instead
    of silently snapping to another unit's body — that divergence merged all
    four 74xx gates onto one instance.
    """
    syms = _symbols_by_ref(doc, ref)
    if not syms:
        fail("VERIFY_FAILED", f"pin reference {ref!r} matches no placed component")
    symdef = None
    for sym in syms:
        lib_id = _inst_lib_id(sym)
        try:
            symdef = _ctx_symdef(doc, ctx, lib_id)
        except AkcliError:
            fail("SYMBOL_NOT_FOUND", f"pin reference {ref!r}: lib_id {lib_id!r} not in cache")
        unit = _symbol_unit(sym)
        comp = _instance_component(sym, lib_id)
        for pin in kicad_lib.unit_pins(symdef, unit):
            if pin.number == pin_number:
                return comp, pin, geometry.pin_world(symdef, comp, pin)
    if symdef is not None:
        owners = sorted({p.owner_part_id for p in symdef.pins if p.number == pin_number})
        if owners:
            fail(
                "VERIFY_FAILED",
                f"pin {ref}.{pin_number} is on unit {owners[0]} which is not placed; "
                f'place it first (place_component with "unit": {owners[0]})',
            )
    fail("VERIFY_FAILED", f"component {ref!r} has no pin {pin_number!r}")


def _resolve_pin_world(doc: SNode, ctx: dict, ref: str, pin_number: str) -> tuple[int, int]:
    """World coordinate (nm) of ``ref.pin_number`` (see :func:`_resolve_pin_inst`)."""
    return _resolve_pin_inst(doc, ctx, ref, pin_number)[2]


def _pin_at_point(
    doc: SNode, ctx: dict, p: tuple[int, int],
) -> tuple[model.Component, model.Pin] | None:
    """The placed ``(instance, pin)`` whose electrical tip sits exactly at ``p``.

    Reverse lookup for label auto-orientation: a label anchored on a raw
    coordinate that happens to be a pin tip should still orient away from that
    pin's body. Returns the first match (coincident pins of two symbols share
    the point anyway; either orientation choice is between the same bodies).
    """
    for sym in _placed_symbols(doc):
        lib_id = _inst_lib_id(sym)
        if not lib_id:
            continue
        try:
            symdef = _ctx_symdef(doc, ctx, lib_id)
        except AkcliError:
            continue
        unit = _symbol_unit(sym)
        comp = _instance_component(sym, lib_id)
        for pin in kicad_lib.unit_pins(symdef, unit):
            if geometry.pin_world(symdef, comp, pin) == p:
                return comp, pin
    return None


def _resolve_anchor_origin(
    doc: SNode, ctx: dict, anchor: str, opname: str,
) -> tuple[int, int]:
    """World coordinate (nm) of a relative-placement anchor.

    ``"REF.PIN"`` resolves to that pin's electrical tip (the same machinery
    wires use); a bare ``"REF"`` resolves to the component's ``(at)`` origin.
    The anchor may have been placed earlier in the SAME op-list — resolution
    happens at execution time against the live document, never as an ops.py
    pre-pass (a second geometry engine would inevitably diverge).
    """
    if "." in anchor:
        ref, pin_number = anchor.rsplit(".", 1)
        return _resolve_pin_world(doc, ctx, ref, pin_number)
    sym = _symbol_by_ref(doc, anchor)
    if sym is None:
        fail("VERIFY_FAILED",
             f"{opname}: anchor {anchor!r}: no placed component {anchor!r}")
    at = sym.find("at")
    return _at_nm(at) if at is not None else (0, 0)


def _anchored_pos_nm(doc: SNode, ctx: dict, op: dict, opname: str) -> tuple[int, int]:
    """Grid-snapped world position for an anchor (+world-frame offset_mil) op."""
    base = _resolve_anchor_origin(doc, ctx, str(op["anchor"]), opname)
    off = op.get("offset_mil") or [0, 0]
    return geometry.grid_snap_nm(
        (base[0] + geometry.mil_to_nm(float(off[0])),
         base[1] + geometry.mil_to_nm(float(off[1]))),
        _GRID_NM,
    )


def _resolve_endpoint(doc: SNode, ctx: dict, ep: object) -> tuple[int, int]:
    """Resolve a wire/port endpoint to integer-nm coordinates.

    ``"REF.PIN"`` snaps to the pin's world coordinate (exact, never grid-snapped);
    a raw ``[x, y]`` mil point is converted to nm and snapped to the 50-mil grid.
    """
    if isinstance(ep, str):
        ref, pin_number = ep.rsplit(".", 1)
        return _resolve_pin_world(doc, ctx, ref, pin_number)
    if isinstance(ep, (list, tuple)) and len(ep) == 2:
        nm = (geometry.mil_to_nm(float(ep[0])), geometry.mil_to_nm(float(ep[1])))
        return geometry.grid_snap_nm(nm, _GRID_NM)
    fail("NON_ORTHOGONAL_WIRE", f"malformed endpoint {ep!r}")


def _point_nm(at: object) -> tuple[int, int]:
    """Convert a raw ``[x, y]`` mil point to grid-snapped nm."""
    if isinstance(at, (list, tuple)) and len(at) == 2:
        nm = (geometry.mil_to_nm(float(at[0])), geometry.mil_to_nm(float(at[1])))
        return geometry.grid_snap_nm(nm, _GRID_NM)
    fail("OFF_GRID", f"malformed point {at!r}")


# mid() anchors: the two pins must be axis-aligned within half a grid step.
def _snap_within_nm(v: int, a: int, b: int) -> int:
    """Snap ``v`` to the 50-mil grid, clamped into ``[min(a,b), max(a,b)]``.

    Clamping keeps the anchor ON the wire between off-grid pins — a snapped
    midpoint outside the span would leave the label/flag floating.
    """
    s = int(round(v / _GRID_NM)) * _GRID_NM
    lo, hi = min(a, b), max(a, b)
    return min(max(s, lo), hi)


def _resolve_mid_anchor(
    doc: SNode, ctx: dict, at: str, opname: str,
) -> tuple[tuple[int, int], str]:
    """Resolve ``"mid(A.p,B.p)"`` to ``(point_nm, wire_axis)``.

    Both pins must be EXACTLY axis-aligned (equal integer-nm cross-axis
    coordinate); the midpoint is snapped to the 50-mil grid ALONG the wire
    axis (``"x"`` horizontal / ``"y"`` vertical) while the cross-axis
    coordinate keeps the pins' shared value — so the anchor always lands on
    the straight wire drawn between the two pins. A tolerance here would be
    a lie: a snapped anchor between misaligned pins leaves the (slightly
    diagonal) wire, and netbuild's exact-integer on-segment test would let
    the label/flag silently attach to nothing.
    """
    parsed = parse_mid_anchor(at)
    if parsed is None:
        fail("OP_UNSUPPORTED",
             f'{opname}: malformed mid() anchor {at!r}; '
             f'expected "mid(REF.PIN,REF.PIN)"')
    a, b = parsed
    pa = _resolve_pin_world(doc, ctx, *a.rsplit(".", 1))
    pb = _resolve_pin_world(doc, ctx, *b.rsplit(".", 1))
    dx, dy = abs(pa[0] - pb[0]), abs(pa[1] - pb[1])
    if min(dx, dy) != 0:
        fail("NON_ORTHOGONAL_WIRE",
             f"{opname}: mid() pins are not axis-aligned: "
             f"{a} at ({units.nm_to_mil(pa[0]):g},{units.nm_to_mil(pa[1]):g}) mil, "
             f"{b} at ({units.nm_to_mil(pb[0]):g},{units.nm_to_mil(pb[1]):g}) mil")
    if dx >= dy:   # wire runs along X
        return (_snap_within_nm((pa[0] + pb[0]) // 2, pa[0], pb[0]),
                (pa[1] + pb[1]) // 2), "x"
    return ((pa[0] + pb[0]) // 2,
            _snap_within_nm((pa[1] + pb[1]) // 2, pa[1], pb[1])), "y"


# --------------------------------------------------------------------------- #
# property helpers
# --------------------------------------------------------------------------- #
def _set_property(sym: SNode, key: str, value: str) -> None:
    """Set/replace ``(property "<key>" "<value>" ...)`` on a placed symbol."""
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 2 and kids[1].value == key:
            val_atom = _atom(_q(value))
            if len(kids) >= 3:
                kids[2] = val_atom
            else:
                prop.children.insert(2, val_atom)   # type: ignore[union-attr]
                prop.ws.insert(2, " ")              # type: ignore[union-attr]
            return
    at = sym.find("at")
    pos = (_at_nm(at) if at is not None else (0, 0))
    # KiCad creates every field except Reference/Value hidden by default —
    # a NEW property node sits at the symbol anchor, so a visible custom
    # field (LCSC, MPN, ...) renders as raw text piled on the body.
    hide = key not in _VISIBLE_PROPERTIES
    prop = _list(
        _atom("property"),
        _atom(_q(key)),
        _atom(_q(value)),
        _at(pos, 0),
        _effects(hide=hide),
    )
    indent = _doc_child_indent(sym)
    k = len(sym.children or [])
    sym.children.append(prop)              # type: ignore[union-attr]
    sym.ws.insert(k, indent)               # type: ignore[union-attr]


def _at_nm(at: SNode) -> tuple[int, int]:
    def _f(idx: int) -> float:
        if at.children and idx < len(at.children):
            v = at.children[idx].value
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    return (units.mm_to_nm(_f(1)), units.mm_to_nm(_f(2)))


# Properties eeschema creates hidden (raw text over the body otherwise).
_VISIBLE_PROPERTIES = frozenset({"Reference", "Value"})

# Gap between a symbol's pin bounding box and its Reference/Value text (nm).
_PROP_MARGIN_NM = units.mm_to_nm(1.27)


# Two text anchors collide when closer than roughly one label's extent.
_TEXT_CLEAR_X_NM = units.mm_to_nm(2.54)
_TEXT_CLEAR_Y_NM = units.mm_to_nm(1.27)


def _free_anchor(ctx: dict, pos: tuple[int, int],
                 step: tuple[int, int]) -> tuple[int, int]:
    """First anchor at/beyond ``pos`` (stepping by ``step``) clear of others.

    Registered VISIBLE text anchors within ~one label extent count as
    collisions; the bump direction follows the side the text was placed on, so
    the result stays deterministic and replay-stable.
    """
    anchors = ctx.setdefault("text_anchors", [])
    x, y = pos
    for _ in range(8):
        hit = any(
            abs(x - ax) < _TEXT_CLEAR_X_NM and abs(y - ay) < _TEXT_CLEAR_Y_NM
            for ax, ay in anchors
        )
        if not hit:
            break
        x += step[0]
        y += step[1]
    anchors.append((x, y))
    return (x, y)


def _body_box_world(
    symdef, unit: int, comp, origin: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """World (nm, +Y down) box of the symbol's drawn body, or ``None``."""
    ext = kicad_lib.body_extent_mil(symdef, unit)
    if ext is None:
        return None
    return geometry.world_box_from_extent(ext, comp.rotation, comp.mirror, origin)


def _autoplace_ref_value(sym: SNode, symdef, unit: int, ctxpos: tuple[int, int],
                         ctx: dict | None = None) -> None:
    """Position Reference/Value clear of the body (KiCad-autoplace style).

    The writer used to leave every property at the component origin (and the
    synthesized Reference at absolute 0,0), stacking raw text over the symbol
    body. Heuristic: from the placed unit's pin bounding box UNION its drawn
    body extent, put the text to the RIGHT of a tall part, or ABOVE/BELOW a
    wide one; power symbols hide the reference (any ``(power)`` symbol, not
    just ``#``-refs — a PWR_FLAG placed as ``FLG1`` must not print its ref)
    and show the value past the side the body extends to (+5V arrow: above;
    GND: below), exactly like eeschema. Property text angle compensates the
    instance rotation (KiCad renders properties at symbol+property angle), so
    a rotated resistor's "R3"/"470" still read horizontally. Neighboring
    parts' labels are avoided via the per-apply anchor registry
    (deterministic bump, replay-stable).
    """
    ctx = ctx if ctx is not None else {}
    lib_id = _inst_lib_id(sym)
    comp = _instance_component(sym, lib_id)
    pts = [geometry.pin_world(symdef, comp, p) for p in kicad_lib.unit_pins(symdef, unit)]
    ref = _symbol_reference(sym) or ""
    is_power = ref.startswith("#") or kicad_lib.is_power_symbol(symdef)
    # Counter-rotate so the text renders horizontal on a rotated instance
    # (mod 180: KiCad never draws text upside-down, and an angle of 180
    # WOULD render inverted rather than normalize).
    text_angle = (360 - comp.rotation) % 180

    if pts:
        min_x = min(p[0] for p in pts); max_x = max(p[0] for p in pts)
        min_y = min(p[1] for p in pts); max_y = max(p[1] for p in pts)
    else:
        min_x = max_x = ctxpos[0]
        min_y = max_y = ctxpos[1]
    body = _body_box_world(symdef, unit, comp, ctxpos)
    if body is not None:
        min_x = min(min_x, body[0]); min_y = min(min_y, body[1])
        max_x = max(max_x, body[2]); max_y = max(max_y, body[3])

    if is_power:
        # eeschema: hidden reference; value past the body's far side (a +5V
        # arrow extends up -> value above it; GND extends down -> below).
        _place_prop(sym, "Reference", (ctxpos[0], max_y + _PROP_MARGIN_NM),
                    hide=True, angle=text_angle)
        if (min_y + max_y) // 2 < ctxpos[1]:
            vpos = _free_anchor(ctx, (ctxpos[0], min_y - _PROP_MARGIN_NM),
                                (0, -_PROP_MARGIN_NM))
        else:
            vpos = _free_anchor(ctx, (ctxpos[0], max_y + _PROP_MARGIN_NM),
                                (0, _PROP_MARGIN_NM))
        _place_prop(sym, "Value", vpos, angle=text_angle)
        return
    tall = (max_y - min_y) >= (max_x - min_x)
    if tall:   # vertical body (R/C/L...): text to the right, left-justified
        x = max_x + _PROP_MARGIN_NM
        rpos = _free_anchor(ctx, (x, ctxpos[1] - _PROP_MARGIN_NM),
                            (_TEXT_CLEAR_X_NM, 0))
        vpos = _free_anchor(ctx, (rpos[0], ctxpos[1] + _PROP_MARGIN_NM),
                            (_TEXT_CLEAR_X_NM, 0))
        _place_prop(sym, "Reference", rpos, justify="left", angle=text_angle)
        _place_prop(sym, "Value", vpos, justify="left", angle=text_angle)
    else:      # wide body (ICs, connectors): text above/below the body
        rpos = _free_anchor(ctx, (ctxpos[0], min_y - _PROP_MARGIN_NM),
                            (0, -_PROP_MARGIN_NM))
        vpos = _free_anchor(ctx, (ctxpos[0], max_y + _PROP_MARGIN_NM),
                            (0, _PROP_MARGIN_NM))
        _place_prop(sym, "Reference", rpos, angle=text_angle)
        _place_prop(sym, "Value", vpos, angle=text_angle)


def _place_prop(
    sym: SNode, key: str, pos_nm: tuple[int, int],
    *, justify: str | None = None, hide: bool = False, angle: float = 0.0,
) -> None:
    """Set an existing property's ``(at ...)`` / ``(effects ...)`` in place."""
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 2 and kids[1].value == key:
            at = prop.find("at")
            new_at = _at(pos_nm, angle)
            if at is not None:
                kids[kids.index(at)] = new_at
            else:
                prop.children.append(new_at)          # type: ignore[union-attr]
                prop.ws.insert(len(prop.ws) - 1, " ")  # type: ignore[union-attr]
            eff = prop.find("effects")
            new_eff = _effects(hide=hide, justify=justify)
            if eff is not None:
                kids[kids.index(eff)] = new_eff
            else:
                prop.children.append(new_eff)          # type: ignore[union-attr]
                prop.ws.insert(len(prop.ws) - 1, " ")  # type: ignore[union-attr]
            return


# --------------------------------------------------------------------------- #
# symbol-instance construction
# --------------------------------------------------------------------------- #
def _make_symbol(
    lib_id: str, pos_nm: tuple[int, int], rotation: int, mirror: str,
    uuid_str: str, pin_numbers: list[str], pin_uuids: list[str], unit: int = 1,
) -> SNode:
    """Build a placed ``(symbol ...)`` node with per-pin ``(pin "N" (uuid))``."""
    children = [
        _list(_atom("lib_id"), _atom(_q(escape_lib_id(lib_id)))),
        _at(pos_nm, rotation),
        _list(_atom("unit"), _atom(str(int(unit)))),
    ]
    if mirror in ("x", "y"):
        children.insert(2, _list(_atom("mirror"), _atom(mirror)))
    children.append(_uuid_node(uuid_str))
    for num, puid in zip(pin_numbers, pin_uuids):
        children.append(_list(_atom("pin"), _atom(_q(num)), _uuid_node(puid)))
    return _list(_atom("symbol"), *children)


def _place_symbol(
    doc: SNode, ctx: dict, src_libs: list, lib_id: str, designator: str,
    pos_nm: tuple[int, int], rotation: int, mirror: str, op_index: int, path: str,
    unit: int = 1,
) -> str:
    """Cache + place ONE UNIT of a symbol; return its (deterministic) uuid."""
    body = lib_cache.ensure_cached(doc, lib_id, src_libs)
    symdef = ctx["symdefs"].get(lib_id)
    if symdef is None:
        # Resolve from just this one cached body — never the whole cache (perf).
        symdef = _symdef_from_body(body, lib_id)
        ctx["symdefs"][lib_id] = symdef
    if unit < 1 or unit > max(1, symdef.part_count):
        fail("VERIFY_FAILED",
             f"{lib_id!r} has {symdef.part_count} unit(s); cannot place unit {unit}")
    # The instance carries only ITS unit's pins (eeschema draws and connects
    # only those); emitting every unit's pins mapped all gates onto one body
    # and let phantom pin points mask real dangles in the verifier.
    pin_numbers = [p.number for p in kicad_lib.unit_pins(symdef, unit)]

    root = _root_uuid(doc)
    # Unit 1 keeps the historical seed so existing files replay byte-identically.
    ref_seed = designator if unit == 1 else f"{designator}#u{unit}"
    sym_uuid = instances.deterministic_uuid(root, ref_seed, op_index)
    # A pin NUMBER may legitimately repeat within one symbol (multi-unit parts
    # with shared pads, e.g. dual DirectFETs: unit A pins 1,2,3 / unit B pins
    # 1,4,5). Seed later occurrences with a #k suffix so their uuids stay
    # unique — otherwise the two "(pin "1" ...)" nodes collide and the
    # connectivity gate refuses the write (DUPLICATE_UUID). The first
    # occurrence keeps the historical seed, so existing files replay unchanged.
    pin_uuids = []
    _seen: dict[str, int] = {}
    for n in pin_numbers:
        k = _seen.get(n, 0)
        _seen[n] = k + 1
        base = f"{designator}.pin{n}" if unit == 1 else f"{designator}#u{unit}.pin{n}"
        seed = base if k == 0 else f"{base}#{k + 1}"
        pin_uuids.append(instances.deterministic_uuid(root, seed, op_index))

    # Idempotent replay: a same-uuid instance already present is replaced wholesale.
    sym = _make_symbol(lib_id, pos_nm, rotation, mirror, sym_uuid, pin_numbers, pin_uuids, unit)
    _append_top_idempotent(doc, sym, sym_uuid)
    instances.write_instance(doc, sym, designator, path)
    # New pins invalidate the route_net corner-avoidance memo (ops run in
    # order; a later route must see THIS part's pins).
    ctx.pop("pin_tips", None)
    return sym_uuid


# --------------------------------------------------------------------------- #
# per-op handlers (each returns the list of created uuids)
# --------------------------------------------------------------------------- #
def _op_place_component(doc, op, idx, src_libs, path, ctx) -> list[str]:
    sources = list(src_libs)
    if op.get("symbol_source"):
        sources = [op["symbol_source"], *sources]
    rotation = int(op.get("rotation", 0))
    mirror = op.get("mirror", "none")
    unit = int(op.get("unit", 1))
    if isinstance(op.get("anchor"), str):
        pos = _anchored_pos_nm(doc, ctx, op, "place_component")
    else:
        pos = geometry.grid_snap_nm(
            (geometry.mil_to_nm(float(op["x_mil"])), geometry.mil_to_nm(float(op["y_mil"]))),
            _GRID_NM,
        )
    uid = _place_symbol(
        doc, ctx, sources, op["lib_id"], op["designator"],
        pos, rotation, mirror, idx, path, unit,
    )
    sym = _symbol_by_uuid(doc, uid)
    if op.get("value") is not None and sym is not None:
        _set_property(sym, "Value", str(op["value"]))
    if op.get("footprint") is not None and sym is not None:
        _set_property(sym, "Footprint", str(op["footprint"]))
    if isinstance(op.get("group"), str) and sym is not None:
        # Functional-group membership persists in the sheet as a hidden
        # property, so `akcli groups` / `arrange --groups` can recover the
        # module map without the original op-list.
        _set_property(sym, "Group", op["group"])
    if sym is not None:
        _autoplace_ref_value(sym, _ctx_symdef(doc, ctx, op["lib_id"]), unit, pos, ctx)
    return [uid]


def _op_set_component_transform(doc, op, idx, src_libs, path, ctx) -> list[str]:
    sym = _symbol_by_ref(doc, op["designator"])
    if sym is None:
        fail("VERIFY_FAILED", f"set_component_transform: no component {op['designator']!r}")
    at = sym.find("at")
    if "rotation" in op and at is not None and len(at.children or []) >= 4:
        at.children[3] = _atom(str(int(op["rotation"])))
    if "mirror" in op:
        mval = op["mirror"]
        existing = sym.find("mirror")
        if mval in ("x", "y"):
            node = _list(_atom("mirror"), _atom(mval))
            if existing is not None:
                sym.children[sym.children.index(existing)] = node
            else:
                # insert just after (at ...)
                ipos = sym.children.index(at) + 1 if at is not None else len(sym.children)
                sym.children.insert(ipos, node)      # type: ignore[union-attr]
                sym.ws.insert(ipos, " ")             # type: ignore[union-attr]
        elif existing is not None:                    # mirror "none" -> drop it
            j = sym.children.index(existing)
            del sym.children[j]
            del sym.ws[j]
    return []


def _op_set_component_parameters(doc, op, idx, src_libs, path, ctx) -> list[str]:
    sym = _symbol_by_ref(doc, op["designator"])
    if sym is None:
        fail("VERIFY_FAILED", f"set_component_parameters: no component {op['designator']!r}")
    if op.get("reference"):
        instances.write_instance(doc, sym, str(op["reference"]), path)
    if op.get("value") is not None:
        _set_property(sym, "Value", str(op["value"]))
    if op.get("footprint") is not None:
        _set_property(sym, "Footprint", str(op["footprint"]))
    for k, v in (op.get("parameters") or {}).items():
        _set_property(sym, str(k), str(v))
    return []


def _op_add_wire(doc, op, idx, src_libs, path, ctx, tag="wire") -> list[str]:
    verts = op["vertices"]
    pts = [_resolve_endpoint(doc, ctx, v) for v in verts]
    root = _root_uuid(doc)
    return _emit_wire_segments(doc, root, idx, pts, tag)


def _emit_wire_segments(
    doc: SNode, root: str | None, idx: int,
    pts: list[tuple[int, int]], tag: str,
) -> list[str]:
    """One ``(wire ...)``/``(bus ...)`` node per consecutive point pair.

    Shared by ``add_wire``/``add_bus`` and ``route_net`` — the uuid seed is
    the segment's coordinates, so identical geometry replays byte-identically
    regardless of which op produced it.
    """
    created: list[str] = []
    for a, b in zip(pts, pts[1:]):
        if a == b:
            continue
        uid = instances.deterministic_uuid(root, f"{tag}:{a[0]}:{a[1]}:{b[0]}:{b[1]}", idx)
        node = _list(_atom(tag), _pts([a, b]), _stroke(), _uuid_node(uid))
        _append_top_idempotent(doc, node, uid)
        created.append(uid)
    return created


def _op_add_bus(doc, op, idx, src_libs, path, ctx) -> list[str]:
    return _op_add_wire(doc, op, idx, src_libs, path, ctx, tag="bus")


def _pin_tip_set(doc: SNode, ctx: dict) -> set[tuple[int, int]]:
    """Every placed pin's world tip, memoized per apply (invalidated on place).

    route_net's corner rule consults this so an auto-picked L-corner never
    lands ON a pin — a coincident corner would silently merge that pin's net
    into the route (the trap docs teach by hand; the router avoids it by
    construction).
    """
    tips = ctx.get("pin_tips")
    if tips is None:
        tips = set()
        for sym in _placed_symbols(doc):
            lib_id = _inst_lib_id(sym)
            if not lib_id:
                continue
            try:
                symdef = _ctx_symdef(doc, ctx, lib_id)
            except AkcliError:
                continue
            unit = _symbol_unit(sym)
            comp = _instance_component(sym, lib_id)
            for pin in kicad_lib.unit_pins(symdef, unit):
                tips.add(geometry.pin_world(symdef, comp, pin))
        ctx["pin_tips"] = tips
    return tips


def _route_points(
    a: tuple[int, int], b: tuple[int, int], style: str,
    tips: set[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Deterministic orthogonal route from ``a`` to ``b``.

    Coaxial endpoints -> one straight segment. Otherwise an L with corner
    ``hv`` = (b.x, a.y) (run horizontal first) or ``vh`` = (a.x, b.y);
    ``auto`` prefers ``hv``, swaps to ``vh`` when that corner sits on a pin
    tip, and falls back to a 3-segment ``z`` (longer axis split at its
    grid-snapped midpoint) when both corners are pins. Collision-aware
    routing is explicitly out of scope — geometry stays advisory; the
    connectivity gate and the NET_WIRE_CORNER_ON_PIN lint are the safety net.
    """
    if a[0] == b[0] or a[1] == b[1]:
        return [a, b]
    c_hv = (b[0], a[1])
    c_vh = (a[0], b[1])
    if style == "hv":
        return [a, c_hv, b]
    if style == "vh":
        return [a, c_vh, b]
    if style == "auto":
        if c_hv not in tips:
            return [a, c_hv, b]
        if c_vh not in tips:
            return [a, c_vh, b]
    # style "z", or auto with both L-corners on pin tips
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    if dx >= dy:
        mx = _snap_within_nm((a[0] + b[0]) // 2, a[0], b[0])
        return [a, (mx, a[1]), (mx, b[1]), b]
    my = _snap_within_nm((a[1] + b[1]) // 2, a[1], b[1])
    return [a, (a[0], my), (b[0], my), b]


def _op_route_net(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Orthogonal auto-route between two endpoints (+ optional mid-wire label).

    The non-coaxial companion of the ``wire_net``/``connect_and_label``
    pattern: resolves both endpoints (pin tips exact, points grid-snapped),
    synthesizes the L/Z vertices via :func:`_route_points`, and emits the
    segments through the shared wire path — same seeds, byte-identical
    re-apply. ``label`` names the net once at the longest segment's midpoint,
    snapped along the wire axis and clamped into the span.
    """
    a = _resolve_endpoint(doc, ctx, op["from"])
    b = _resolve_endpoint(doc, ctx, op["to"])
    if a == b:
        fail("VERIFY_FAILED", "route_net: from and to resolve to the same point")
    pts = _route_points(a, b, op.get("style", "auto"), _pin_tip_set(doc, ctx))
    root = _root_uuid(doc)
    created = _emit_wire_segments(doc, root, idx, pts, "wire")
    if op.get("label"):
        (ax, ay), (bx, by) = max(
            zip(pts, pts[1:]),
            key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
        if ay == by:   # horizontal segment: snap along X, read along the wire
            p = (_snap_within_nm((ax + bx) // 2, ax, bx), ay)
            orientation = 0
        else:
            p = (ax, _snap_within_nm((ay + by) // 2, ay, by))
            orientation = 90
        created.append(_emit_net_label(
            doc, root, idx, str(op["label"]), p, orientation,
            op.get("scope", "local")))
    return created


def _op_add_junction(doc, op, idx, src_libs, path, ctx) -> list[str]:
    p = _point_nm(op["at"])
    root = _root_uuid(doc)
    uid = instances.deterministic_uuid(root, f"junction:{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("junction"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _list(_atom("diameter"), _atom("0")),
        _list(_atom("color"), _atom("0"), _atom("0"), _atom("0"), _atom("0")),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _op_add_no_connect(doc, op, idx, src_libs, path, ctx) -> list[str]:
    ep = op["pin"]
    if isinstance(ep, str) and "." in ep:
        ref, pin_number = ep.rsplit(".", 1)
        p = _resolve_pin_world(doc, ctx, ref, pin_number)
    else:
        p = _point_nm(ep)
    root = _root_uuid(doc)
    uid = instances.deterministic_uuid(root, f"no_connect:{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("no_connect"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _op_add_net_label(doc, op, idx, src_libs, path, ctx) -> list[str]:
    at = op["at"]
    comp = pin = axis = None
    if isinstance(at, str) and at.startswith("mid("):
        # "mid(A.p,B.p)" anchor: grid-snapped midpoint of two axis-aligned pins.
        p, axis = _resolve_mid_anchor(doc, ctx, at, "add_net_label")
    elif isinstance(at, str) and "." in at:
        # "REF.PIN" anchor: exact pin world coordinate, never grid-snapped.
        ref, pin_number = at.rsplit(".", 1)
        comp, pin, p = _resolve_pin_inst(doc, ctx, ref, pin_number)
    else:
        p = _point_nm(at)
        if "orientation" not in op:
            hit = _pin_at_point(doc, ctx, p)
            if hit is not None:
                comp, pin = hit
    scope = op.get("scope", "local")
    if "orientation" in op:
        orientation = int(op["orientation"])
    elif pin is not None:
        # Label lands on a pin tip: orient the text away from the symbol body
        # so it never runs over the part it names.
        orientation = geometry.label_angle_away(pin.orientation, comp.rotation, comp.mirror)
    elif axis is not None:
        # Label sits mid-wire: read along the wire axis (never across it).
        orientation = 0 if axis == "x" else 90
    else:
        orientation = 0
    root = _root_uuid(doc)
    return [_emit_net_label(doc, root, idx, str(op["name"]), p, orientation, scope)]


def _emit_net_label(
    doc: SNode, root: str | None, idx: int, name: str,
    p: tuple[int, int], orientation: int, scope: str,
) -> str:
    """Emit one net-label node (shared by ``add_net_label`` and ``route_net``)."""
    tag = {"local": "label", "global": "global_label",
           "hierarchical": "hierarchical_label"}[scope]
    uid = instances.deterministic_uuid(root, f"{tag}:{name}:{p[0]}:{p[1]}", idx)
    children = [_atom(tag), _atom(_q(name))]
    if tag != "label":
        children.append(_list(_atom("shape"), _atom("input")))
    children += [
        _at(p, orientation),
        _effects(justify=_label_justify(tag, orientation)),
        _uuid_node(uid),
    ]
    _append_top_idempotent(doc, _list(*children), uid)
    return uid


def _existing_pwr_ref_for_op(doc: SNode, root: str | None, op_index: int) -> str | None:
    """Reference of a power symbol a prior replay of ``op_index`` placed, else ``None``.

    A power port's ``#PWR0<n>`` reference is *auto-allocated* (it isn't carried in
    the op), so on replay :func:`instances.alloc_pwr_ref` would hand out the *next*
    free number — a different designator and therefore a different deterministic
    uuid, breaking idempotency.  Recover the original instead: the symbol this op
    placed has a uuid equal to ``deterministic_uuid(root, <its own ref>, op_index)``.
    Reusing that ref keeps re-applies byte-identical.
    """
    for sym in _placed_symbols(doc):
        ref = _symbol_reference(sym)
        if not ref or not ref.startswith("#"):
            continue
        un = sym.find("uuid")
        if un is None or len(un.children or []) < 2:
            continue
        if un.children[1].value == instances.deterministic_uuid(root, ref, op_index):
            return ref
    return None


def _op_place_power_port(doc, op, idx, src_libs, path, ctx) -> list[str]:
    name = op.get("op")
    if name in _SUGAR_POWER:
        default_lib, default_net = _SUGAR_POWER[name]
        lib_id = op.get("lib_id", default_lib)
        net_name = op.get("net_name", default_net)
    else:
        lib_id = op["lib_id"]
        net_name = op["net_name"]
    at = op["at"]
    if isinstance(at, str) and at.startswith("mid("):
        # "mid(A.p,B.p)" anchor: the port lands mid-wire (on-seg connects).
        pos, _axis = _resolve_mid_anchor(doc, ctx, at, name or "place_power_port")
    elif isinstance(at, str) and "." in at:
        # "REF.PIN" anchor: the port lands on the pin tip (connects with no wire).
        ref_ep, pin_number = at.rsplit(".", 1)
        pos = _resolve_pin_world(doc, ctx, ref_ep, pin_number)
    else:
        pos = _point_nm(at)
    rotation = int(op.get("rotation", 0))
    # Reuse the ref from a prior replay of this op so the auto-allocated #PWR
    # designator (and thus the deterministic uuid) stays stable -> idempotent.
    ref = _existing_pwr_ref_for_op(doc, _root_uuid(doc), idx) or instances.alloc_pwr_ref(doc)
    uid = _place_symbol(
        doc, ctx, list(src_libs), lib_id, ref,
        pos, rotation, "none", idx, path,
    )
    sym = _symbol_by_uuid(doc, uid)
    if sym is not None:
        _set_property(sym, "Value", str(net_name))
        if isinstance(op.get("group"), str):
            _set_property(sym, "Group", op["group"])
        _autoplace_ref_value(sym, _ctx_symdef(doc, ctx, lib_id), 1, pos, ctx)
    return [uid]


def _op_add_bus_entry(doc, op, idx, src_libs, path, ctx) -> list[str]:
    p = _point_nm(op["at"])
    if isinstance(op.get("size"), (list, tuple)) and len(op["size"]) == 2:
        size = (geometry.mil_to_nm(float(op["size"][0])), geometry.mil_to_nm(float(op["size"][1])))
    else:
        size = (_BUS_ENTRY_NM, _BUS_ENTRY_NM)
    root = _root_uuid(doc)
    uid = instances.deterministic_uuid(root, f"bus_entry:{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("bus_entry"),
        _list(_atom("at"), _mm(p[0]), _mm(p[1])),
        _list(_atom("size"), _mm(size[0]), _mm(size[1])),
        _stroke(),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _op_add_text(doc, op, idx, src_libs, path, ctx) -> list[str]:
    p = _point_nm(op["at"])
    angle = float(op.get("angle", 0))
    root = _root_uuid(doc)
    uid = _annotation_uuid(root, "text", op, f"{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("text"),
        _atom(_q(str(op["text"]))),
        _at(p, angle),
        _effects(),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _annotation_uuid(root: str | None, tag: str, op: dict,
                     coord_seed: str, idx: int) -> str:
    """Deterministic uuid for a graphic/annotation node.

    Default seed follows the wire convention (coordinates + op index). An
    optional ``key`` gives the node a STABLE handle independent of both its
    coordinates and its position in the op-list: re-emitting the same key
    replaces the node in place (``_append_top_idempotent``), which is what
    lets a group frame refresh itself after parts move instead of piling up.
    """
    key = op.get("key")
    if isinstance(key, str) and key:
        return instances.deterministic_uuid(root, f"{tag}:key:{key}", 0)
    return instances.deterministic_uuid(root, f"{tag}:{coord_seed}", idx)


def _op_add_rectangle(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Emit a top-level graphic ``(rectangle ...)`` (SPEC §2.2, add_rectangle).

    Pure annotation — netbuild never reads graphics, so a rectangle is
    connectivity-neutral by construction. The primary customer is the
    functional-group frame (``akcli groups --frame``), which draws a border
    box around a module's bounding box; a ``key`` keeps the frame's uuid
    stable so a refresh replaces it in place.
    """
    a = _point_nm(op["start"])
    b = _point_nm(op["end"])
    width_nm = geometry.mil_to_nm(float(op.get("stroke_width_mil", 0) or 0))
    root = _root_uuid(doc)
    uid = _annotation_uuid(root, "rectangle", op,
                           f"{a[0]}:{a[1]}:{b[0]}:{b[1]}", idx)
    node = _list(
        _atom("rectangle"),
        _list(_atom("start"), _mm(a[0]), _mm(a[1])),
        _list(_atom("end"), _mm(b[0]), _mm(b[1])),
        _stroke(width_nm),
        _fill(str(op.get("fill", "none"))),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


def _op_add_text_box(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Emit a bordered note box ``(text_box ...)`` (SPEC §2.2, add_text_box).

    Multi-line annotation with a border — module notes, design rationale,
    bring-up instructions. Grammar is fixture-verified against a real KiCad
    (tests/fixtures/kicad/text_box.kicad_sch, accepted by kicad-cli export +
    ERC): content, exclude_from_sim, at, size, stroke, fill, effects
    (left/top justified), uuid. ``at`` is the TOP-LEFT corner; connectivity-
    neutral like every graphic; an optional ``key`` gives a stable handle.
    """
    p = _point_nm(op["at"])
    size = op["size"]
    size_nm = (geometry.mil_to_nm(float(size[0])),
               geometry.mil_to_nm(float(size[1])))
    angle = float(op.get("angle", 0))
    root = _root_uuid(doc)
    uid = _annotation_uuid(root, "text_box", op, f"{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("text_box"),
        _atom(_q(str(op["text"]))),
        _list(_atom("exclude_from_sim"), _atom("no")),
        _at(p, angle),
        _list(_atom("size"), _mm(size_nm[0]), _mm(size_nm[1])),
        _stroke(),
        _fill(),
        _effects(justify="left top"),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


_TITLE_BLOCK_SIMPLE = ("title", "date", "rev", "company")


def _unescape_atom(s: str) -> str:
    """Undo ``_q``'s quoting/escaping on a freshly-written atom token.

    A PARSED atom's ``.value`` is already unquoted; a node this same apply
    wrote carries the quoted token — normalize both for comparison.
    """
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    body = s[1:-1]
    out: list[str] = []
    i = 0
    esc = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(esc.get(body[i + 1], "\\" + body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _set_tb_field(tb: SNode, tag: str, comment_n: int | None, value: str) -> int:
    """Set/replace one title_block child; 1 when the stored value changed."""
    val_idx = 1 if comment_n is None else 2
    for c in tb.children or []:
        if not c.is_list or not (c.children or []) or c.children[0].value != tag:
            continue
        kids = c.children
        if comment_n is not None and not (
                len(kids) >= 2 and str(kids[1].value) == str(comment_n)):
            continue
        if len(kids) > val_idx and _unescape_atom(str(kids[val_idx].value)) == value:
            return 0
        atom = _atom(_q(value))
        if len(kids) > val_idx:
            kids[val_idx] = atom
        else:
            c.children.append(atom)                # type: ignore[union-attr]
            c.ws.insert(len(c.ws) - 1, " ")        # type: ignore[union-attr]
        return 1
    kids = [_atom(tag)]
    if comment_n is not None:
        kids.append(_atom(str(comment_n)))
    kids.append(_atom(_q(value)))
    node = _list(*kids)
    k = len(tb.children or [])
    tb.children.append(node)                       # type: ignore[union-attr]
    tb.ws.insert(k, _doc_child_indent(tb))         # type: ignore[union-attr]
    return 1


def _op_set_title_block(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Set/replace title-block fields (SPEC §2.2, set_title_block).

    Find-or-create of the single ``(title_block ...)`` node — inserted after
    ``(paper ...)`` and before ``(lib_symbols ...)``, the position eeschema
    expects (elsewhere triggers a repair prompt). The node has no uuid, so
    idempotency is find-or-replace-by-tag; an op that changes nothing raises
    a replay-safe note. Pure metadata: netbuild never reads the title block,
    so the op is connectivity-neutral by construction.
    """
    tb = doc.find("title_block")
    if tb is None:
        tb = _list(_atom("title_block"))
        pos = None
        for i, c in enumerate(doc.children or []):
            if c.is_list and (c.children or []) and c.children[0].value == "paper":
                pos = i + 1
                break
        if pos is None:
            for i, c in enumerate(doc.children or []):
                if (c.is_list and (c.children or [])
                        and c.children[0].value == "lib_symbols"):
                    pos = i
                    break
        if pos is None:
            pos = len(doc.children or [])
        doc.children.insert(pos, tb)               # type: ignore[union-attr]
        doc.ws.insert(pos, _doc_child_indent(doc))  # type: ignore[union-attr]
    changed = 0
    for fname in _TITLE_BLOCK_SIMPLE:
        if op.get(fname) is not None:
            changed += _set_tb_field(tb, fname, None, str(op[fname]))
    for n in range(1, 10):
        v = op.get(f"comment{n}")
        if v is not None:
            changed += _set_tb_field(tb, "comment", n, str(v))
    if not changed:
        raise _Note("title_block already matches (nothing to change)")
    return []


# --------------------------------------------------------------------------- #
# hierarchical sheet construction
# --------------------------------------------------------------------------- #
# KiCad sheet-pin edge angle: right side = 0 (verified against real files);
# left/top/bottom follow. Only the (x, y) anchor matters for connectivity — the
# angle is cosmetic (which way the pin name text renders).
_SHEET_PIN_ANGLE = {"right": 0, "left": 180, "top": 90, "bottom": 270}


def _sheet_pin_pos(
    origin: tuple[int, int], size: tuple[int, int], side: str, off_nm: int,
) -> tuple[int, int]:
    """Edge-anchor (nm) of a sheet pin on ``side`` at ``off_nm`` from the corner."""
    x0, y0 = origin
    w, h = size
    if side == "left":
        return (x0, y0 + off_nm)
    if side == "right":
        return (x0 + w, y0 + off_nm)
    if side == "top":
        return (x0 + off_nm, y0)
    return (x0 + off_nm, y0 + h)             # bottom


def _op_add_sheet(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Emit a hierarchical ``(sheet ...)`` node (SPEC §2.2, add_sheet).

    Writes Sheetname/Sheetfile properties, stroke/fill defaults, a deterministic
    uuid, one sheet pin per ``pins`` entry at its computed edge coordinate, and
    the ``(instances (project ... (path "/<root>" (page N))))`` block KiCad needs
    to page the sub-sheet. The referenced child ``.kicad_sch`` is NOT created
    here — author it separately (e.g. ``akcli new``). Wires attach to a sheet pin
    by its coordinate: ``at`` + ``offset_mil`` along the pin's side, grid-snapped
    (the connectivity gate already treats sheet pins as label anchors, so a wire
    ending there does not dangle).
    """
    origin = geometry.grid_snap_nm(
        (geometry.mil_to_nm(float(op["at"][0])), geometry.mil_to_nm(float(op["at"][1]))),
        _GRID_NM,
    )
    size = (geometry.mil_to_nm(float(op["size"][0])),
            geometry.mil_to_nm(float(op["size"][1])))
    root = instances.root_uuid(doc)
    name = str(op["name"])
    sheet_uuid = instances.deterministic_uuid(root, f"sheet:{name}", idx)

    x0, y0 = origin
    w, h = size
    children = [
        _list(_atom("at"), _mm(x0), _mm(y0)),
        _list(_atom("size"), _mm(w), _mm(h)),
        _stroke(),
        _list(_atom("fill"), _list(_atom("color"), _atom("0"), _atom("0"),
                                   _atom("0"), _atom("0.0000"))),
        _uuid_node(sheet_uuid),
        _list(
            _atom("property"), _atom(_q("Sheetname")), _atom(_q(name)),
            _at((x0, y0), 0), _effects(justify="left bottom"),
        ),
        _list(
            _atom("property"), _atom(_q("Sheetfile")), _atom(_q(str(op["file"]))),
            _at((x0, y0 + h), 0), _effects(justify="left top"),
        ),
    ]
    for pin in op.get("pins") or []:
        side = pin["side"]
        off_nm = geometry.mil_to_nm(float(pin["offset_mil"]))
        pos = geometry.grid_snap_nm(
            _sheet_pin_pos(origin, size, side, off_nm), _GRID_NM)
        pname = str(pin["name"])
        puid = instances.deterministic_uuid(root, f"sheet:{name}.pin.{pname}", idx)
        children.append(_list(
            _atom("pin"), _atom(_q(pname)), _atom(str(pin["type"])),
            _at(pos, _SHEET_PIN_ANGLE.get(side, 0)),
            _effects(),
            _uuid_node(puid),
        ))
    # (instances): a sub-sheet is paged under the root path; page number is
    # per-op-index so replaying the same op-list stays byte-identical.
    proj = instances.project_name(doc)
    page = _list(_atom("page"), _atom(_q(str(idx + 2))))
    inst = _list(
        _atom("instances"),
        _list(_atom("project"), _atom(_q(proj)),
              _list(_atom("path"), _atom(_q("/" + root)), page)),
    )
    children.append(inst)
    _append_top_idempotent(doc, _list(_atom("sheet"), *children), sheet_uuid)
    return [sheet_uuid]


def _delete_top_nodes(doc: SNode, keep) -> int:
    """Delete top-level list nodes for which ``keep(node)`` is False; return count."""
    removed = 0
    kids = doc.children or []
    for i in range(len(kids) - 1, -1, -1):
        c = kids[i]
        if c.is_list and not keep(c):
            del doc.children[i]
            del doc.ws[i]
            removed += 1
    return removed


def _pt_on_seg_nm(p: tuple[int, int], a: tuple[int, int],
                  b: tuple[int, int]) -> bool:
    """Exact integer-nm point-on-segment (endpoints inclusive)."""
    if not (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])):
        return False
    return ((b[0] - a[0]) * (p[1] - a[1])
            == (b[1] - a[1]) * (p[0] - a[0]))


def _xy_points_nm(node: SNode) -> list[tuple[int, int]]:
    """The ``(xy ...)`` endpoints of a wire/bus ``(pts ...)`` block, in nm."""
    pts = node.find("pts")
    out: list[tuple[int, int]] = []
    for xy in (pts.find_all("xy") if pts is not None else []):
        kids = xy.children or []
        if len(kids) >= 3:
            try:
                out.append((units.mm_to_nm(float(kids[1].value)),
                            units.mm_to_nm(float(kids[2].value))))
            except (TypeError, ValueError):
                continue
    return out


def _node_uuid(node: SNode) -> str | None:
    u = node.find("uuid")
    if u is not None and len(u.children or []) >= 2:
        return u.children[1].value
    return None


def _op_delete_component(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Remove every placed instance of a designator (all units).

    Attached wires are intentionally left in place: the connectivity gate then
    reports their now-dangling endpoints, so stale wiring is cleaned up
    explicitly instead of silently. With ``"cascade": true`` the cleanup is
    done here: wires with an endpoint on any deleted pin's world coordinate,
    plus labels / no_connects anchored there, are deleted too; a junction
    anchored there is deleted only when fewer than two SURVIVING wires still
    pass through its point (a pure-X crossing of two untouched wires keeps
    its junction, else their shared net would silently split). The cascaded
    uuids are reported in the result message. Deleting an absent
    designator is a no-op (idempotent replay of a delta op-list must
    converge), reported in the result message.
    """
    ref = op["designator"]
    syms = _symbols_by_ref(doc, ref)
    cascade = bool(op.get("cascade", False))
    pin_pts: set[tuple[int, int]] = set()
    if cascade:
        for sym in syms:
            lib_id = _inst_lib_id(sym)
            symdef = _ctx_symdef(doc, ctx, lib_id)   # uncached lib_id fails loudly
            comp = _instance_component(sym, lib_id)
            for pin in kicad_lib.unit_pins(symdef, _symbol_unit(sym)):
                pin_pts.add(geometry.pin_world(symdef, comp, pin))
    targets = {id(s) for s in syms}
    removed = _delete_top_nodes(doc, lambda c: id(c) not in targets)
    if removed == 0:
        # replay-safe no-op; surfaced via the op result message (not an error)
        raise _Note(f"no placed instance of {ref!r} (already absent)")
    cascaded: list[str] = []
    if pin_pts:
        hit_ids: set[int] = set()
        junction_nodes: list[SNode] = []
        surviving_segs: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for c in doc.children or []:
            if not c.is_list or not (c.children or []):
                continue
            head = c.children[0].value
            if head in ("wire", "bus"):
                pts = _xy_points_nm(c)
                if any(p in pin_pts for p in pts):
                    hit_ids.add(id(c))
                    uid = _node_uuid(c)
                    if uid:
                        cascaded.append(uid)
                else:
                    surviving_segs.extend(zip(pts, pts[1:]))
            elif head == "junction":
                a = c.find("at")
                if a is not None and _at_nm(a) in pin_pts:
                    junction_nodes.append(c)
            elif head in ("label", "global_label", "hierarchical_label",
                          "no_connect"):
                a = c.find("at")
                if a is not None and _at_nm(a) in pin_pts:
                    hit_ids.add(id(c))
                    uid = _node_uuid(c)
                    if uid:
                        cascaded.append(uid)
        # A junction anchored on a deleted pin may STILL be doing real work:
        # two surviving wires crossing there (pure X) stay joined only through
        # it, and auto_junctions never re-adds pure-X joins — deleting it would
        # silently rewire nets the op never touched. Keep the junction unless
        # fewer than two surviving segments pass through its point.
        for c in junction_nodes:
            at = _at_nm(c.find("at"))
            touching = sum(1 for a, b in surviving_segs
                           if _pt_on_seg_nm(at, a, b))
            if touching < 2:
                hit_ids.add(id(c))
                uid = _node_uuid(c)
                if uid:
                    cascaded.append(uid)
        if hit_ids:
            _delete_top_nodes(doc, lambda c: id(c) not in hit_ids)
    if cascaded:
        raise _Note(f"cascade deleted {len(cascaded)} object(s): "
                    + ", ".join(cascaded))
    return []


def _op_delete_object(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Remove ONE top-level object by ``uuid`` or by a ``match`` selector.

    ``match`` is ``{kind, name?, at?}``: ``kind`` is the node tag (wire, label,
    global_label, ...), ``name`` matches a label/text's content, ``at`` an
    exact ``[x, y]`` mil anchor (a wire matches when EITHER endpoint lands
    there; the point is NOT grid-snapped, so a pin's exact off-grid coordinate
    matches too). Exactly-one semantics: 0 matches is a replay-safe note,
    >1 is an error listing the candidate uuids (tighten the selector).
    """
    if "match" in op:
        return _delete_object_match(doc, op["match"])
    uid = op["uuid"]

    def keep(c: SNode) -> bool:
        u = c.find("uuid")
        return not (u is not None and len(u.children or []) >= 2 and u.children[1].value == uid)

    removed = _delete_top_nodes(doc, keep)
    if removed == 0:
        raise _Note(f"no object with uuid {uid!r} (already absent)")
    return []


def _delete_object_match(doc: SNode, match: dict) -> list[str]:
    kind = match.get("kind")
    want_name = match.get("name")
    want_at = None
    if match.get("at") is not None:
        at = match["at"]
        want_at = (geometry.mil_to_nm(float(at[0])), geometry.mil_to_nm(float(at[1])))
    candidates: list[SNode] = []
    for c in doc.children or []:
        if not c.is_list or not (c.children or []) or c.children[0].value != kind:
            continue
        if want_name is not None:
            kids = c.children or []
            text = kids[1].value if len(kids) >= 2 and kids[1].is_atom else None
            if text != want_name:
                continue
        if want_at is not None:
            if kind in ("wire", "bus"):
                if want_at not in _xy_points_nm(c):
                    continue
            else:
                a = c.find("at")
                if a is None or _at_nm(a) != want_at:
                    continue
        candidates.append(c)
    if not candidates:
        raise _Note(f"no {kind} matches the selector (already absent)")
    if len(candidates) > 1:
        uuids = ", ".join(_node_uuid(c) or "<no-uuid>" for c in candidates)
        fail("VERIFY_FAILED",
             f"delete_object match is ambiguous: {len(candidates)} candidates: "
             f"{uuids} (add name/at to the selector or use uuid)")
    target = id(candidates[0])
    _delete_top_nodes(doc, lambda c: id(c) != target)
    return []


def _op_rename_net(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Rename a net everywhere it is NAMED: label texts + power-port Values.

    ``scope`` restricts the rewrite to one label kind (local / global /
    hierarchical); without it every label kind AND power-port net Values
    (symbols with an auto ``#``-prefixed reference) are rewritten. Renaming a
    net nobody names is a replay-safe note, not an error; the match count is
    reported in the result message.
    """
    frm, to = str(op["from"]), str(op["to"])
    scope = op.get("scope")
    tags = {"local": ("label",), "global": ("global_label",),
            "hierarchical": ("hierarchical_label",)}.get(
                scope, ("label", "global_label", "hierarchical_label"))
    count = 0
    for tag in tags:
        for node in doc.find_all(tag):
            kids = node.children or []
            if len(kids) >= 2 and kids[1].is_atom and kids[1].value == frm:
                kids[1] = _atom(_q(to))
                count += 1
    if scope is None:
        for sym in _placed_symbols(doc):
            ref = _symbol_reference(sym) or ""
            if not ref.startswith("#"):
                continue
            for prop in sym.find_all("property"):
                kids = prop.children or []
                if (len(kids) >= 3 and kids[1].value == "Value"
                        and kids[2].value == frm):
                    kids[2] = _atom(_q(to))
                    count += 1
    if count == 0:
        raise _Note(f"no label or power port named {frm!r} (nothing renamed)")
    raise _Note(f"renamed {count} object(s) from {frm!r} to {to!r}")


def _instance_pin_points_nm(doc, ctx, sym) -> set[tuple[int, int]]:
    """World-coordinate (nm) pin tips of ONE placed instance, or ``set()``.

    Resolved from the document's ``(lib_symbols)`` cache. An unresolved lib_id
    yields the empty set rather than raising — a plain move must not fail just
    because a carry could not be computed; the connectivity gate then flags any
    anchor the move stranded, keeping the edit loud instead of silent.
    """
    lib_id = _inst_lib_id(sym)
    try:
        symdef = _ctx_symdef(doc, ctx, lib_id)
    except AkcliError:
        return set()
    comp = _instance_component(sym, lib_id)
    return {geometry.pin_world(symdef, comp, pin)
            for pin in kicad_lib.unit_pins(symdef, _symbol_unit(sym))}


def _translate_at(node: SNode, dx: int, dy: int) -> None:
    """Shift a node's ``(at x y [angle])`` by ``(dx, dy)`` nm, keeping the angle."""
    at = node.find("at")
    if at is None:
        return
    px, py = _at_nm(at)
    kids = node.children or []
    kids[kids.index(at)] = _at((px + dx, py + dy), _fnum_at(at, 3))


def _translate_wire_pins(wire: SNode, pins: set[tuple[int, int]],
                         dx: int, dy: int) -> bool:
    """Shift only the ``(xy)`` endpoints of ``wire`` that sit on a carried pin.

    An endpoint on a moved pin follows it (stays electrically glued); an
    endpoint on a stationary pin of another component is left in place, so the
    segment stretches to keep BOTH nets connected. Returns whether any endpoint
    moved.
    """
    pts = wire.find("pts")
    if pts is None:
        return False
    moved = False
    for xy in pts.find_all("xy"):
        kids = xy.children or []
        if len(kids) < 3:
            continue
        try:
            p = (units.mm_to_nm(float(kids[1].value)),
                 units.mm_to_nm(float(kids[2].value)))
        except (TypeError, ValueError):
            continue
        if p in pins:
            kids[1] = _mm(p[0] + dx)
            kids[2] = _mm(p[1] + dy)
            moved = True
    return moved


_CARRY_LABEL_TAGS = ("label", "global_label", "hierarchical_label")


def _op_move_component(doc, op, idx, src_libs, path, ctx) -> list[str]:
    """Move ONE placed instance (designator + optional unit) to x/y.

    Properties travel with the body (their ``(at)`` is absolute, so the same
    delta is applied). By default wires do NOT stretch — the connectivity gate
    flags any endpoint the move disconnected, keeping the edit loud instead of
    silently leaving wires behind.

    ``carry_labels`` / ``carry_wires`` promote the move to a rigid-body
    relocation: any net label (``carry_labels``) or wire endpoint
    (``carry_wires``) anchored on one of this instance's pin tips is shifted by
    the SAME delta. With the label-on-pin connectivity pattern (no cross-part
    wires) ``carry_labels`` makes the move provably net-preserving — every pin
    keeps the label that names its net. This is the atomic operation a group
    re-layout is built from (``akcli arrange --groups``).
    """
    ref = op["designator"]
    unit = int(op.get("unit", 1))
    carry_labels = bool(op.get("carry_labels", False))
    carry_wires = bool(op.get("carry_wires", False))
    sym = next((s for s in _symbols_by_ref(doc, ref) if _symbol_unit(s) == unit), None)
    if sym is None:
        fail("VERIFY_FAILED", f"move_component: no placed instance of {ref!r} unit {unit}")
    if isinstance(op.get("anchor"), str):
        new = _anchored_pos_nm(doc, ctx, op, "move_component")
    else:
        new = geometry.grid_snap_nm(
            (geometry.mil_to_nm(float(op["x_mil"])), geometry.mil_to_nm(float(op["y_mil"]))),
            _GRID_NM,
        )
    at = sym.find("at")
    old = _at_nm(at) if at is not None else (0, 0)
    rot = _fnum_at(at, 3)

    # Pin tips must be sampled BEFORE the body moves — a carried anchor is one
    # sitting on a pre-move pin coordinate.
    pins = (_instance_pin_points_nm(doc, ctx, sym)
            if (carry_labels or carry_wires) else set())

    kids = sym.children or []
    kids[kids.index(at)] = _at(new, rot)
    dx, dy = new[0] - old[0], new[1] - old[1]
    for prop in sym.find_all("property"):
        pat = prop.find("at")
        if pat is None:
            continue
        px, py = _at_nm(pat)
        pkids = prop.children or []
        pkids[pkids.index(pat)] = _at((px + dx, py + dy), _fnum_at(pat, 3))

    carried_labels = carried_wires = 0
    if pins and (dx or dy):
        for c in doc.children or []:
            if not c.is_list or not (c.children or []):
                continue
            head = c.children[0].value
            if carry_labels and head in _CARRY_LABEL_TAGS:
                a = c.find("at")
                if a is not None and _at_nm(a) in pins:
                    _translate_at(c, dx, dy)
                    carried_labels += 1
            elif carry_wires and head in ("wire", "bus"):
                if _translate_wire_pins(c, pins, dx, dy):
                    carried_wires += 1
    if carry_labels or carry_wires:
        raise _Note(
            f"moved {ref!r}; carried {carried_labels} label(s), "
            f"{carried_wires} wire(s)")
    return []


def _fnum_at(at: SNode | None, idx: int) -> float:
    if at is not None and at.children and idx < len(at.children or []):
        try:
            return float(at.children[idx].value or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


class _Note(Exception):
    """Non-error op outcome carrying a human-readable message (status stays ok)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_HANDLERS = {
    "place_component": _op_place_component,
    "set_component_transform": _op_set_component_transform,
    "set_component_parameters": _op_set_component_parameters,
    "add_wire": _op_add_wire,
    "add_bus": _op_add_bus,
    "route_net": _op_route_net,
    "add_junction": _op_add_junction,
    "add_no_connect": _op_add_no_connect,
    "add_net_label": _op_add_net_label,
    "place_power_port": _op_place_power_port,
    "place_gnd": _op_place_power_port,
    "place_vcc": _op_place_power_port,
    "add_bus_entry": _op_add_bus_entry,
    "add_text": _op_add_text,
    "add_rectangle": _op_add_rectangle,
    "add_text_box": _op_add_text_box,
    "add_sheet": _op_add_sheet,
    "set_title_block": _op_set_title_block,
    "delete_component": _op_delete_component,
    "delete_object": _op_delete_object,
    "move_component": _op_move_component,
    "rename_net": _op_rename_net,
}


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def gui_lock_path(path: Path) -> Path:
    """KiCad's GUI lock-file for ``path`` (``~<name>.lck`` in the same dir)."""
    return path.parent / f"~{path.name}.lck"


def apply(
    oplist: dict,
    path: str,
    apply: bool = False,
    *,
    sources: object = None,
    backup_dir: object = None,
    backup_depth: int = _DEFAULT_BACKUP_DEPTH,
    tol_nm: int = 0,
    verify_out: list | None = None,
    allow_open: bool = False,
) -> list[OpResult]:
    """Apply ``oplist`` to the ``.kicad_sch`` at ``path`` (SPEC §3.5).

    Returns one :class:`OpResult` per op. When ``apply`` is ``False`` (the default,
    "--dry-run") nothing is written: the document is edited in memory, connectivity
    is verified, and the results + verify findings (into ``verify_out``) are
    returned. When ``apply`` is ``True`` the edited document is written atomically
    with a backup, but ONLY if every op succeeded and post-write
    :func:`connectivity.verify` reports no errors.

    ``verify_out``, when a list is supplied, is populated with the connectivity
    :class:`~..report.Finding` objects (so the CLI can show them).

    Raises :class:`~..errors.AkcliError` for fatal, document-level problems:
    ``PROTOCOL_MISMATCH`` (op-list major version too new), op-list structural
    errors, a non-``kicad`` ``target_format``, or ``VERIFY_FAILED`` on an
    optimistic-lock conflict / failed post-write verify.
    """
    # --- protocol gate (reject a higher major up front) -------------------- #
    pv = oplist.get("protocol_version")
    if isinstance(pv, int) and pv > PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"op-list protocol_version {pv} > supported {PROTOCOL_VERSION}")

    # --- structural validation (mirrors ops.schema.json) ------------------- #
    errs = validate_oplist(oplist)
    if errs:
        e = errs[0]
        fail(e.code, e.message)

    if oplist.get("target_format") != "kicad":
        fail("OP_UNSUPPORTED",
             f"kicad writer cannot apply target_format {oplist.get('target_format')!r}")

    # --- read + snapshot (optimistic lock) --------------------------------- #
    p = Path(path)

    # GUI-open guard: KiCad drops ~<name>.lck next to an open document. A write
    # under an open GUI is a losing race — the GUI's later save overwrites it
    # from memory. Refuse by default; --allow-open is explicit risk acceptance.
    if apply and not allow_open and gui_lock_path(p).exists():
        fail("TARGET_LOCKED",
             f"{p.name} appears open in the KiCad GUI (found "
             f"{gui_lock_path(p).name}); close it first — or pass --allow-open "
             "and File>Revert in KiCad afterwards, because a GUI save would "
             "overwrite this edit")

    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"file exceeds {MAX_FILE_BYTES} bytes")
    snap_sha = hashlib.sha256(data).hexdigest()
    try:
        snap_mtime = os.stat(p).st_mtime_ns
    except OSError:
        snap_mtime = None

    doc = sexpr.parse(data.decode("utf-8"))

    src_libs = _coerce_source_list(sources)

    # --- apply ops --------------------------------------------------------- #
    results: list[OpResult] = []
    any_error = False
    ctx = _ctx_new()
    for idx, op in enumerate(oplist.get("ops", [])):
        name = op.get("op")
        handler = _HANDLERS.get(name)
        res = OpResult(op_index=idx, op=name)
        if handler is None:
            res.status = "error"
            res.error_code = "OP_UNSUPPORTED"
            res.message = f"no kicad writer for op {name!r}"
            res.remediation = _remediation_for("OP_UNSUPPORTED")
            any_error = True
            results.append(res)
            continue
        try:
            res.created_uuids = handler(doc, op, idx, src_libs, instances.instances_path(doc), ctx)
        except _Note as note:
            res.message = note.message
        except AkcliError as exc:
            res.status = "error"
            res.error_code = exc.code
            res.message = exc.message
            res.remediation = _remediation_for(exc.code)
            any_error = True
        except Exception as exc:  # noqa: BLE001 — per-op containment (SPEC §2.4):
            # a handler bug must surface as ONE failed OpResult, never as a
            # traceback aborting the whole run (nothing is written on any_error).
            res.status = "error"
            res.error_code = "INTERNAL"
            res.message = f"{type(exc).__name__}: {exc}"
            any_error = True
        results.append(res)

    # --- connectivity verify (always; over the resulting bytes) ------------ #
    if not any_error:
        connectivity.auto_junctions(doc, tol_nm=tol_nm)
    text_out = sexpr.dumps(doc)
    redoc = sexpr.parse(text_out)
    findings = [] if any_error else connectivity.verify(redoc, tol_nm=tol_nm)
    if verify_out is not None:
        verify_out.clear()
        verify_out.extend(findings)

    verify_errors = [
        f for f in findings if f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]

    # --- write (only on --apply AND fully clean) --------------------------- #
    if apply and not any_error and not verify_errors:
        _atomic_write(p, text_out, snap_sha, snap_mtime, backup_dir, backup_depth)

    return results


def _coerce_source_list(sources: object) -> list:
    if sources is None:
        return []
    if isinstance(sources, (list, tuple)):
        return list(sources)
    return [sources]


def backup_name(name: str, level: int) -> str:
    """Backup filename for ``level`` >= 1: level 1 is ``<name>.bak``, 2 ``.bak2`` …"""
    return f"{name}.bak" if level <= 1 else f"{name}.bak{level}"


def _rotate_backups(bd: Path, name: str, depth: int) -> None:
    """Shift ``<name>.bak`` -> ``.bak2`` -> … -> ``.bak{depth}`` to free ``.bak``.

    The caller writes a fresh ``<name>.bak`` afterwards, so this makes room while
    keeping the ``depth`` most-recent snapshots; the oldest (``.bak{depth}``) is
    overwritten and any deeper stragglers are left untouched. ``depth`` 1 skips
    rotation entirely (historical single-backup behaviour).
    """
    depth = max(1, int(depth))
    for level in range(depth, 1, -1):
        src = bd / backup_name(name, level - 1)
        if src.exists():
            os.replace(src, bd / backup_name(name, level))


def _atomic_write(
    p: Path, text: str, snap_sha: str, snap_mtime, backup_dir,
    backup_depth: int = _DEFAULT_BACKUP_DEPTH,
) -> None:
    """Snapshot-guarded atomic write: temp -> fsync -> re-parse+verify -> replace.

    Re-stats the original against the snapshot (mtime + sha256) right before the
    swap; a concurrent modification raises ``VERIFY_FAILED`` rather than clobbering
    someone else's edit. The temp is verified one final time and only ``os.replace``
    -d when it parses and verifies error-free.
    """
    # optimistic-lock re-check
    try:
        cur = p.read_bytes()
    except OSError:
        cur = b""
    if hashlib.sha256(cur).hexdigest() != snap_sha:
        fail("VERIFY_FAILED",
             f"{p} changed on disk since it was read (optimistic-lock conflict)")

    directory = p.parent if str(p.parent) else Path(".")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        # re-parse + verify the TEMP file itself (SPEC §3.5)
        verify_doc = sexpr.parse(Path(tmp).read_text(encoding="utf-8"))
        bad = [
            f for f in connectivity.verify(verify_doc)
            if f.severity in (Severity.ERROR, Severity.CRITICAL)
        ]
        if bad:
            fail("VERIFY_FAILED",
                 f"post-write connectivity verify failed: {bad[0].message}")
        if backup_dir is not None and p.exists():
            bd = Path(backup_dir)
            bd.mkdir(parents=True, exist_ok=True)
            # Snapshot FIRST, rotate, then promote atomically: a crash
            # mid-sequence leaves at worst a .bak-pending file next to a
            # fully usable stack — never a level-1 gap with the newest
            # snapshot existing only as the (about-to-change) live file.
            pending = bd / (backup_name(p.name, 1) + ".pending")
            shutil.copy2(p, pending)
            _rotate_backups(bd, p.name, backup_depth)
            os.replace(pending, bd / backup_name(p.name, 1))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
