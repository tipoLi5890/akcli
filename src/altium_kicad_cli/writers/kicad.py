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
* ``place_power_port`` (+ sugar ``place_gnd`` / ``place_vcc``) — a power symbol with
  an auto-allocated ``#PWR0<n>`` reference.

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
from ..ops import PROTOCOL_VERSION, validate_oplist
from ..readers import kicad_lib, sexpr
from ..readers.sexpr import SNode
from ..report import Severity
from ..safety import MAX_FILE_BYTES
from . import connectivity, geometry, instances, lib_cache

__all__ = ["OpResult", "apply"]

# Default schematic grid (50 mil) in integer nm — raw [x,y] endpoints snap here.
_GRID_NM = geometry.DEFAULT_GRID_NM

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

    def to_dict(self) -> dict:
        return {
            "op_index": self.op_index,
            "op": self.op,
            "status": self.status,
            "created_uuids": list(self.created_uuids),
            "error_code": self.error_code,
            "message": self.message,
        }


# --------------------------------------------------------------------------- #
# small SNode construction helpers
# --------------------------------------------------------------------------- #
def _q(value: object) -> str:
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


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


def _stroke() -> SNode:
    return _list(
        _atom("stroke"),
        _list(_atom("width"), _atom("0")),
        _list(_atom("type"), _atom("default")),
    )


def _effects() -> SNode:
    return _list(
        _atom("effects"),
        _list(_atom("font"), _list(_atom("size"), _atom("1.27"), _atom("1.27"))),
    )


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


def _remove_top_by_uuid(doc: SNode, uid: str) -> bool:
    """Remove the first top-level node whose ``(uuid ...)`` equals ``uid``."""
    for i, c in enumerate(doc.children or []):
        if not c.is_list:
            continue
        u = c.find("uuid")
        if u is not None and len(u.children or []) >= 2 and u.children[1].value == uid:
            del doc.children[i]            # type: ignore[union-attr]
            del doc.ws[i]                  # type: ignore[union-attr]
            return True
    return False


def _append_top_idempotent(doc: SNode, child: SNode, uid: str) -> None:
    """Idempotent top-level append: drop any prior node with ``uid`` first.

    Re-running an op-list yields the same deterministic ``uid`` per created node,
    so replacing-then-appending makes ``draw --apply`` converge (SPEC risk #7)
    instead of accumulating duplicate wires/labels/junctions on every run.
    """
    _remove_top_by_uuid(doc, uid)
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


def _inline_library(doc: SNode) -> model.Library:
    """A :class:`model.Library` view of the document's current ``(lib_symbols)``."""
    node = doc.find("lib_symbols")
    if node is not None:
        return kicad_lib.library_from_lib_symbols(node, "<inline>")
    return model.Library(source_path="<inline>", source_format="kicad", symbols=[])


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


def _resolve_pin_world(doc: SNode, library, ref: str, pin_number: str) -> tuple[int, int]:
    """World coordinate (nm) of ``ref.pin_number`` in the current document."""
    sym = _symbol_by_ref(doc, ref)
    if sym is None:
        fail("VERIFY_FAILED", f"pin reference {ref!r} matches no placed component")
    lib_id = sym.find("lib_id").children[1].value or ""
    try:
        symdef = kicad_lib.resolve(lib_id, [library])
    except AkcliError:
        fail("SYMBOL_NOT_FOUND", f"pin reference {ref!r}: lib_id {lib_id!r} not in cache")
    comp = _instance_component(sym, lib_id)
    for pin in symdef.pins:
        if pin.number == pin_number:
            return geometry.pin_world(symdef, comp, pin)
    fail("VERIFY_FAILED", f"component {ref!r} has no pin {pin_number!r}")


