"""Label auto-orientation, ``REF.PIN`` anchors, justify, and the layout lint.

These pin down the *visual-correctness* layer added on top of the electrical
writer: a net label anchored on a pin must extend AWAY from the symbol body
(angle + the matching ``(justify ...)`` — KiCad ignores a bare 180° and
renders the text over the part), rotated instances keep their Reference/Value
readable, and ``akcli check --layout`` flags geometric pile-ups that ERC can
never see.
"""

from __future__ import annotations

import re
from pathlib import Path

from altium_kicad_cli.checks import layout
from altium_kicad_cli.readers import kicad_lib
from altium_kicad_cli.writers import geometry
from altium_kicad_cli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"

_FRESH = ('(kicad_sch (version 20231120) (generator "akcli") '
          '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')

_POWER_LIB = """(kicad_symbol_lib (version 20231120) (generator "test")
  (symbol "PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0))
    (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
    (symbol "PWR_FLAG_0_0"
      (pin power_out line (at 0 0 90) (length 0)
        (name "pwr" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))))
    (symbol "PWR_FLAG_0_1"
      (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))
        (stroke (width 0) (type default)) (fill (type none)))))
  (symbol "GND_DEEP" (power) (pin_numbers (hide yes)) (pin_names (offset 0))
    (property "Reference" "#PWR" (at 0 -1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GND_DEEP" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))
    (symbol "GND_DEEP_0_0"
      (pin power_in line (at 0 0 270) (length 0)
        (name "gnd" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))))
    (symbol "GND_DEEP_0_1"
      (polyline (pts (xy 0 0) (xy 0 -2.54) (xy -1.27 -2.54) (xy 1.27 -2.54))
        (stroke (width 0) (type default)) (fill (type none))))))
"""


def _oplist(*ops):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops)}


def _fresh(tmp_path: Path) -> Path:
    tgt = tmp_path / "t.kicad_sch"
    tgt.write_text(_FRESH)
    return tgt


def _labels(text: str) -> dict[tuple[str, float, float], tuple[int, str]]:
    """{(name, x_mm, y_mm): (angle, justify)} for every global label."""
    out = {}
    for m in re.finditer(
        r'\(global_label "(\w+)" \(shape input\) \(at ([\d.-]+) ([\d.-]+) (\d+)\)'
        r' \(effects \(font [^)]*\)\)?\s*(?:\(justify ([a-z ]+)\))?',
        text,
    ):
        out[(m.group(1), float(m.group(2)), float(m.group(3)))] = (
            int(m.group(4)), m.group(5) or "",
        )
    return out


# --------------------------------------------------------------------------- #
# geometry: away-angle math
# --------------------------------------------------------------------------- #
def test_label_angle_away_cardinal():
    # Device:R: pin1 tip above body pointing DOWN toward it (lib angle 270).
    # Instance rotation follows eeschema's convention (+90 = CCW on screen,
    # established by tests/test_kicad_parity.py): rot 90 puts pin 1 LEFT.
    assert geometry.label_angle_away(270, 0) == 90     # extends up-screen
    assert geometry.label_angle_away(90, 0) == 270     # bottom pin: down
    assert geometry.label_angle_away(270, 90) == 180   # rotated 90: pin lands left
    assert geometry.label_angle_away(90, 90) == 0
    assert geometry.label_angle_away(180, 0) == 0      # right-side pin (points left)
    assert geometry.label_angle_away(0, 0) == 180      # left-side pin


