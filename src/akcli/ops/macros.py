"""Macro ops — compound placements expanded to core ops BEFORE validation.

Macros never reach the JSON schema, the executors, or the live bridge (and
therefore never touch ``protocol_version``). Connectivity uses the
collision-proof label-on-pin pattern (``"REF.PIN"`` anchors), not wires.

Which macros exist, which fields they take and which of those are required is
:mod:`akcli.opspec`'s business (see :mod:`akcli.ops.vocab`); this module is
the expansion BEHAVIOUR only.
"""

from __future__ import annotations

from ..errors import AkcliError
from .base import GRID_MIL, _in, _is_pin_ref, _is_point, parse_mid_anchor
from .lints import _duplicate_placements
from .vocab import MACRO_OPS, MACRO_REQUIRED


def _macro_fail(idx: int, name: str, msg: str) -> None:
    raise AkcliError("OP_UNSUPPORTED", f"[{idx}] {name}: {msg}")


def _macro_num(idx: int, name: str, op: dict, key: str, default=None) -> float:
    v = op.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        _macro_fail(idx, name, f"{key} must be a number")
    return float(v)


def _macro_str(idx: int, name: str, op: dict, key: str, default=None) -> str:
    v = op.get(key, default)
    if not isinstance(v, str) or not v:
        _macro_fail(idx, name, f"{key} must be a non-empty string")
    return v


def _expand_divider(idx: int, op: dict) -> list[dict]:
    """Two series resistors + the four labels that name the chain's nets."""
    x = _macro_num(idx, "place_divider", op, "x_mil")
    y = _macro_num(idx, "place_divider", op, "y_mil")
    spacing = _macro_num(idx, "place_divider", op, "spacing_mil", 400)
    lib_id = _macro_str(idx, "place_divider", op, "lib_id", "Device:R")
    desigs = op.get("designators", ["R1", "R2"])
    values = op.get("values", ["10k", "10k"])
    for key, seq in (("designators", desigs), ("values", values)):
        if (not isinstance(seq, list) or len(seq) != 2
                or not all(isinstance(s, str) and s for s in seq)):
            _macro_fail(idx, "place_divider", f"{key} must be 2 strings")
    top = _macro_str(idx, "place_divider", op, "top_net")
    mid = _macro_str(idx, "place_divider", op, "mid_net")
    bot = _macro_str(idx, "place_divider", op, "bottom_net")
    r1, r2 = desigs
    return [
        {"op": "place_component", "lib_id": lib_id, "designator": r1,
         "x_mil": x, "y_mil": y, "value": values[0]},
        {"op": "place_component", "lib_id": lib_id, "designator": r2,
         "x_mil": x, "y_mil": y + spacing, "value": values[1]},
        # label-on-pin: the shared mid label on BOTH pins is the connection
        {"op": "add_net_label", "name": top, "at": f"{r1}.1"},
        {"op": "add_net_label", "name": mid, "at": f"{r1}.2"},
        {"op": "add_net_label", "name": mid, "at": f"{r2}.1"},
        {"op": "add_net_label", "name": bot, "at": f"{r2}.2"},
    ]


def _macro_position(idx: int, name: str, op: dict, place: dict) -> None:
    """Fill ``place`` with the macro's position: x/y OR anchor (+offset_mil).

    Single-part macros mirror place_component's exactly-one rule; the anchor
    form ("decoupling cap on U1.VCC") passes straight through to the emitted
    place_component, which resolves it at execution time.
    """
    if op.get("anchor") is not None:
        if "x_mil" in op or "y_mil" in op:
            _macro_fail(idx, name,
                        "give either x_mil/y_mil or anchor (+offset_mil), not both")
        place["anchor"] = op["anchor"]
        if "offset_mil" in op:
            place["offset_mil"] = op["offset_mil"]
        return
    if "offset_mil" in op:
        _macro_fail(idx, name, "offset_mil requires anchor")
    place["x_mil"] = _macro_num(idx, name, op, "x_mil")
    place["y_mil"] = _macro_num(idx, name, op, "y_mil")


