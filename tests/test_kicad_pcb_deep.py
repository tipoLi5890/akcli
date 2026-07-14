"""Deep ``.kicad_pcb`` reading: pad nets, tracks, vias, zones, board setup.

Pad positions/rotation were cross-validated against KiCad's own ``pcbnew``
(``pad.GetPosition()``) on a real 4-layer board; these tests pin that behavior
with hand-computable values. Both ``(net N "name")`` (v6-v9) and
``(net "name")`` (KiCad 10 pads) dialects are covered.
"""

from __future__ import annotations

import pytest

from akcli.readers import kicad

_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "pcbnew")
  (general (thickness 1.0))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "V3V3")
  (footprint "Lib:ROT" (layer "F.Cu") (at 100 100 90)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "X" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at 2 1 90) (size 1 0.5)
      (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "V3V3"))
    (pad "2" thru_hole circle (at -2 0 90) (size 1.5 1.5) (drill 0.8)
      (layers "*.Cu" "*.Mask") (net "GND"))
  )
  (gr_line (start 90 90) (end 140 90) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 140 90) (end 140 120) (layer "Edge.Cuts") (width 0.1))
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 105 100) (size 0.4) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via blind (at 106 100) (size 0.4) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 2))
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (name "gnd_pour")
    (polygon (pts (xy 95 95) (xy 130 95) (xy 130 115) (xy 95 115)))
  )
)"""


@pytest.fixture()
def pcb(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_BOARD)
    return kicad.read_pcb(str(p))


def test_pad_rotation_matches_pcbnew(pcb):
    """Footprint at (100,100) rot 90 CCW: local (2,1) -> (100+1, 100-2)."""
    pads = {p["number"]: p for p in pcb.pads}
    assert pads["1"]["at"] == (101.0, 98.0)
    assert pads["2"]["at"] == (100.0, 102.0)


def test_pad_net_dialects(pcb):
    pads = {p["number"]: p for p in pcb.pads}
    assert pads["1"]["net"] == "V3V3"        # (net 2 "V3V3") index+name
    assert pads["2"]["net"] == "GND"         # (net "GND") name-only (KiCad 10)
    assert pads["2"]["pad_type"] == "thru_hole"
    assert pads["2"]["drill"] == pytest.approx(0.8)


def test_tracks_vias_zones(pcb):
    assert pcb.tracks == [{
        "start": (100.0, 100.0), "end": (110.0, 100.0),
        "width": 0.2, "layer": "F.Cu", "net": "GND",
    }]
    assert len(pcb.vias) == 2
    through, blind = pcb.vias
    assert through["type"] == "through"
    assert through["net"] == "GND"
    assert through["drill"] == pytest.approx(0.3)
    assert blind["type"] == "blind"
    assert blind["layers"] == ["F.Cu", "In1.Cu"]
    assert pcb.zones[0]["net"] == "GND"
    assert pcb.zones[0]["bbox"] == ((95.0, 95.0), (130.0, 115.0))


def test_board_setup(pcb):
    assert pcb.board["units"] == "mm"
    assert pcb.board["thickness"] == pytest.approx(1.0)
    assert pcb.board["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert pcb.board["setup"]["pad_to_mask_clearance"] == "0"
    assert pcb.board["outline_bbox"] == ((90.0, 90.0), (140.0, 120.0))


def test_export_is_json_native(pcb):
    import json
    doc = pcb.export()
    json.dumps(doc)                          # must not raise
    assert doc["schema_version"] == "1.2"
    assert doc["pads"][0]["component"] == "U1"