def _resolve_endpoint(doc: SNode, library, ep: object) -> tuple[int, int]:
    """Resolve a wire/port endpoint to integer-nm coordinates.

    ``"REF.PIN"`` snaps to the pin's world coordinate (exact, never grid-snapped);
    a raw ``[x, y]`` mil point is converted to nm and snapped to the 50-mil grid.
    """
    if isinstance(ep, str):
        ref, pin_number = ep.rsplit(".", 1)
        return _resolve_pin_world(doc, library, ref, pin_number)
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
    prop = _list(
        _atom("property"),
        _atom(_q(key)),
        _atom(_q(value)),
        _at(pos, 0),
        _effects(),
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


# --------------------------------------------------------------------------- #
# symbol-instance construction
# --------------------------------------------------------------------------- #
def _make_symbol(
    lib_id: str, pos_nm: tuple[int, int], rotation: int, mirror: str,
    uuid_str: str, pin_numbers: list[str], pin_uuids: list[str],
) -> SNode:
    """Build a placed ``(symbol ...)`` node with per-pin ``(pin "N" (uuid))``."""
    children = [
        _list(_atom("lib_id"), _atom(_q(lib_id))),
        _at(pos_nm, rotation),
        _list(_atom("unit"), _atom("1")),
    ]
    if mirror in ("x", "y"):
        children.insert(2, _list(_atom("mirror"), _atom(mirror)))
    children.append(_uuid_node(uuid_str))
    for num, puid in zip(pin_numbers, pin_uuids):
        children.append(_list(_atom("pin"), _atom(_q(num)), _uuid_node(puid)))
    return _list(_atom("symbol"), *children)


def _place_symbol(
    doc: SNode, library, src_libs: list, lib_id: str, designator: str,
    pos_nm: tuple[int, int], rotation: int, mirror: str, op_index: int, path: str,
) -> str:
    """Cache + place a symbol instance; return its (deterministic) uuid."""
    lib_cache.ensure_cached(doc, lib_id, src_libs)
    library = _inline_library(doc)        # refresh after caching
    symdef = kicad_lib.resolve(lib_id, [library])
    pin_numbers = [p.number for p in symdef.pins]

    root = _root_uuid(doc)
    sym_uuid = instances.deterministic_uuid(root, designator, op_index)
    pin_uuids = [
        instances.deterministic_uuid(root, f"{designator}.pin{n}", op_index)
        for n in pin_numbers
    ]

    # Idempotent replay: a same-uuid instance already present is replaced wholesale.
    sym = _make_symbol(lib_id, pos_nm, rotation, mirror, sym_uuid, pin_numbers, pin_uuids)
    _append_top_idempotent(doc, sym, sym_uuid)
    instances.write_instance(doc, sym, designator, path)
    return sym_uuid


# --------------------------------------------------------------------------- #
# per-op handlers (each returns the list of created uuids)
# --------------------------------------------------------------------------- #
def _op_place_component(doc, op, idx, src_libs, path) -> list[str]:
    sources = list(src_libs)
    if op.get("symbol_source"):
        sources = [op["symbol_source"], *sources]
    rotation = int(op.get("rotation", 0))
    mirror = op.get("mirror", "none")
    pos = geometry.grid_snap_nm(
        (geometry.mil_to_nm(float(op["x_mil"])), geometry.mil_to_nm(float(op["y_mil"]))),
        _GRID_NM,
    )
    uid = _place_symbol(
        doc, _inline_library(doc), sources, op["lib_id"], op["designator"],
        pos, rotation, mirror, idx, path,
    )
    sym = _symbol_by_ref(doc, op["designator"])
    if op.get("value") is not None and sym is not None:
        _set_property(sym, "Value", str(op["value"]))
    if op.get("footprint") is not None and sym is not None:
        _set_property(sym, "Footprint", str(op["footprint"]))
    return [uid]


def _op_set_component_transform(doc, op, idx, src_libs, path) -> list[str]:
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


def _op_set_component_parameters(doc, op, idx, src_libs, path) -> list[str]:
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


def _op_add_wire(doc, op, idx, src_libs, path, tag="wire") -> list[str]:
    library = _inline_library(doc)
    verts = op["vertices"]
    pts = [_resolve_endpoint(doc, library, v) for v in verts]
    root = _root_uuid(doc)
    created: list[str] = []
    for n, (a, b) in enumerate(zip(pts, pts[1:])):
        if a == b:
            continue
        uid = instances.deterministic_uuid(root, f"{tag}:{a[0]}:{a[1]}:{b[0]}:{b[1]}", idx)
        node = _list(_atom(tag), _pts([a, b]), _stroke(), _uuid_node(uid))
        _append_top_idempotent(doc, node, uid)
        created.append(uid)
    return created


def _op_add_bus(doc, op, idx, src_libs, path) -> list[str]:
    return _op_add_wire(doc, op, idx, src_libs, path, tag="bus")


def _op_add_junction(doc, op, idx, src_libs, path) -> list[str]:
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


def _op_add_no_connect(doc, op, idx, src_libs, path) -> list[str]:
    library = _inline_library(doc)
    ep = op["pin"]
    if isinstance(ep, str) and "." in ep:
        ref, pin_number = ep.rsplit(".", 1)
        p = _resolve_pin_world(doc, library, ref, pin_number)
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


def _op_add_net_label(doc, op, idx, src_libs, path) -> list[str]:
    p = _point_nm(op["at"])
    scope = op.get("scope", "local")
    orientation = int(op.get("orientation", 0))
    tag = {"local": "label", "global": "global_label", "hierarchical": "hierarchical_label"}[scope]
    root = _root_uuid(doc)
    uid = instances.deterministic_uuid(root, f"{tag}:{op['name']}:{p[0]}:{p[1]}", idx)
    children = [_atom(tag), _atom(_q(str(op["name"])))]
    if tag != "label":
        children.append(_list(_atom("shape"), _atom("input")))
    children += [_at(p, orientation), _effects(), _uuid_node(uid)]
    _append_top_idempotent(doc, _list(*children), uid)
    return [uid]


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


def _op_place_power_port(doc, op, idx, src_libs, path) -> list[str]:
    name = op.get("op")
    if name in _SUGAR_POWER:
        default_lib, default_net = _SUGAR_POWER[name]
        lib_id = op.get("lib_id", default_lib)
        net_name = op.get("net_name", default_net)
    else:
        lib_id = op["lib_id"]
        net_name = op["net_name"]
    pos = _point_nm(op["at"])
    rotation = int(op.get("rotation", 0))
    # Reuse the ref from a prior replay of this op so the auto-allocated #PWR
    # designator (and thus the deterministic uuid) stays stable -> idempotent.
    ref = _existing_pwr_ref_for_op(doc, _root_uuid(doc), idx) or instances.alloc_pwr_ref(doc)
    uid = _place_symbol(
        doc, _inline_library(doc), list(src_libs), lib_id, ref,
        pos, rotation, "none", idx, path,
    )
    sym = _symbol_by_ref(doc, ref)
    if sym is not None:
        _set_property(sym, "Value", str(net_name))
    return [uid]


def _op_add_bus_entry(doc, op, idx, src_libs, path) -> list[str]:
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


def _op_add_text(doc, op, idx, src_libs, path) -> list[str]:
    p = _point_nm(op["at"])
    angle = float(op.get("angle", 0))
    root = _root_uuid(doc)
    uid = instances.deterministic_uuid(root, f"text:{p[0]}:{p[1]}", idx)
    node = _list(
        _atom("text"),
        _atom(_q(str(op["text"]))),
        _at(p, angle),
        _effects(),
        _uuid_node(uid),
    )
    _append_top_idempotent(doc, node, uid)
    return [uid]


_HANDLERS = {
    "place_component": _op_place_component,
    "set_component_transform": _op_set_component_transform,
    "set_component_parameters": _op_set_component_parameters,
    "add_wire": _op_add_wire,
    "add_bus": _op_add_bus,
    "add_junction": _op_add_junction,
    "add_no_connect": _op_add_no_connect,
    "add_net_label": _op_add_net_label,
    "place_power_port": _op_place_power_port,
    "place_gnd": _op_place_power_port,
    "place_vcc": _op_place_power_port,
    "add_bus_entry": _op_add_bus_entry,
    "add_text": _op_add_text,
}


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def apply(
    oplist: dict,
    path: str,
    apply: bool = False,
    *,
    sources: object = None,
    backup_dir: object = None,
    tol_nm: int = 0,
    verify_out: list | None = None,
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
    for idx, op in enumerate(oplist.get("ops", [])):
        name = op.get("op")
        handler = _HANDLERS.get(name)
        res = OpResult(op_index=idx, op=name)
        if handler is None:
            res.status = "error"
            res.error_code = "OP_UNSUPPORTED"
            res.message = f"no kicad writer for op {name!r}"
            any_error = True
            results.append(res)
            continue
        try:
            res.created_uuids = handler(doc, op, idx, src_libs, instances.instances_path(doc))
        except AkcliError as exc:
            res.status = "error"
            res.error_code = exc.code
            res.message = exc.message
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
        _atomic_write(p, text_out, snap_sha, snap_mtime, backup_dir)

    return results


def _coerce_source_list(sources: object) -> list:
    if sources is None:
        return []
    if isinstance(sources, (list, tuple)):
        return list(sources)
    return [sources]


def _atomic_write(
    p: Path, text: str, snap_sha: str, snap_mtime, backup_dir
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
            shutil.copy2(p, bd / (p.name + ".bak"))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