def _expand_decoupling(idx: int, op: dict) -> list[dict]:
    """One bypass capacitor with its rail + ground labels on the pins."""
    lib_id = _macro_str(idx, "place_decoupling", op, "lib_id", "Device:C")
    desig = _macro_str(idx, "place_decoupling", op, "designator", "C1")
    value = _macro_str(idx, "place_decoupling", op, "value", "100n")
    power = _macro_str(idx, "place_decoupling", op, "power_net")
    gnd = _macro_str(idx, "place_decoupling", op, "gnd_net", "GND")
    place = {"op": "place_component", "lib_id": lib_id, "designator": desig,
             "value": value}
    _macro_position(idx, "place_decoupling", op, place)
    return [
        place,
        {"op": "add_net_label", "name": power, "at": f"{desig}.1"},
        {"op": "add_net_label", "name": gnd, "at": f"{desig}.2"},
    ]


def _macro_desigs(idx: int, name: str, op: dict, key: str,
                  default: list[str]) -> list[str]:
    seq = op.get(key, default)
    if (not isinstance(seq, list) or len(seq) != len(default)
            or not all(isinstance(s, str) and s for s in seq)):
        _macro_fail(idx, name, f"{key} must be {len(default)} strings")
    return seq


def _expand_pullup(idx: int, op: dict) -> list[dict]:
    """One resistor from a signal to a rail (labels on both pins)."""
    desig = _macro_str(idx, "place_pullup", op, "designator", "R1")
    place = {"op": "place_component",
             "lib_id": _macro_str(idx, "place_pullup", op, "lib_id", "Device:R"),
             "designator": desig,
             "value": _macro_str(idx, "place_pullup", op, "value", "10k")}
    _macro_position(idx, "place_pullup", op, place)
    return [
        place,
        {"op": "add_net_label",
         "name": _macro_str(idx, "place_pullup", op, "rail_net"),
         "at": f"{desig}.1"},
        {"op": "add_net_label",
         "name": _macro_str(idx, "place_pullup", op, "net"),
         "at": f"{desig}.2"},
    ]


def _expand_led_indicator(idx: int, op: dict) -> list[dict]:
    """Series R + LED to ground. Device:LED pins: 1 = K, 2 = A."""
    name = "place_led_indicator"
    x = _macro_num(idx, name, op, "x_mil")
    y = _macro_num(idx, name, op, "y_mil")
    spacing = _macro_num(idx, name, op, "spacing_mil", 400)
    r, d = _macro_desigs(idx, name, op, "designators", ["R1", "D1"])
    net = _macro_str(idx, name, op, "net")
    gnd = _macro_str(idx, name, op, "gnd_net", "GND")
    mid = op.get("mid_net") or f"N_{r}_{d}"
    return [
        {"op": "place_component", "lib_id": "Device:R", "designator": r,
         "x_mil": x, "y_mil": y,
         "value": _macro_str(idx, name, op, "r_value", "330")},
        {"op": "place_component", "lib_id": "Device:LED", "designator": d,
         "x_mil": x, "y_mil": y + spacing, "value": "LED"},
        {"op": "add_net_label", "name": net, "at": f"{r}.1"},
        {"op": "add_net_label", "name": mid, "at": f"{r}.2"},
        {"op": "add_net_label", "name": mid, "at": f"{d}.2"},   # anode
        {"op": "add_net_label", "name": gnd, "at": f"{d}.1"},   # cathode
    ]


def _expand_rc_filter(idx: int, op: dict) -> list[dict]:
    """Series R then shunt C to ground (first-order low-pass)."""
    name = "place_rc_filter"
    x = _macro_num(idx, name, op, "x_mil")
    y = _macro_num(idx, name, op, "y_mil")
    spacing = _macro_num(idx, name, op, "spacing_mil", 400)
    r, c = _macro_desigs(idx, name, op, "designators", ["R1", "C1"])
    inn = _macro_str(idx, name, op, "in_net")
    out = _macro_str(idx, name, op, "out_net")
    gnd = _macro_str(idx, name, op, "gnd_net", "GND")
    return [
        {"op": "place_component", "lib_id": "Device:R", "designator": r,
         "x_mil": x, "y_mil": y,
         "value": _macro_str(idx, name, op, "r_value", "1k")},
        {"op": "place_component", "lib_id": "Device:C", "designator": c,
         "x_mil": x, "y_mil": y + spacing,
         "value": _macro_str(idx, name, op, "c_value", "100n")},
        {"op": "add_net_label", "name": inn, "at": f"{r}.1"},
        {"op": "add_net_label", "name": out, "at": f"{r}.2"},
        {"op": "add_net_label", "name": out, "at": f"{c}.1"},
        {"op": "add_net_label", "name": gnd, "at": f"{c}.2"},
    ]


