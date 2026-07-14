"""Structured positions on findings: text ``@ (x,y)``, JSON pos/anchors, SARIF."""

from __future__ import annotations

import json
from pathlib import Path

from akcli.checks import bom, intent, libsync
from akcli.model import Component, Net, Pin, Schematic
from akcli.report import Finding, Severity, anchor, render


def test_anchor_helper_shape():
    a = anchor("pin", "U1.3", (100, 200))
    assert a == {"kind": "pin", "id": "U1.3", "pos": (100.0, 200.0)}
    # pos is optional (a net anchor has no single coordinate)
    assert anchor("net", "GND") == {"kind": "net", "id": "GND"}


def test_text_appends_position():
    f = Finding("X", Severity.WARNING, "msg", refs=["U1.3"], pos=(150.0, 275.0))
    out = render([f], fmt="text")
    assert "@ (150,275)" in out
    # a positionless finding does not grow an @-clause
    out2 = render([Finding("Y", Severity.NOTE, "no pos")], fmt="text")
    assert "@" not in out2


def test_json_emits_pos_and_anchors_verbatim():
    f = Finding(
        "X", Severity.WARNING, "msg", refs=["U1.3"],
        pos=(150.0, 275.0), anchors=[anchor("pin", "U1.3", (150, 275))],
    )
    payload = json.loads(render([f], fmt="json"))
    fj = payload["findings"][0]
    assert fj["pos"] == [150.0, 275.0]
    assert fj["anchors"] == [{"kind": "pin", "id": "U1.3", "pos": [150.0, 275.0]}]


def test_json_positionless_finding_keeps_historical_shape():
    f = Finding("X", Severity.WARNING, "msg", refs=["R1.2"])
    fj = json.loads(render([f], fmt="json"))["findings"][0]
    assert fj == {"code": "X", "severity": "warning", "message": "msg",
                  "refs": ["R1.2"]}
    assert "pos" not in fj and "anchors" not in fj


def test_sarif_logical_locations_and_properties_but_stable_fingerprint():
    plain = Finding("X", Severity.WARNING, "msg", refs=["U1.3"])
    placed = Finding("X", Severity.WARNING, "msg", refs=["U1.3"],
                     pos=(10, 20), anchors=[anchor("pin", "U1.3", (10, 20))])
    fp_plain = json.loads(render([plain], "sarif", {}, source="s.kicad_sch"))[
        "runs"][0]["results"][0]["partialFingerprints"]["akcliFinding/v1"]
    r = json.loads(render([placed], "sarif", {}, source="s.kicad_sch"))[
        "runs"][0]["results"][0]
    # position must NOT perturb the fingerprint (alert identity stays stable)
    assert r["partialFingerprints"]["akcliFinding/v1"] == fp_plain
    logical = r["locations"][0]["logicalLocations"]
    assert logical == [{"name": "U1.3", "kind": "pin"}]
    assert r["properties"]["akcli"]["pos"] == [10, 20]
    assert r["properties"]["akcli"]["anchors"][0]["kind"] == "pin"


# --------------------------------------------------------------------------- #
# checks/bom.py — component anchors
# --------------------------------------------------------------------------- #
def _bcomp(designator, x, y, **kw) -> Component:
    return Component(designator=designator, library_ref="Device:R",
                      x_mil=x, y_mil=y, value=kw.pop("value", "10k"),
                      footprint=kw.pop("footprint", "0402"), **kw)


def test_bom_missing_value_anchors_component_position():
    sch = Schematic(source_path="<t>", source_format="altium",
                     components=[_bcomp("R1", 100.0, 200.0, value=None)], nets=[])
    findings = [f for f in bom.run(sch) if f.code == "BOM_MISSING_VALUE"]
    assert len(findings) == 1
    f = findings[0]
    assert f.pos == (100.0, 200.0)
    assert f.anchors == [anchor("component", "R1", (100.0, 200.0))]


def test_bom_refdes_gap_stays_positionless():
    """A gap names refdes that don't exist as components -- no position exists."""
    sch = Schematic(
        source_path="<t>", source_format="altium",
        components=[_bcomp("R7", 0.0, 0.0), _bcomp("R12", 50.0, 50.0)], nets=[],
    )
    findings = [f for f in bom.run(sch) if f.code == "BOM_REFDES_GAP"]
    assert len(findings) == 1
    f = findings[0]
    assert f.pos is None and f.anchors == []
    fj = json.loads(render([f], fmt="json"))["findings"][0]
    assert "pos" not in fj and "anchors" not in fj


# --------------------------------------------------------------------------- #
# checks/intent.py — pin + net anchors
# --------------------------------------------------------------------------- #
def _icomp(designator: str, pins: dict[str, tuple[float, float]]) -> Component:
    return Component(
        designator=designator, library_ref="Device:U", x_mil=0.0, y_mil=0.0,
        pins=[Pin(number=n, name=None, x_mil=x, y_mil=y) for n, (x, y) in pins.items()],
    )


def _spec(nets: dict[str, list[tuple[str, str]]]) -> intent.IntentSpec:
    out = {
        name: intent.NetSpec(members=[intent.Member(ref=r, pin=p) for r, p in members])
        for name, members in nets.items()
    }
    return intent.IntentSpec(nets=out)