# --------------------------------------------------------------------------- #
# writer: REF.PIN anchors + auto-orientation + justify
# --------------------------------------------------------------------------- #
def test_on_pin_labels_auto_orient_and_justify(tmp_path):
    tgt = _fresh(tmp_path)
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
             "x_mil": 3000, "y_mil": 2000, "rotation": 90},
            {"op": "add_net_label", "name": "TOP", "at": "R1.1", "scope": "global"},
            {"op": "add_net_label", "name": "BOT", "at": "R1.2", "scope": "global"},
            # eeschema rotation (+90 = CCW on screen): R2.1 lands LEFT, R2.2 RIGHT
            {"op": "add_net_label", "name": "LFT", "at": "R2.1", "scope": "global"},
            {"op": "add_net_label", "name": "RGT", "at": "R2.2", "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert all(r.status == "ok" for r in results)
    labels = {k[0]: v for k, v in _labels(tgt.read_text()).items()}
    # 0/90 pair with (justify left); 180/270 with (justify right) — the angle
    # alone is NOT enough for KiCad to flip the text.
    assert labels["TOP"] == (90, "left")
    assert labels["BOT"] == (270, "right")
    assert labels["LFT"] == (180, "right")
    assert labels["RGT"] == (0, "left")


def test_coordinate_anchor_on_pin_auto_orients(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            # raw coordinate that happens to BE R1.1's tip (150 mil above origin)
            {"op": "add_net_label", "name": "N1", "at": [2000, 1850], "scope": "global"},
            # explicit orientation must always win
            {"op": "add_net_label", "name": "N2", "at": [2000, 2150],
             "scope": "global", "orientation": 0},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    labels = {k[0]: v for k, v in _labels(tgt.read_text()).items()}
    assert labels["N1"] == (90, "left")
    assert labels["N2"] == (0, "left")


def test_power_port_ref_pin_anchor(tmp_path):
    tgt = _fresh(tmp_path)
    lib = tmp_path / "power.kicad_sym"
    lib.write_text(_POWER_LIB)
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_power_port", "lib_id": "PWR_FLAG",
             "net_name": "PWR_FLAG", "at": "R1.1"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE), str(lib)],
    )
    assert all(r.status == "ok" for r in results)
    # the port symbol landed exactly on R1.1's world coordinate (50.8, 46.99 mm)
    assert re.search(r'\(lib_id "PWR_FLAG"\) \(at 50\.8 46\.99 0\)', tgt.read_text())


def test_power_symbol_ref_hidden_even_without_hash(tmp_path):
    """A PWR_FLAG placed as FLG1 (no '#') must still hide its reference."""
    tgt = _fresh(tmp_path)
    lib = tmp_path / "power.kicad_sym"
    lib.write_text(_POWER_LIB)
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "PWR_FLAG",
                 "designator": "FLG1", "x_mil": 2000, "y_mil": 2000}),
        str(tgt), apply=True, sources=[str(lib)],
    )
    m = re.search(r'\(property "Reference" "FLG1"[^\n]*', tgt.read_text())
    assert m and "(hide yes)" in m.group(0)


def test_rotated_instance_properties_counter_rotated(tmp_path):
    """Reference/Value of a rot-90 part carry angle 90 so they render level."""
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:R", "designator": "R9",
                 "x_mil": 2000, "y_mil": 2000, "rotation": 90, "value": "470"}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    text = tgt.read_text()
    ref = re.search(r'\(property "Reference" "R9" \(at [\d.-]+ [\d.-]+ (\d+)\)', text)
    val = re.search(r'\(property "Value" "470" \(at [\d.-]+ [\d.-]+ (\d+)\)', text)
    assert ref and int(ref.group(1)) == 90
    assert val and int(val.group(1)) == 90


def test_autoplace_uses_body_extent(tmp_path):
    """A rot-90 R is WIDE by body (80x200 mil): text goes above/below, outside it."""
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:R", "designator": "R9",
                 "x_mil": 2000, "y_mil": 2000, "rotation": 90, "value": "470"}),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    text = tgt.read_text()
    ref = re.search(r'\(property "Reference" "R9" \(at ([\d.-]+) ([\d.-]+)', text)
    val = re.search(r'\(property "Value" "470" \(at ([\d.-]+) ([\d.-]+)', text)
    body_half_mm = 1.016  # R body is 2.032 mm across, rotated onto the Y axis
    assert float(ref.group(2)) < 50.8 - body_half_mm    # ref above the body
    assert float(val.group(2)) > 50.8 + body_half_mm    # value below the body