def _expand_crystal(idx: int, op: dict) -> list[dict]:
    """Crystal + two load capacitors to ground (ST AN2867 topology)."""
    name = "place_crystal"
    x = _macro_num(idx, name, op, "x_mil")
    y = _macro_num(idx, name, op, "y_mil")
    spacing = _macro_num(idx, name, op, "spacing_mil", 400)
    yy, c1, c2 = _macro_desigs(idx, name, op, "designators", ["Y1", "C1", "C2"])
    inn = _macro_str(idx, name, op, "in_net")
    out = _macro_str(idx, name, op, "out_net")
    gnd = _macro_str(idx, name, op, "gnd_net", "GND")
    load = _macro_str(idx, name, op, "load_c", "22p")
    xtal_value = op.get("value") or ""
    return [
        {"op": "place_component", "lib_id": "Device:Crystal", "designator": yy,
         "x_mil": x, "y_mil": y,
         **({"value": xtal_value} if xtal_value else {})},
        {"op": "place_component", "lib_id": "Device:C", "designator": c1,
         "x_mil": x - spacing, "y_mil": y + spacing, "value": load},
        {"op": "place_component", "lib_id": "Device:C", "designator": c2,
         "x_mil": x + spacing, "y_mil": y + spacing, "value": load},
        {"op": "add_net_label", "name": inn, "at": f"{yy}.1"},
        {"op": "add_net_label", "name": out, "at": f"{yy}.2"},
        {"op": "add_net_label", "name": inn, "at": f"{c1}.1"},
        {"op": "add_net_label", "name": gnd, "at": f"{c1}.2"},
        {"op": "add_net_label", "name": out, "at": f"{c2}.1"},
        {"op": "add_net_label", "name": gnd, "at": f"{c2}.2"},
    ]


# mid() geometry shared by connect_and_label point endpoints (mils). The pins
# (or points) must be axis-aligned within half a grid step; the midpoint is
# snapped to the 50-mil grid ALONG the wire axis and clamped into the span so
# the anchor can never leave the wire.
_MID_TOL_MIL = 25.0


def _snap_clamp_mil(v: float, a: float, b: float) -> float:
    s = round(v / GRID_MIL) * GRID_MIL
    lo, hi = min(a, b), max(a, b)
    return min(max(s, lo), hi)


def _mid_point_mils(idx: int, name: str, a, b) -> tuple[list, str]:
    """Midpoint of two ``[x, y]`` mil points + the wire axis (``"x"``/``"y"``)."""
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    if min(dx, dy) > _MID_TOL_MIL:
        _macro_fail(idx, name,
                    f"from/to are not axis-aligned: {list(a)} vs {list(b)} mil")
    if dx >= dy:   # wire runs along X
        pt = [_snap_clamp_mil((a[0] + b[0]) / 2, a[0], b[0]), (a[1] + b[1]) / 2]
        axis = "x"
    else:          # wire runs along Y
        pt = [(a[0] + b[0]) / 2, _snap_clamp_mil((a[1] + b[1]) / 2, a[1], b[1])]
        axis = "y"
    return [int(c) if float(c).is_integer() else c for c in pt], axis


def _expand_connect_and_label(idx: int, op: dict) -> list[dict]:
    """Pin-to-pin wire + ONE net label anchored mid-wire.

    The canonical fix for facing-pin label collisions: two coaxial pins that
    each get a label-on-pin extend their texts toward each other
    (LAYOUT_LABEL_OVERLAP). This draws the straight wire and names it once at
    the midpoint instead. Two pin-ref endpoints defer the midpoint to the
    writer via a ``"mid(from,to)"`` anchor (auto-oriented along the wire);
    two ``[x, y]`` endpoints are resolved here. Mixing kinds is rejected —
    a pin's world coordinate is unknown until the document is loaded.
    """
    name = "connect_and_label"
    frm, to = op["from"], op["to"]
    net = _macro_str(idx, name, op, "net")
    label: dict = {"op": "add_net_label", "name": net}
    if _is_pin_ref(frm) and _is_pin_ref(to):
        label["at"] = f"mid({frm},{to})"
    elif _is_point(frm) and _is_point(to):
        at, axis = _mid_point_mils(idx, name, frm, to)
        label["at"] = at
        label["rotation"] = 0 if axis == "x" else 90
    else:
        _macro_fail(idx, name,
                    'from/to must BOTH be "REF.PIN" refs or BOTH [x, y] points')
    if "orientation" in op:
        # the macro's own field keeps its v1 name; the emitted core op must
        # speak protocol 2 (``add_net_label.orientation`` -> ``rotation``)
        label["rotation"] = op["orientation"]
    if "scope" in op:
        label["scope"] = op["scope"]
    return [{"op": "add_wire", "vertices": [frm, to]}, label]


