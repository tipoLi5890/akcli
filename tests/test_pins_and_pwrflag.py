"""Tests for two op-list-authoring improvements:

* ``akcli pins`` — prints a symbol's pin *world* coordinates for a placement, so
  op-list authors can target exact points (mirrors ``geometry.pin_world``).
* PWR_FLAG net-naming fix — a ``power:PWR_FLAG`` symbol marks a net as driven for
  ERC but must NOT inject a net name; two flags on two rails must stay separate
  (previously they merged every rail they touched into one net — a false short).

Both are exercised through a tiny self-contained ``.kicad_sym`` (no KiCad install
needed): a resistor plus ``+5V`` / ``GND`` / ``PWR_FLAG`` power symbols.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import kicad as kreader

# A minimal KiCad symbol library: R (2 passive pins), +5V / GND power ports, and
# PWR_FLAG (a (power) symbol whose single pin is power_out). Symbol names are
# unqualified — the resolver matches "Device:R" -> "R", "power:PWR_FLAG" -> ...
_MIN_SYM = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "R" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "+5V" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "+5V" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "+5V_1_1"
      (pin power_in line (at 0 0 90) (length 0)
        (name "+5V" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "GND" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "GND_1_1"
      (pin power_in line (at 0 0 270) (length 0)
        (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "PWR_FLAG" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "PWR_FLAG_1_1"
      (pin power_out line (at 0 0 90) (length 0)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
)
"""


def _sym(tmp_path: Path) -> Path:
    p = tmp_path / "min.kicad_sym"
    p.write_text(_MIN_SYM)
    return p


def _seed(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_sch"
    p.write_text(f'(kicad_sch (version 20231120) (generator "akcli") '
                 f'(uuid "{uuid.uuid4()}") (paper "A4"))\n')
    return p


def _ops(tmp_path: Path, ops: list) -> Path:
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({"protocol_version": 1, "target_format": "kicad", "ops": ops}))
    return p


# --------------------------------------------------------------------------- #
# akcli pins
# --------------------------------------------------------------------------- #
def test_pins_reports_world_coords(tmp_path, capsys):
    sym = _sym(tmp_path)
    rc = main(["pins", "Device:R", "--at", "1000", "1000",
               "--symbols", str(sym), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT["OK"]
    assert payload["lib_id"] == "Device:R"
    by = {p["number"]: (p["x_mil"], p["y_mil"]) for p in payload["pins"]}
    # pin1 local +3.81mm (150mil, +Y up) -> world y = 1000-150; pin2 -> 1000+150
    assert by["1"] == (1000, 850)
    assert by["2"] == (1000, 1150)
    assert all(p["type"] == "passive" for p in payload["pins"])


def test_pins_unknown_symbol_errors(tmp_path, capsys):
    sym = _sym(tmp_path)
    rc = main(["pins", "Device:NOPE", "--symbols", str(sym)])
    assert rc == EXIT["OPLIST"]                    # SYMBOL_NOT_FOUND -> exit 6
    assert "SYMBOL_NOT_FOUND" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# PWR_FLAG must not merge rails
# --------------------------------------------------------------------------- #
def _build_two_rails_with_flags(tmp_path) -> Path:
    """R1 with +5V+PWR_FLAG on pin1 and GND+PWR_FLAG on pin2 (all coincident)."""
    sym = _sym(tmp_path)
    tgt = _seed(tmp_path)
    p1, p2 = [1000, 850], [1000, 1150]            # R1 pin world coords @ (1000,1000)
    ops = _ops(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000, "value": "1k"},
        {"op": "place_power_port", "lib_id": "power:+5V", "net_name": "+5V", "at": p1},
        {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND", "at": p2},
        {"op": "place_component", "lib_id": "power:PWR_FLAG", "designator": "FLGA",
         "x_mil": p1[0], "y_mil": p1[1]},
        {"op": "place_component", "lib_id": "power:PWR_FLAG", "designator": "FLGB",
         "x_mil": p2[0], "y_mil": p2[1]},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply", "--symbols", str(sym)])
    assert rc == EXIT["OK"]
    return tgt


def test_pwr_flag_does_not_merge_rails(tmp_path):
    sch = kreader.read_sch(_build_two_rails_with_flags(tmp_path))
    net1 = next((n for n in sch.nets if ("R1", "1") in n.members), None)
    net2 = next((n for n in sch.nets if ("R1", "2") in n.members), None)
    assert net1 is not None and net2 is not None
    # The two rails must stay SEPARATE (the bug merged them into one net).
    assert ("R1", "2") not in net1.members
    assert ("R1", "1") not in net2.members
    names = set(net1.source_names) | set(net2.source_names)
    assert "+5V" in names and "GND" in names
    assert "PWR_FLAG" not in names                # flag never names a net
    # each flag still lands on its rail (provides the ERC power-source pin)
    assert ("FLGA", "1") in net1.members
    assert ("FLGB", "1") in net2.members