# --------------------------------------------------------------------------- #
# kicad_lib: body extent + power detection
# --------------------------------------------------------------------------- #
def test_body_extent_of_fixture_r():
    lib = kicad_lib.read(DEVICE)
    r = kicad_lib.resolve("Device:R", [lib])
    ext = kicad_lib.body_extent_mil(r, 1)
    assert ext is not None
    x0, y0, x1, y1 = ext
    assert (round(x0), round(y0), round(x1), round(y1)) == (-40, -100, 40, 100)
    assert not kicad_lib.is_power_symbol(r)


# --------------------------------------------------------------------------- #
# layout lint
# --------------------------------------------------------------------------- #
def _lint_codes(path: Path) -> set[str]:
    return {f.code for f in layout.run(path)}


def test_layout_clean_schematic(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
             "x_mil": 3000, "y_mil": 2000},
            {"op": "add_net_label", "name": "A", "at": "R1.1", "scope": "global"},
            {"op": "add_net_label", "name": "A", "at": "R2.1", "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert _lint_codes(tgt) == set()


def test_layout_flags_overlapping_symbols(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
             "x_mil": 2050, "y_mil": 2000},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_SYMBOL_OVERLAP in _lint_codes(tgt)


def test_layout_flags_label_into_body(tmp_path):
    """A label on the LEFT pin of a horizontal R, forced to extend RIGHT."""
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000, "rotation": 90},
            # eeschema rotation: R1.1 is the LEFT pin of a rot-90 R
            {"op": "add_net_label", "name": "WRONGWAY", "at": "R1.1",
             "scope": "global", "orientation": 0},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_LABEL_OVER_SYMBOL in _lint_codes(tgt)


def test_layout_flags_coincident_labels(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "add_net_label", "name": "A", "at": "R1.1", "scope": "global"},
            {"op": "add_net_label", "name": "B", "at": "R1.1", "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_COINCIDENT_TEXT in _lint_codes(tgt)


def test_layout_skips_non_kicad(tmp_path):
    f = tmp_path / "x.SchDoc"
    f.write_bytes(b"\xd0\xcf\x11\xe0")
    findings = layout.run(f)
    assert len(findings) == 1 and findings[0].severity.value == "info"


# --------------------------------------------------------------------------- #
# power symbol anchored on a pin tip (dedicated finding + overlap suppression)
# --------------------------------------------------------------------------- #
def _power_on_pin_sch(tmp_path, lib_id: str) -> Path:
    """R1 at (2000,2000) with power symbol ``lib_id`` anchored on R1.1's tip."""
    tgt = _fresh(tmp_path)
    lib = tmp_path / "power.kicad_sym"
    lib.write_text(_POWER_LIB)
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_power_port", "lib_id": lib_id,
             "net_name": lib_id, "at": "R1.1"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE), str(lib)],
    )
    assert all(r.status == "ok" for r in results)
    return tgt


def test_pwr_flag_on_pin_tip_gets_dedicated_warning(tmp_path):
    tgt = _power_on_pin_sch(tmp_path, "PWR_FLAG")
    hits = [f for f in layout.run(tgt) if f.code == layout.LAYOUT_POWER_ON_PIN]
    assert len(hits) == 1
    msg = hits[0].message
    assert msg.startswith("PWR_FLAG")            # special-cased, not "power symbol"
    assert "anchored on R1 pin 1 tip" in msg
    assert "place_pwr_flag" in msg
    assert "R1.1" in hits[0].refs


def test_power_on_pin_suppresses_generic_overlap(tmp_path):
    # GND_DEEP's body digs INTO R1's pin field: without the dedicated rule the
    # generic LAYOUT_SYMBOL_OVERLAP would fire with the WRONG advice ("move
    # one apart" would break the connection).
    tgt = _power_on_pin_sch(tmp_path, "GND_DEEP")
    codes = _lint_codes(tgt)
    assert layout.LAYOUT_POWER_ON_PIN in codes
    assert layout.LAYOUT_SYMBOL_OVERLAP not in codes
    hit = next(f for f in layout.run(tgt)
               if f.code == layout.LAYOUT_POWER_ON_PIN)
    assert "power symbol GND_DEEP" in hit.message