def _expand_pwr_flag(idx: int, op: dict) -> list[dict]:
    """One ``power:PWR_FLAG`` marking a rail as driven (ERC power source).

    ``at`` is ``[x, y]`` or ``"mid(A.p,B.p)"`` — place the flag MID-WIRE, not
    on a pin: on-pin placement stacks the flag body on the pin's symbol (or on
    a power port whose body extends from the same point) and trips
    LAYOUT_SYMBOL_OVERLAP. The default rotation 90 turns the flag body off the
    wire axis into empty space. The flag never names a net (the reader skips
    PWR_FLAG name injection), so it is safe on every rail.
    """
    at = op["at"]
    if not (_is_point(at) or parse_mid_anchor(at) is not None or _is_pin_ref(at)):
        _macro_fail(idx, "place_pwr_flag",
                    'at must be [x, y] or "mid(REF.PIN,REF.PIN)"')
    rotation = op.get("rotation", 90)
    return [{"op": "place_power_port", "lib_id": "power:PWR_FLAG",
             "net_name": "PWR_FLAG", "at": at, "rotation": rotation}]


def _expand_terminate_unused_unit(idx: int, op: dict) -> list[dict]:
    """Place + terminate an unused comparator/op-amp unit (ERC-clean).

    KiCad ERC warns ``missing_input_pin`` on unplaced units of a multi-unit
    part; the standard termination is +input to ground, -input to a rail, and
    the output no-connected. Expands to a ``place_component`` of the given
    unit, two on-pin power ports (gnd at ``in_plus``, vcc at ``in_minus``)
    and an ``add_no_connect`` on ``out``.
    """
    name = "terminate_unused_unit"
    desig = _macro_str(idx, name, op, "designator")
    lib_id = _macro_str(idx, name, op, "lib_id")
    unit = op["unit"]
    if not isinstance(unit, int) or isinstance(unit, bool) or unit < 1:
        _macro_fail(idx, name, "unit must be an integer >= 1")
    at = op["at"]
    if not _is_point(at):
        _macro_fail(idx, name, "at must be an [x, y] point")
    in_plus = _macro_str(idx, name, op, "in_plus")
    in_minus = _macro_str(idx, name, op, "in_minus")
    out = _macro_str(idx, name, op, "out")
    vcc = _macro_str(idx, name, op, "vcc", "VCC")
    gnd = _macro_str(idx, name, op, "gnd", "GND")
    return [
        {"op": "place_component", "lib_id": lib_id, "designator": desig,
         "x_mil": at[0], "y_mil": at[1], "unit": unit},
        {"op": "place_power_port", "lib_id": f"power:{gnd}",
         "net_name": gnd, "at": f"{desig}.{in_plus}"},
        {"op": "place_power_port", "lib_id": f"power:{vcc}",
         "net_name": vcc, "at": f"{desig}.{in_minus}"},
        {"op": "add_no_connect", "pin": f"{desig}.{out}"},
    ]


# Unit step per place_array direction. The KEY SET is the vocabulary (opspec's
# "array_direction" enum, asserted in tests/test_ops_kit.py); the vectors are
# this expander's geometry.
_ARRAY_DIRECTIONS: dict[str, tuple[int, int]] = {
    "right": (1, 0), "down": (0, 1), "left": (-1, 0), "up": (0, -1),
}
_ARRAY_MAX_COUNT = 200


