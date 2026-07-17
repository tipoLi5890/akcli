"""``akcli bbox`` — world bounding boxes for a hypothetical placement.

The spacing-planning sibling of ``akcli pins``: body box + full box (body
UNION pin tips) per unit, computed through the writer's own transform chain
(``geometry.world_box_from_extent`` / ``pin_world``) so the numbers can never
disagree with where ``draw`` places the part.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli

SYMBOLS = Path(__file__).parent / "fixtures" / "kicad" / "symbols"
DEVICE = str(SYMBOLS / "Device.kicad_sym")
POWER = str(SYMBOLS / "power.kicad_sym")


def _bbox_json(capsys, *argv) -> dict:
    assert cli.main(["bbox", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_bbox_reports_body_and_full_box(capsys):
    d = _bbox_json(capsys, "Device:R", "--at", "1000", "1000",
                   "--symbols", DEVICE)
    assert d["schema_version"]
    assert d["recommended_min_spacing_mil"] == 400
    (u,) = d["units"]
    assert u["pin_only"] is False
    x0, y0, x1, y1 = u["full_box"]
    # full box contains the body box and is centered on the placement
    bx0, by0, bx1, by1 = u["body_box"]
    assert x0 <= bx0 and y0 <= by0 and x1 >= bx1 and y1 >= by1
    assert x0 < 1000 < x1 and y0 < 1000 < y1
    assert u["width_mil"] == x1 - x0
    assert u["height_mil"] == y1 - y0


def test_bbox_rotation_transposes_the_box(capsys):
    d0 = _bbox_json(capsys, "Device:R", "--symbols", DEVICE)
    d90 = _bbox_json(capsys, "Device:R", "--rotation", "90",
                     "--symbols", DEVICE)
    (u0,), (u90,) = d0["units"], d90["units"]
    assert (u90["width_mil"], u90["height_mil"]) == (u0["height_mil"], u0["width_mil"])


def test_bbox_full_box_contains_all_pin_world_coords(capsys):
    """bbox and pins must agree — both delegate to the same transform chain."""
    d = _bbox_json(capsys, "Device:R", "--at", "1000", "1000",
                   "--rotation", "90", "--symbols", DEVICE)
    assert cli.main(["pins", "Device:R", "--at", "1000", "1000",
                     "--rotation", "90", "--symbols", DEVICE, "--json"]) == 0
    pins = json.loads(capsys.readouterr().out)["pins"]
    x0, y0, x1, y1 = d["units"][0]["full_box"]
    for p in pins:
        assert x0 <= p["x_mil"] <= x1 and y0 <= p["y_mil"] <= y1


def test_bbox_text_output_and_missing_lib(capsys):
    assert cli.main(["bbox", "Device:R", "--symbols", DEVICE]) == 0
    out = capsys.readouterr().out
    assert "full_box" in out and "400 mil" in out
    # unknown symbol -> SYMBOL_NOT_FOUND (exit 6, same contract as `pins`)
    assert cli.main(["bbox", "Device:NOPE", "--symbols", DEVICE]) == 6