def test_free_standing_power_symbol_is_clean(tmp_path):
    tgt = _fresh(tmp_path)
    lib = tmp_path / "power.kicad_sym"
    lib.write_text(_POWER_LIB)
    results = kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            # mid-wire flag: on the wire, on no pin tip (the blessed pattern)
            {"op": "add_wire", "vertices": [[2000, 1850], [2000, 1650]]},
            {"op": "add_net_label", "name": "VX", "at": [2000, 1650],
             "scope": "global"},
            {"op": "add_junction", "at": [2000, 1750]},
            {"op": "place_component", "lib_id": "PWR_FLAG", "designator": "FLG1",
             "x_mil": 2000, "y_mil": 1750},
        ),
        str(tgt), apply=True, sources=[str(DEVICE), str(lib)],
    )
    assert all(r.status == "ok" for r in results)
    assert layout.LAYOUT_POWER_ON_PIN not in _lint_codes(tgt)


# --------------------------------------------------------------------------- #
# wire through a symbol body / label over a foreign wire
# --------------------------------------------------------------------------- #
def test_layout_flags_wire_through_symbol_body(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            # horizontal wire straight through the R body (x 1960..2040);
            # endpoint labels keep the writer's connectivity gate green
            {"op": "add_wire", "vertices": [[1800, 2000], [2200, 2000]]},
            {"op": "add_net_label", "name": "A", "at": [1800, 2000],
             "scope": "global", "orientation": 180},
            {"op": "add_net_label", "name": "A", "at": [2200, 2000],
             "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    hits = [f for f in layout.run(tgt)
            if f.code == layout.LAYOUT_WIRE_THROUGH_SYMBOL]
    assert len(hits) == 1 and "R1" in hits[0].refs


def test_wire_ending_on_pin_is_not_through_symbol(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "add_wire", "vertices": [[2000, 1850], [2000, 1600]]},
            {"op": "add_net_label", "name": "S", "at": [2000, 1600],
             "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_WIRE_THROUGH_SYMBOL not in _lint_codes(tgt)


def test_layout_flags_label_over_foreign_wire(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            # label on R1.1 auto-extends up; an unrelated wire crosses its text
            {"op": "add_net_label", "name": "SIG", "at": "R1.1", "scope": "global"},
            {"op": "add_wire", "vertices": [[1800, 1700], [2200, 1700]]},
            {"op": "add_net_label", "name": "W", "at": [1800, 1700],
             "scope": "global", "orientation": 180},
            {"op": "add_net_label", "name": "W", "at": [2200, 1700],
             "scope": "global"},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    hits = [f for f in layout.run(tgt)
            if f.code == layout.LAYOUT_LABEL_OVER_WIRE]
    assert len(hits) == 1
    assert hits[0].severity.value == "note"
    assert "SIG" in hits[0].refs


def test_label_anchored_on_wire_is_exempt(tmp_path):
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            # label at the wire's end, extending back over its own wire
            {"op": "add_wire", "vertices": [[2000, 1500], [2000, 1300]]},
            {"op": "add_net_label", "name": "S", "at": [2000, 1500],
             "scope": "global", "orientation": 90},
            {"op": "add_net_label", "name": "S", "at": [2000, 1300],
             "scope": "global", "orientation": 90},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_LABEL_OVER_WIRE not in _lint_codes(tgt)


def test_wire_touching_own_pin_is_connection_not_crossing(tmp_path):
    # A pin-to-pin wire straight through the body shorts the part — that is
    # netbuild's business, not a layout crossing. More importantly, symbols
    # whose graphics overhang a pin tip (LED emission arrows, PWR_FLAG on a
    # mid-wire anchor) made every legitimately terminating wire "penetrate"
    # the hull and fire false LAYOUT_WIRE_THROUGH_SYMBOL warnings.
    tgt = _fresh(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "add_wire", "vertices": [[2000, 1850], [2000, 2150]]},
        ),
        str(tgt), apply=True, sources=[str(DEVICE)],
    )
    assert layout.LAYOUT_WIRE_THROUGH_SYMBOL not in _lint_codes(tgt)
