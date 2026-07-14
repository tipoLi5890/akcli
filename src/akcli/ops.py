"""Op-list vocabulary, ``PROTOCOL_VERSION`` and a zero-dependency validator.

The validator mirrors ``schemas/ops.schema.json`` structurally without pulling in
``jsonschema`` at runtime (``jsonschema`` is a dev/test-only dependency). It rejects
free rotation angles, malformed wire vertex arrays, unknown ops and protocol
mismatches using the frozen ERROR codes from :mod:`errors`, and additionally
lints: per-field TYPES (a table mirroring the schema), unknown fields (with a
difflib did-you-mean; keys starting with ``_`` are ignored as annotations), and
duplicate ``(designator, unit)`` placements across the document.

Coordinate/unit contract (SPEC §2.1): origin top-left, +Y down, units mils,
default 50-mil grid. Rotation is an enum ``{0,90,180,270}`` (``add_text`` may use
any ``angle``); mirror is ``{none,x,y}``; wire/port endpoints are ``[x,y]`` points
or a ``"REF.PIN"`` pin reference string. Label/power-port anchors additionally
accept ``"mid(REF.PIN,REF.PIN)"`` — the midpoint of two axis-aligned pins,
snapped to the 50-mil grid along the wire axis (the collision-proof spot for a
net name or a PWR_FLAG on a pin-to-pin wire).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import AkcliError

PROTOCOL_VERSION: int = 1

# Core ops (mirror schemas/ops.schema.json) + documented sugar (place_gnd/place_vcc).
_CORE_OPS: frozenset[str] = frozenset(
    {
        "place_component",
        "set_component_transform",
        "set_component_parameters",
        "add_wire",
        "add_junction",
        "add_no_connect",
        "add_net_label",
        "place_power_port",
        "add_bus",
        "add_bus_entry",
        "add_text",
        "add_sheet",
        "delete_component",
        "delete_object",
        "move_component",
        "rename_net",
    }
)
_SUGAR_OPS: frozenset[str] = frozenset({"place_gnd", "place_vcc"})
OP_NAMES: frozenset[str] = _CORE_OPS | _SUGAR_OPS  # 18 ops total


# --------------------------------------------------------------------------- #
# mid() anchor grammar
# --------------------------------------------------------------------------- #
# "mid(REF.PIN,REF.PIN)" — midpoint of two axis-aligned pins. Resolved by the
# writer against the live document (pins must exist when the op executes).
_MID_RE = re.compile(
    r"^mid\(\s*([^,()\s]+\.[^,()\s]+)\s*,\s*([^,()\s]+\.[^,()\s]+)\s*\)$"
)


def parse_mid_anchor(s: object) -> tuple[str, str] | None:
    """The two ``"REF.PIN"`` arguments of a ``"mid(A.p,B.p)"`` anchor, else ``None``."""
    if not isinstance(s, str):
        return None
    m = _MID_RE.match(s)
    return (m.group(1), m.group(2)) if m else None

# Optional per-op fields with template placeholders (kept in sync with
# schemas/ops.schema.json by test_ops_kit.test_tables_match_schema).
_OP_OPTIONAL: dict[str, dict] = {
    "place_component": {"rotation": 0, "mirror": "none", "unit": 1,
                        "value": "<value>", "footprint": "<Lib:Footprint>",
                        "symbol_source": "<extra.kicad_sym>"},
    "set_component_transform": {"rotation": 0, "mirror": "none"},
    "set_component_parameters": {"reference": "<REF>", "value": "<value>",
                                 "footprint": "<Lib:Footprint>",
                                 "parameters": {"<KEY>": "<VALUE>"}},
    "add_wire": {},
    "add_bus": {},
    "add_junction": {},
    "add_no_connect": {},
    "add_net_label": {"scope": "local", "orientation": 0},
    "place_power_port": {"rotation": 0},
    "place_gnd": {"lib_id": "power:GND", "net_name": "GND", "rotation": 0},
    "place_vcc": {"lib_id": "power:VCC", "net_name": "VCC", "rotation": 0},
    "add_bus_entry": {"size": [100, 100]},
    "add_text": {"angle": 0},
    "add_sheet": {"pins": [{"name": "<pin>", "type": "input",
                            "side": "left", "offset_mil": 0}]},
    "delete_component": {"cascade": False},
    # delete_object takes EXACTLY ONE of uuid | match; the template shows uuid.
    "delete_object": {"uuid": "<object-uuid>"},
    "move_component": {"unit": 1},
    "rename_net": {"scope": "local"},
}

# Required-field template placeholders by field name.
_FIELD_PLACEHOLDER: dict[str, object] = {
    "lib_id": "<Lib:Symbol>",
    "designator": "<REF>",
    "x_mil": 0,
    "y_mil": 0,
    "vertices": ["<REF.PIN>", [0, 0]],
    "at": [0, 0],
    "pin": "<REF.PIN>",
    "name": "<NET_NAME>",
    "net": "<NET_NAME>",
    "net_name": "<NET_NAME>",
    "text": "<free text>",
    "uuid": "<object-uuid>",
}

# Per-op placeholder overrides (win over _FIELD_PLACEHOLDER): the same field
# name means different things across ops (rename_net's from/to are net names,
# connect_and_label's are pin refs; add_bus takes points only).
_TEMPLATE_OVERRIDES: dict[str, dict[str, object]] = {
    "add_bus": {"vertices": [[0, 0], [100, 0]]},
    "rename_net": {"from": "<OLD_NET>", "to": "<NEW_NET>"},
    "connect_and_label": {"from": "<REF.PIN>", "to": "<REF.PIN>"},
    "place_pwr_flag": {"at": "mid(<REF.PIN>,<REF.PIN>)"},
    "add_sheet": {"name": "<sheet-name>", "file": "<child.kicad_sch>",
                  "size": [1000, 800]},
    "terminate_unused_unit": {"unit": 2, "in_plus": "<PIN>",
                              "in_minus": "<PIN>", "out": "<PIN>"},
}


def _placeholder(name: str, field: str) -> object:
    override = _TEMPLATE_OVERRIDES.get(name, {})
    if field in override:
        return override[field]
    return _FIELD_PLACEHOLDER.get(field, f"<{field}>")


def op_template(name: str, *, include_optional: bool = True) -> dict:
    """A fill-in-the-blanks skeleton for one op (see ``akcli ops template``)."""
    if name in MACRO_OPS:
        op = {"op": name}
        for field in MACRO_REQUIRED.get(name, []):
            op[field] = _placeholder(name, field)
        if include_optional:
            for field, placeholder in MACRO_OPTIONAL.get(name, {}).items():
                op.setdefault(field, placeholder)
        return op
    if name not in OP_NAMES:
        raise KeyError(name)
    op: dict = {"op": name}
    for field in _OP_REQUIRED.get(name, []):
        op[field] = _placeholder(name, field)
    if include_optional:
        for field, placeholder in _OP_OPTIONAL.get(name, {}).items():
            op.setdefault(field, placeholder)
    return op

# ---------------------------------------------------------------------------
# Macro ops — compound placements expanded to core ops BEFORE validation, so
# they never reach the JSON schema, the executors, or the live bridge (and
# therefore never touch ``protocol_version``). Connectivity uses the
# collision-proof label-on-pin pattern (``"REF.PIN"`` anchors), not wires.
# ---------------------------------------------------------------------------
MACRO_OPS: frozenset[str] = frozenset({
    "place_divider", "place_decoupling", "place_pullup",
    "place_led_indicator", "place_rc_filter", "place_crystal",
    "connect_and_label", "place_pwr_flag", "terminate_unused_unit",
})

MACRO_REQUIRED: dict[str, list[str]] = {
    "place_divider": ["x_mil", "y_mil", "top_net", "mid_net", "bottom_net"],
    "place_decoupling": ["x_mil", "y_mil", "power_net"],
    "place_pullup": ["x_mil", "y_mil", "net", "rail_net"],
    "place_led_indicator": ["x_mil", "y_mil", "net"],
    "place_rc_filter": ["x_mil", "y_mil", "in_net", "out_net"],
    "place_crystal": ["x_mil", "y_mil", "in_net", "out_net"],
    "connect_and_label": ["from", "to", "net"],
    "place_pwr_flag": ["at"],
    "terminate_unused_unit": ["designator", "lib_id", "unit", "at",
                              "in_plus", "in_minus", "out"],
}
MACRO_OPTIONAL: dict[str, dict] = {
    "place_divider": {"designators": ["R1", "R2"], "values": ["10k", "10k"],
                      "spacing_mil": 400, "lib_id": "Device:R"},
    "place_decoupling": {"designator": "C1", "value": "100n",
                         "gnd_net": "GND", "lib_id": "Device:C"},
    "place_pullup": {"designator": "R1", "value": "10k",
                     "lib_id": "Device:R"},
    "place_led_indicator": {"designators": ["R1", "D1"], "r_value": "330",
                            "gnd_net": "GND", "mid_net": "",
                            "spacing_mil": 400},
    "place_rc_filter": {"designators": ["R1", "C1"], "r_value": "1k",
                        "c_value": "100n", "gnd_net": "GND",
                        "spacing_mil": 400},
    "place_crystal": {"designators": ["Y1", "C1", "C2"], "value": "",
                      "load_c": "22p", "gnd_net": "GND", "spacing_mil": 400},
    "connect_and_label": {"orientation": 0, "scope": "local"},
    "place_pwr_flag": {"rotation": 90},
    "terminate_unused_unit": {"vcc": "VCC", "gnd": "GND"},
}


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


def _expand_decoupling(idx: int, op: dict) -> list[dict]:
    """One bypass capacitor with its rail + ground labels on the pins."""
    x = _macro_num(idx, "place_decoupling", op, "x_mil")
    y = _macro_num(idx, "place_decoupling", op, "y_mil")
    lib_id = _macro_str(idx, "place_decoupling", op, "lib_id", "Device:C")
    desig = _macro_str(idx, "place_decoupling", op, "designator", "C1")
    value = _macro_str(idx, "place_decoupling", op, "value", "100n")
    power = _macro_str(idx, "place_decoupling", op, "power_net")
    gnd = _macro_str(idx, "place_decoupling", op, "gnd_net", "GND")
    return [
        {"op": "place_component", "lib_id": lib_id, "designator": desig,
         "x_mil": x, "y_mil": y, "value": value},
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
    x = _macro_num(idx, "place_pullup", op, "x_mil")
    y = _macro_num(idx, "place_pullup", op, "y_mil")
    desig = _macro_str(idx, "place_pullup", op, "designator", "R1")
    return [
        {"op": "place_component",
         "lib_id": _macro_str(idx, "place_pullup", op, "lib_id", "Device:R"),
         "designator": desig, "x_mil": x, "y_mil": y,
         "value": _macro_str(idx, "place_pullup", op, "value", "10k")},
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
_GRID_MIL = 50.0


def _snap_clamp_mil(v: float, a: float, b: float) -> float:
    s = round(v / _GRID_MIL) * _GRID_MIL
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


def _is_pin_ref(v: object) -> bool:
    return (isinstance(v, str) and not v.startswith("mid(")
            and "." in v and not v.startswith(".") and not v.endswith("."))


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
        label["orientation"] = 0 if axis == "x" else 90
    else:
        _macro_fail(idx, name,
                    'from/to must BOTH be "REF.PIN" refs or BOTH [x, y] points')
    if "orientation" in op:
        label["orientation"] = op["orientation"]
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
}


def _duplicate_placements(ops_list: list) -> list[tuple[int, str, object, int]]:
    """``(op_index, designator, unit, first_index)`` per repeated placement.

    A ``delete_component`` releases its designator (all units), so a
    delete-then-replace sequence in one op-list is NOT a duplicate.
    """
    seen: dict[tuple, int] = {}
    dups: list[tuple[int, str, object, int]] = []
    for i, op in enumerate(ops_list):
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name == "delete_component" and isinstance(op.get("designator"), str):
            gone = op["designator"]
            seen = {k: v for k, v in seen.items() if k[0] != gone}
        elif name == "place_component":
            d, u = op.get("designator"), op.get("unit", 1)
            if isinstance(d, str) and isinstance(u, int) and not isinstance(u, bool):
                key = (d, u)
                if key in seen:
                    dups.append((i, d, u, seen[key]))
                else:
                    seen[key] = i
    return dups


def expand_macros(doc: dict) -> dict:
    """Expand macro ops into core ops; non-macro ops pass through untouched.

    Returns a new document (the input is never mutated). Bad macro arguments
    raise :class:`AkcliError` (``OP_UNSUPPORTED`` -> exit 6), mirroring the
    validator's contract. The expanded ops are also linted for duplicate
    ``(designator, unit)`` placements — two macros left on their default
    designators would silently place two ``R1``s.
    """
    ops = doc.get("ops")
    if not isinstance(ops, list) or not any(
            isinstance(o, dict) and _in(o.get("op"), MACRO_OPS) for o in ops):
        return doc
    out: list = []
    for idx, op in enumerate(ops):
        if isinstance(op, dict) and _in(op.get("op"), MACRO_OPS):
            for f in MACRO_REQUIRED[op["op"]]:
                if f not in op:
                    _macro_fail(idx, op["op"], f"missing required field {f!r}")
            out.extend(_MACRO_EXPANDERS[op["op"]](idx, op))
        else:
            out.append(op)
    for i, d, u, first in _duplicate_placements(out):
        raise AkcliError(
            "OP_UNSUPPORTED",
            f"[{i}] place_component: duplicate placement of designator {d!r} "
            f"unit {u} after macro expansion (first at expanded op [{first}]; "
            f"give each macro explicit designators)")
    new = dict(doc)
    new["ops"] = out
    return new


_VALID_ROTATIONS: frozenset[int] = frozenset({0, 90, 180, 270})
_VALID_MIRRORS: frozenset[str] = frozenset({"none", "x", "y"})
_VALID_SCOPES: frozenset[str] = frozenset({"local", "global", "hierarchical"})
_VALID_TARGETS: frozenset[str] = frozenset({"kicad", "altium"})

# Required fields per op (mirror the schema's `required` arrays).
# delete_object is special: it needs EXACTLY ONE of uuid | match (checked in
# _validate_op), so neither is in its required list.
_OP_REQUIRED: dict[str, list[str]] = {
    "place_component": ["lib_id", "designator", "x_mil", "y_mil"],
    "set_component_transform": ["designator"],
    "set_component_parameters": ["designator"],
    "add_wire": ["vertices"],
    "add_junction": ["at"],
    "add_no_connect": ["pin"],
    "add_net_label": ["name", "at"],
    "place_power_port": ["lib_id", "net_name", "at"],
    "add_bus": ["vertices"],
    "add_bus_entry": ["at"],
    "add_text": ["text", "at"],
    "add_sheet": ["name", "file", "at", "size"],
    "place_gnd": ["at"],
    "place_vcc": ["at"],
    "delete_component": ["designator"],
    "delete_object": [],
    "move_component": ["designator", "x_mil", "y_mil"],
    "rename_net": ["from", "to"],
}

# Per-op field-TYPE table (mirrors the schema; also the unknown-field registry).
# Kinds: str · num · int>=1 (posint) · bool · point · endpoint (point|"REF.PIN")
# · anchor (endpoint|"mid(A.p,B.p)") · dict · rotation/mirror/scope enums ·
# vertices (op-specific, checked in the wire block) · match (selector object).
_OP_FIELDS: dict[str, dict[str, str]] = {
    "place_component": {"lib_id": "str", "designator": "str", "x_mil": "num",
                        "y_mil": "num", "rotation": "rotation",
                        "mirror": "mirror", "unit": "posint", "value": "str",
                        "footprint": "str", "symbol_source": "str"},
    "set_component_transform": {"designator": "str", "rotation": "rotation",
                                "mirror": "mirror"},
    "set_component_parameters": {"designator": "str", "reference": "str",
                                 "value": "str", "footprint": "str",
                                 "parameters": "dict"},
    "add_wire": {"vertices": "vertices"},
    "add_bus": {"vertices": "vertices"},
    "add_junction": {"at": "point"},
    "add_no_connect": {"pin": "endpoint"},
    "add_net_label": {"name": "str", "at": "anchor", "scope": "scope",
                      "orientation": "rotation"},
    "place_power_port": {"lib_id": "str", "net_name": "str", "at": "anchor",
                         "rotation": "rotation"},
    "place_gnd": {"lib_id": "str", "net_name": "str", "at": "anchor",
                  "rotation": "rotation"},
    "place_vcc": {"lib_id": "str", "net_name": "str", "at": "anchor",
                  "rotation": "rotation"},
    "add_bus_entry": {"at": "point", "size": "point"},
    "add_text": {"text": "str", "at": "point", "angle": "num"},
    "add_sheet": {"name": "str", "file": "str", "at": "point", "size": "point",
                  "pins": "sheetpins"},
    "delete_component": {"designator": "str", "cascade": "bool"},
    "delete_object": {"uuid": "str", "match": "match"},
    "move_component": {"designator": "str", "x_mil": "num", "y_mil": "num",
                       "unit": "posint"},
    "rename_net": {"from": "str", "to": "str", "scope": "scope"},
}

# delete_object match selector: object kinds it can address by name/position.
_MATCH_KINDS: frozenset[str] = frozenset({
    "wire", "bus", "label", "global_label", "hierarchical_label",
    "junction", "no_connect", "text", "bus_entry",
})

# add_sheet pin electrical types + edge sides (mirror the KiCad tokens).
_SHEET_PIN_TYPES: frozenset[str] = frozenset({
    "input", "output", "bidirectional", "tri_state", "passive",
})
_SHEET_PIN_SIDES: frozenset[str] = frozenset({"left", "right", "top", "bottom"})


def _in(value: object, choices: frozenset) -> bool:
    """Membership test that treats an UNHASHABLE value as 'not a member'.

    Enum-checked slots (op / target_format / rotation / mirror / scope / sheet
    pin type|side) reach ``value in <frozenset>``; a list/dict there raises
    ``TypeError``. The validator must NEVER raise for a structural problem — it
    returns OpErrors — so this contains the crash into a clean 'not a member'.
    """
    try:
        return value in choices
    except TypeError:
        return False


@dataclass
class OpError:
    """A single structural problem found in an op-list.

    ``op_index`` is ``-1`` for document-level problems (protocol/target/ops shape).
    ``code`` is a frozen ERROR code from :mod:`errors`.
    """

    op_index: int
    op: str | None
    code: str
    message: str


class Op:
    """Typed accessor wrapper over a raw op dict."""

    __slots__ = ("raw",)

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def name(self) -> str | None:
        return self.raw.get("op")

    def __getitem__(self, key: str):
        return self.raw[key]

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def _is_point(v: object) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v)
    )


def _is_endpoint(v: object) -> bool:
    # endpoint = [x,y] point OR a "REF.PIN" reference string (NOT a mid() anchor)
    if isinstance(v, str):
        if v.startswith("mid("):
            return False
        return v.count(".") >= 1 and not v.startswith(".") and not v.endswith(".")
    return _is_point(v)


def _is_anchor(v: object) -> bool:
    # anchor = endpoint OR "mid(REF.PIN,REF.PIN)" (label / power-port `at`)
    if isinstance(v, str) and v.startswith("mid("):
        return parse_mid_anchor(v) is not None
    return _is_endpoint(v)


def _check_rotation(idx: int, name: str, op: dict, key: str, out: list[OpError]) -> None:
    if key in op and not _in(op[key], _VALID_ROTATIONS):
        out.append(OpError(idx, name, "BAD_ANGLE", f"{key} {op[key]!r} not in {{0,90,180,270}}"))


# Generic kind checkers: predicate + human description for the error message.
_KIND_CHECKS: dict[str, tuple] = {
    "str": (lambda v: isinstance(v, str), "a string"),
    "num": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "a number"),
    "posint": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
               "an integer >= 1"),
    "bool": (lambda v: isinstance(v, bool), "true or false"),
    "point": (_is_point, "an [x, y] point"),
    "endpoint": (_is_endpoint, 'an [x, y] point or "REF.PIN"'),
    "anchor": (_is_anchor,
               'an [x, y] point, "REF.PIN" or "mid(REF.PIN,REF.PIN)"'),
    "dict": (lambda v: isinstance(v, dict), "an object"),
}


def _unknown_field(idx: int, name: str, field: str, known,
                   out: list[OpError]) -> None:
    hint = difflib.get_close_matches(field, list(known), n=1)
    suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
    out.append(OpError(idx, name, "OP_UNSUPPORTED",
                       f"{name}: unknown field {field!r}{suggestion}"))


def _check_match(idx: int, name: str, m: object, out: list[OpError]) -> None:
    """Validate a delete_object ``match`` selector {kind, name?, at?}."""
    if not isinstance(m, dict):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match must be an object {{kind, name?, at?}}"))
        return
    kind = m.get("kind")
    if not isinstance(kind, str) or kind not in _MATCH_KINDS:
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.kind must be one of "
                           f"{{{', '.join(sorted(_MATCH_KINDS))}}}"))
    if "name" in m and not isinstance(m["name"], str):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.name must be a string"))
    if "at" in m and not _is_point(m["at"]):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.match.at must be an [x, y] point"))
    for k in m:
        if k not in ("kind", "name", "at") and not k.startswith("_"):
            hint = difflib.get_close_matches(k, ["kind", "name", "at"], n=1)
            suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}: unknown field 'match.{k}'{suggestion}"))


def _check_sheet_pins(idx: int, name: str, pins: object, out: list[OpError]) -> None:
    """Validate an add_sheet ``pins`` array of {name, type, side, offset_mil}."""
    if not isinstance(pins, list):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           f"{name}.pins must be an array of pin objects"))
        return
    for i, pin in enumerate(pins):
        if not isinstance(pin, dict):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}] must be an object "
                               "{name, type, side, offset_mil}"))
            continue
        for req in ("name", "type", "side", "offset_mil"):
            if req not in pin:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name}.pins[{i}] missing required field {req!r}"))
        if "name" in pin and not isinstance(pin["name"], str):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].name must be a string"))
        if "type" in pin and not _in(pin["type"], _SHEET_PIN_TYPES):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].type {pin['type']!r} not in "
                               f"{{{', '.join(sorted(_SHEET_PIN_TYPES))}}}"))
        if "side" in pin and not _in(pin["side"], _SHEET_PIN_SIDES):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].side {pin['side']!r} not in "
                               f"{{left, right, top, bottom}}"))
        off = pin.get("offset_mil")
        if "offset_mil" in pin and (not isinstance(off, (int, float))
                                    or isinstance(off, bool)):
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name}.pins[{i}].offset_mil must be a number"))
        for k in pin:
            if k not in ("name", "type", "side", "offset_mil") and not k.startswith("_"):
                _unknown_field(idx, name, f"pins[{i}].{k}",
                               ["name", "type", "side", "offset_mil"], out)


def _validate_op(idx: int, op: object) -> list[OpError]:
    out: list[OpError] = []
    if not isinstance(op, dict):
        out.append(OpError(idx, None, "OP_UNSUPPORTED", "op must be a JSON object"))
        return out
    name = op.get("op")
    if _in(name, MACRO_OPS):
        # macros are part of the document vocabulary: a not-yet-expanded
        # op-list must validate; plan/draw expand them before execution
        for req in MACRO_REQUIRED.get(name, []):
            if req not in op:
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"missing required field {req!r}"))
        known = set(MACRO_REQUIRED.get(name, [])) | set(MACRO_OPTIONAL.get(name, {}))
        for field in op:
            if field != "op" and field not in known and not field.startswith("_"):
                _unknown_field(idx, name, field, known, out)
        return out
    if not _in(name, OP_NAMES):
        hint = difflib.get_close_matches(
            str(name), list(OP_NAMES | MACRO_OPS), n=1) if isinstance(name, str) else []
        suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
        out.append(OpError(idx, name if isinstance(name, str) else None,
                           "OP_UNSUPPORTED", f"unknown op {name!r}{suggestion}"))
        return out

    for req in _OP_REQUIRED.get(name, []):
        if req not in op:
            out.append(OpError(idx, name, "OP_UNSUPPORTED",
                               f"{name} missing required field {req!r}"))

    if name == "delete_object" and ("uuid" in op) == ("match" in op):
        out.append(OpError(idx, name, "OP_UNSUPPORTED",
                           "delete_object needs exactly one of 'uuid' or 'match'"))

    # field-TYPE table: unknown fields (did-you-mean) + per-field kind checks.
    # Keys starting with "_" are ignored (annotation escape, schema-compatible).
    fields = _OP_FIELDS.get(name, {})
    for field, value in op.items():
        if field == "op" or field.startswith("_"):
            continue
        kind = fields.get(field)
        if kind is None:
            _unknown_field(idx, name, field, fields, out)
        elif kind == "rotation":
            _check_rotation(idx, name, op, field, out)
        elif kind == "mirror":
            if not _in(value, _VALID_MIRRORS):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"mirror {value!r} not in {{none,x,y}}"))
        elif kind == "scope":
            if not _in(value, _VALID_SCOPES):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"scope {value!r} not in {{local,global,hierarchical}}"))
        elif kind == "match":
            _check_match(idx, name, value, out)
        elif kind == "sheetpins":
            _check_sheet_pins(idx, name, value, out)
        elif kind != "vertices":                    # wire block below
            check, desc = _KIND_CHECKS[kind]
            if not check(value):
                out.append(OpError(idx, name, "OP_UNSUPPORTED",
                                   f"{name}.{field} must be {desc}, got {value!r}"))

    if name in ("add_wire", "add_bus"):
        verts = op.get("vertices")
        if not isinstance(verts, list) or len(verts) < 2:
            out.append(OpError(idx, name, "NON_ORTHOGONAL_WIRE",
                               "vertices must be an array of >= 2 points"))
        else:
            endpoint_ok = _is_endpoint if name == "add_wire" else _is_point
            for v in verts:
                if not endpoint_ok(v):
                    out.append(OpError(idx, name, "NON_ORTHOGONAL_WIRE",
                                       f"malformed vertex {v!r}"))
                    break
    return out


def validate_oplist(doc: dict) -> list[OpError]:
    """Structurally validate an op-list document; return all problems found.

    Never raises for structural issues — returns a complete ``OpError`` list so a
    caller can report everything at once. Document-level issues use ``op_index=-1``.
    """
    errs: list[OpError] = []
    if not isinstance(doc, dict):
        return [OpError(-1, None, "OP_UNSUPPORTED", "op-list must be a JSON object")]

    pv = doc.get("protocol_version")
    if pv != PROTOCOL_VERSION:
        errs.append(OpError(-1, None, "PROTOCOL_MISMATCH",
                            f"protocol_version {pv!r} != {PROTOCOL_VERSION}"))

    tf = doc.get("target_format")
    if not _in(tf, _VALID_TARGETS):
        errs.append(OpError(-1, None, "OP_UNSUPPORTED",
                            f"target_format {tf!r} not in {{kicad,altium}}"))

    ops = doc.get("ops")
    if not isinstance(ops, list):
        errs.append(OpError(-1, None, "OP_UNSUPPORTED", "ops must be an array"))
        return errs

    for i, op in enumerate(ops):
        errs.extend(_validate_op(i, op))

    # document-level lint: the same (designator, unit) placed twice
    for i, d, u, first in _duplicate_placements(ops):
        errs.append(OpError(i, "place_component", "OP_UNSUPPORTED",
                            f"duplicate placement of designator {d!r} unit {u} "
                            f"(first placed at op [{first}])"))
    return errs


def load_oplist(path: str | Path) -> dict:
    """Load and JSON-parse an op-list file. Malformed JSON -> ``OP_UNSUPPORTED``."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("OP_UNSUPPORTED", f"invalid op-list JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise AkcliError("OP_UNSUPPORTED", "op-list root must be a JSON object")
    return doc


def _schemas_dir() -> Path:
    # repo_root/schemas (this file: repo_root/src/akcli/ops.py)
    return Path(__file__).resolve().parents[2] / "schemas"


def _schema_text(filename: str) -> str:
    """Schema JSON text: packaged copy first, repo-root ``schemas/`` fallback.

    The packaged mirror (:mod:`.schemas`) ships in wheels where the repo root
    does not exist; the repo-root copy stays CANONICAL (test_schema_exports
    asserts the two are byte-identical).
    """
    try:
        from importlib import resources
        node = resources.files(__package__ + ".schemas") / filename
        if node.is_file():
            return node.read_text(encoding="utf-8")
    except (ImportError, ModuleNotFoundError, OSError):
        pass
    return (_schemas_dir() / filename).read_text(encoding="utf-8")


def load_capabilities() -> dict:
    """Load ``schemas/ops.capabilities.json`` (per-executor support matrix)."""
    return json.loads(_schema_text("ops.capabilities.json"))


def load_schema() -> dict:
    """Load ``schemas/ops.schema.json`` (the op-list JSON schema)."""
    return json.loads(_schema_text("ops.schema.json"))