def test_intent_missing_member_anchors_pin_and_net():
    sch = Schematic(
        source_path="<t>", source_format="altium",
        components=[
            _icomp("U1", {"1": (10.0, 20.0), "2": (30.0, 40.0)}),
            _icomp("U2", {"1": (50.0, 60.0)}),
        ],
        nets=[Net(name="SWCLK", members=[("U1", "1"), ("U2", "1")])],
    )
    spec = _spec({"SWCLK": [("U1", "1"), ("U1", "2")]})  # U1.2 is not on SWCLK
    findings = [f for f in intent.run(sch, spec) if f.code == "INTENT_MISSING_MEMBER"]
    assert len(findings) == 1
    f = findings[0]
    assert f.pos == (30.0, 40.0)
    assert anchor("pin", "U1.2", (30.0, 40.0)) in f.anchors
    assert anchor("net", "SWCLK") in f.anchors


def test_intent_pin_unknown_stays_positionless():
    sch = Schematic(
        source_path="<t>", source_format="altium",
        components=[_icomp("U1", {"1": (10.0, 20.0)})],
        nets=[Net(name="SWCLK", members=[("U1", "1")])],
    )
    spec = _spec({"SWCLK": [("U1", "1"), ("U9", "9")]})  # U9 doesn't exist
    findings = [f for f in intent.run(sch, spec) if f.code == "INTENT_PIN_UNKNOWN"]
    assert len(findings) == 1
    f = findings[0]
    assert f.pos is None and f.anchors == []
    fj = json.loads(render([f], fmt="json"))["findings"][0]
    assert "pos" not in fj and "anchors" not in fj


# --------------------------------------------------------------------------- #
# checks/libsync.py — first-placed-instance anchor
# --------------------------------------------------------------------------- #
_R2 = (
    '\t(symbol "R2"\n'
    "\t\t(exclude_from_sim no)\n"
    "\t\t(in_bom yes)\n"
    "\t\t(on_board yes)\n"
    '\t\t(property "Reference" "R"\n'
    "\t\t\t(at 2.032 0 90)\n"
    "\t\t\t(effects (font (size 1.27 1.27))))\n"
    '\t\t(symbol "R2_0_1"\n'
    "\t\t\t(rectangle\n"
    "\t\t\t\t(start -1.016 -2.54)\n"
    "\t\t\t\t(end 1.016 2.54)\n"
    "\t\t\t\t(stroke (width 0.254) (type default))\n"
    "\t\t\t\t(fill (type none))))\n"
    '\t\t(symbol "R2_1_1"\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 3.81 270)\n"
    "\t\t\t\t(length 1.27)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))\n'
    "\t\t\t(pin passive line\n"
    "\t\t\t\t(at 0 -3.81 90)\n"
    "\t\t\t\t(length 1.27)\n"
    '\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))\n'
    '\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))))\n'
)
_EMBED = _R2.replace('(symbol "R2"', '(symbol "Fake:R2"', 1)


def _lib_text(body: str = _R2) -> str:
    return ("(kicad_symbol_lib\n\t(version 20231120)\n"
            '\t(generator "test")\n' + body + ")\n")


def _sch_text(embed: str, *, placed: str = "") -> str:
    return (
        "(kicad_sch\n\t(version 20231120)\n"
        '\t(generator "test")\n'
        '\t(uuid "00000000-0000-4000-8000-00000000abcd")\n'
        '\t(paper "A4")\n' + placed +
        "\t(lib_symbols\n" + embed + "\t)\n)\n"
    )


def _placed_instance(x_mm: float, y_mm: float) -> str:
    return (
        '\t(symbol\n\t\t(lib_id "Fake:R2")\n'
        f"\t\t(at {x_mm} {y_mm} 0)\n"
        '\t\t(uuid "00000000-0000-4000-8000-000000000001")\n'
        '\t\t(property "Reference" "R2" (at 0 0 0))\n'
        '\t\t(property "Value" "R2" (at 0 0 0))\n'
        "\t)\n"
    )


def test_libsync_stale_anchors_first_placed_instance(tmp_path: Path):
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(
        _sch_text(_EMBED, placed=_placed_instance(25.4, 50.8)), encoding="utf-8",
    )
    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / "Fake.kicad_sym").write_text(
        _lib_text().replace("(at 0 3.81 270)", "(at 0 2.54 270)"), encoding="utf-8",
    )
    findings = [f for f in libsync.run(sch, [libdir]) if f.code == "LIB_EMBED_STALE"]
    assert len(findings) == 1
    f = findings[0]
    # 25.4mm = 1000mil, 50.8mm = 2000mil (KiCad mm -> mil conversion)
    assert f.pos == (1000.0, 2000.0)
    assert f.anchors == [anchor("component", "Fake:R2", (1000.0, 2000.0))]


def test_libsync_stale_stays_positionless_without_a_placed_instance(tmp_path: Path):
    """No placed symbol of the lib_id exists in the sheet -- no anchor to give."""
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_sch_text(_EMBED), encoding="utf-8")
    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / "Fake.kicad_sym").write_text(
        _lib_text().replace("(at 0 3.81 270)", "(at 0 2.54 270)"), encoding="utf-8",
    )
    findings = [f for f in libsync.run(sch, [libdir]) if f.code == "LIB_EMBED_STALE"]
    assert len(findings) == 1
    f = findings[0]
    assert f.pos is None and f.anchors == []
    fj = json.loads(render([f], fmt="json"))["findings"][0]
    assert "pos" not in fj and "anchors" not in fj
