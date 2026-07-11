"""calc → op-list bridge: turn a computed network into `place_component` ops.

`akcli calc <name> ... --ops out.json` runs the calculator, then emits a
ready-to-edit op-list (protocol_version 1) placing the computed parts with
their **standard E-series values** already filled in. Coordinates are a
simple vertical strip — move them where they belong; `akcli plan` validates
before any write.

Only calculators that resolve to a concrete part network are mappable; the
table below is the contract.
"""

from __future__ import annotations

from .registry import CalcError
from .si import fmt_eng

__all__ = ["MAPPABLE", "to_oplist"]

_X, _Y0, _DY = 1000, 1000, 400


def _val(v: float) -> str:
    """KiCad-style compact value: 4700 -> '4.7k', 1e-8 F -> '10n'."""
    return fmt_eng(v, "").replace(" ", "")


def _r(results, key):
    cell = results.get(key)
    if cell is None or not isinstance(cell.get("value"), (int, float)):
        raise CalcError(f"--ops: result {key!r} missing — nothing to place")
    return float(cell["value"])


# each entry: calc name -> fn(inputs, results) -> [(lib_id, ref, value), ...]
MAPPABLE = {
    "vdivider-design": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "r_top"))),
        ("Device:R", "R2", _val(_r(r, "r_bottom")))],
    "regulator-design": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "r_fixed"))),
        ("Device:R", "R2", _val(_r(r, "r2_standard") if "r2_standard" in r
                                else _r(r, "r_top_standard")))],
    "led": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "r_standard"))),
        ("Device:LED", "D1", "LED")],
    "i2c-pullup": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "suggested"))),
        ("Device:R", "R2", _val(_r(r, "suggested")))],
    "crystal-caps": lambda i, r: [
        ("Device:C", "C1", _val(_r(r, "c1_c2_standard"))),
        ("Device:C", "C2", _val(_r(r, "c1_c2_standard")))],
    "hysteresis-design": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "r1"))),
        ("Device:R", "R2", _val(_r(r, "r2_standard"))),
        ("Device:R", "R3", _val(_r(r, "rh_standard")))],
    "sallen-key": lambda i, r: [
        ("Device:R", "R1", _val(_r(r, "r_standard"))),
        ("Device:R", "R2", _val(_r(r, "r_standard"))),
        ("Device:C", "C1", _val(float(i.get("c", 0)))),
        ("Device:C", "C2", _val(float(i.get("c", 0))))],
    "attenuator": lambda i, r: _attenuator_parts(r),
}


def _attenuator_parts(r):
    parts = []
    n = 1
    for key in ("r_series_std", "r_shunt_std", "r_bridge_std"):
        if key in r:
            parts.append(("Device:R", f"R{n}", _val(_r(r, key))))
            n += 1
            if key == "r_shunt_std":       # PI/TEE have two identical shunts
                parts.append(("Device:R", f"R{n}", _val(_r(r, key))))
                n += 1
    if not parts:
        raise CalcError("--ops: no *_std resistor results found")
    return parts


# Calculators whose network maps onto a MACRO op: the emitted op-list carries
# the compound op (placeholder net names — edit them), and `plan`/`draw`
# expand it into components + pin-anchored labels, so the parts arrive
# CONNECTED instead of as a loose strip.
_MACRO_MAP = {
    "vdivider-design": lambda i, r: [{
        "op": "place_divider", "x_mil": _X, "y_mil": _Y0,
        "top_net": "VIN", "mid_net": "VOUT", "bottom_net": "GND",
        "designators": ["R1", "R2"],
        "values": [_val(_r(r, "r_top")), _val(_r(r, "r_bottom"))]}],
    "led": lambda i, r: [{
        "op": "place_led_indicator", "x_mil": _X, "y_mil": _Y0,
        "net": "LED_CTRL", "gnd_net": "GND",
        "r_value": _val(_r(r, "r_standard"))}],
    "i2c-pullup": lambda i, r: [
        {"op": "place_pullup", "x_mil": _X, "y_mil": _Y0,
         "net": "SDA", "rail_net": "VDD",
         "designator": "R1", "value": _val(_r(r, "suggested"))},
        {"op": "place_pullup", "x_mil": _X + _DY, "y_mil": _Y0,
         "net": "SCL", "rail_net": "VDD",
         "designator": "R2", "value": _val(_r(r, "suggested"))}],
    "crystal-caps": lambda i, r: [{
        "op": "place_crystal", "x_mil": _X, "y_mil": _Y0,
        "in_net": "OSC_IN", "out_net": "OSC_OUT",
        "load_c": _val(_r(r, "c1_c2_standard"))}],
}


def to_oplist(calc_name: str, envelope: dict) -> dict:
    """Build a protocol-1 op-list document from a compute() envelope."""
    if calc_name not in MAPPABLE:
        raise CalcError(
            f"--ops not supported for {calc_name!r}; mappable calculators: "
            + ", ".join(sorted(MAPPABLE)))
    macro_fn = _MACRO_MAP.get(calc_name)
    if macro_fn is not None:
        ops = macro_fn(envelope.get("inputs", {}), envelope.get("results", {}))
    else:
        parts = MAPPABLE[calc_name](
            envelope.get("inputs", {}), envelope.get("results", {}))
        ops = [{
            "op": "place_component",
            "lib_id": lib_id,
            "designator": ref,
            "x_mil": _X,
            "y_mil": _Y0 + idx * _DY,
            "value": value,
        } for idx, (lib_id, ref, value) in enumerate(parts)]
    return {
        "protocol_version": 1,
        "target_format": "kicad",
        "target_file": "<board.kicad_sch>",
        "meta": {
            "generated_by": f"akcli calc {calc_name}",
            "reference": envelope.get("reference", ""),
            **({"note": "macro ops: edit the placeholder net names, then "
                        "`akcli plan` — draw expands them into parts + "
                        "pin-anchored labels"} if macro_fn else {}),
        },
        "ops": ops,
    }