def _expand_array(idx: int, op: dict) -> list[dict]:
    """N identical parts in a row/column at a fixed pitch (R networks, LEDs,
    connector loads ...). Expands to ``count`` ``place_component`` ops named
    ``<prefix><start_index>..``; a colliding prefix is caught by the standard
    duplicate-placement lint. Labels/wiring are left to other ops (macro
    philosophy: placement sugar, connectivity stays explicit).
    """
    name = "place_array"
    x = _macro_num(idx, name, op, "x_mil")
    y = _macro_num(idx, name, op, "y_mil")
    pitch = _macro_num(idx, name, op, "pitch_mil", 400)
    lib_id = _macro_str(idx, name, op, "lib_id")
    prefix = _macro_str(idx, name, op, "designator_prefix")
    count = op.get("count")
    if (not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= _ARRAY_MAX_COUNT):
        _macro_fail(idx, name,
                    f"count must be an integer 1..{_ARRAY_MAX_COUNT}")
    start = op.get("start_index", 1)
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        _macro_fail(idx, name, "start_index must be an integer >= 0")
    direction = op.get("direction", "right")
    if not _in(direction, frozenset(_ARRAY_DIRECTIONS)):
        _macro_fail(idx, name,
                    "direction must be one of right, down, left, up")
    values = op.get("values")
    if values is not None and (
            not isinstance(values, list) or len(values) != count
            or not all(isinstance(v, str) and v for v in values)):
        _macro_fail(idx, name, f"values must be {count} non-empty strings")
    ux, uy = _ARRAY_DIRECTIONS[direction]
    out: list[dict] = []
    for k in range(count):
        o: dict = {"op": "place_component", "lib_id": lib_id,
                   "designator": f"{prefix}{start + k}",
                   "x_mil": x + k * pitch * ux, "y_mil": y + k * pitch * uy}
        v = values[k] if values is not None else op.get("value")
        if isinstance(v, str) and v and not v.startswith("<"):
            o["value"] = v
        if "rotation" in op:
            o["rotation"] = op["rotation"]
        if "footprint" in op:
            o["footprint"] = op["footprint"]
        out.append(o)
    return out


_MACRO_EXPANDERS = {
    "place_divider": _expand_divider,
    "place_decoupling": _expand_decoupling,
    "place_pullup": _expand_pullup,
    "place_led_indicator": _expand_led_indicator,
    "place_rc_filter": _expand_rc_filter,
    "place_crystal": _expand_crystal,
    "connect_and_label": _expand_connect_and_label,
    "place_pwr_flag": _expand_pwr_flag,
    "terminate_unused_unit": _expand_terminate_unused_unit,
    "place_array": _expand_array,
}


def expand_macros(doc: dict, *, origins: list[int] | None = None) -> dict:
    """Expand macro ops into core ops; non-macro ops pass through untouched.

    Returns a new document (the input is never mutated). Bad macro arguments
    raise :class:`AkcliError` (``OP_UNSUPPORTED`` -> exit 6), mirroring the
    validator's contract. The expanded ops are also linted for duplicate
    ``(designator, unit)`` placements — two macros left on their default
    designators would silently place two ``R1``s.

    ``origins``, when a list is passed, is filled with the AUTHOR'S op index
    for every emitted op (one entry per output op). :func:`prepare_oplist`
    uses it to report post-expansion errors against the op the user wrote.
    """
    ops = doc.get("ops")
    if not isinstance(ops, list) or not any(
            isinstance(o, dict) and _in(o.get("op"), MACRO_OPS) for o in ops):
        if origins is not None and isinstance(ops, list):
            origins[:] = list(range(len(ops)))
        return doc
    out: list = []
    src: list[int] = []
    for idx, op in enumerate(ops):
        if isinstance(op, dict) and _in(op.get("op"), MACRO_OPS):
            for f in MACRO_REQUIRED[op["op"]]:
                if f not in op:
                    _macro_fail(idx, op["op"], f"missing required field {f!r}")
            expanded = _MACRO_EXPANDERS[op["op"]](idx, op)
            # A grouped macro stays group-local through expansion: the tag
            # rides onto every child so resolve_groups translates the children
            # (and the writer persists their membership). A sheet-routed
            # macro rides the same way: every child lands on the same file.
            if isinstance(op.get("group"), str):
                for child in expanded:
                    child.setdefault("group", op["group"])
            if isinstance(op.get("sheet"), str):
                for child in expanded:
                    child.setdefault("sheet", op["sheet"])
            out.extend(expanded)
            src.extend([idx] * len(expanded))
        else:
            out.append(op)
            src.append(idx)
    if origins is not None:
        origins[:] = src
    for i, d, u, first in _duplicate_placements(out):
        raise AkcliError(
            "OP_UNSUPPORTED",
            f"[{i}] place_component: duplicate placement of designator {d!r} "
            f"unit {u} after macro expansion (first at expanded op [{first}]; "
            f"give each macro explicit designators)")
    new = dict(doc)
    new["ops"] = out
    return new
