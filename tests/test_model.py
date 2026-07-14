"""Tests for the LOCKED normalized data model (model.py)."""

from __future__ import annotations

from akcli.model import (
    ALTIUM_ELECTRICAL,
    KICAD_PINTYPE,
    SCHEMA_VERSION,
    Component,
    Library,
    Net,
    NetPrimitives,
    Pcb,
    Pin,
    PinType,
    Schematic,
    SymbolDef,
    to_json,
)


def test_pintype_values_are_canonical():
    assert PinType.POWER_IN.value == "power_in"
    assert PinType.NO_CONNECT.value == "no_connect"
    assert len(PinType) == 11


def test_altium_electrical_map_matches_spec():
    assert ALTIUM_ELECTRICAL[4] is PinType.PASSIVE
    assert ALTIUM_ELECTRICAL[7] is PinType.POWER_IN
    assert ALTIUM_ELECTRICAL[1] is PinType.BIDIRECTIONAL
    assert set(ALTIUM_ELECTRICAL) == set(range(8))


def test_kicad_pintype_map_covers_tokens():
    assert KICAD_PINTYPE["free"] is PinType.UNSPECIFIED
    assert KICAD_PINTYPE["power_out"] is PinType.POWER_OUT
    assert KICAD_PINTYPE["bidirectional"] is PinType.BIDIRECTIONAL


def test_net_stable_id_is_membership_derived_not_coordinate():
    n1 = Net(name="GND", members=[("U2", "1"), ("R7", "1")])
    n2 = Net(name="GND_RENAMED", members=[("R7", "1"), ("U2", "1")])
    # same membership (order of construction differs but lists equal) -> hash differs
    # because list order differs; identical sorted membership -> identical id.
    n3 = Net(name="X", members=[("U2", "1"), ("R7", "1")])
    assert n1.stable_id == n3.stable_id
    assert n1.stable_id.startswith("net_")
    assert len(n1.stable_id) == len("net_") + 12
    assert n1.stable_id != n2.stable_id  # different list order -> different id


def test_to_json_serializes_enum_and_stamps_schema_version():
    pin = Pin(number="2", name="P0.25", x_mil=100.0, y_mil=200.0,
              electrical_type=PinType.POWER_IN)
    comp = Component(designator="U3", library_ref="Device:R", x_mil=0, y_mil=0,
                     pins=[pin])
    net = Net(name="V3V3", members=[("U3", "2")])
    sch = Schematic(source_path="x.SchDoc", source_format="altium",
                    components=[comp], nets=[net])
    d = sch.export()
    assert d["schema_version"] == SCHEMA_VERSION == "1.1"
    assert d["components"][0]["pins"][0]["electrical_type"] == "power_in"
    # Net carries computed stable_id in JSON
    assert d["nets"][0]["stable_id"] == net.stable_id


def test_to_json_handles_tuples_and_nested():
    prims = NetPrimitives()
    prims.no_erc.append((1.0, 2.0))
    js = to_json(prims)
    assert js["no_erc"] == [[1.0, 2.0]]
    assert js["power_priority"] is False


def test_pcb_and_library_export_schema_version():
    pcb = Pcb(source_path="b.PcbDoc", source_format="altium", nets=["GND"],
              footprints=[])
    lib = Library(source_path="l.SchLib", source_format="altium",
                  symbols=[SymbolDef(name="R", lib_id=None, pins=[])])
    assert pcb.export()["schema_version"] == "1.1"
    assert lib.export()["schema_version"] == "1.1"


def test_component_defaults():
    c = Component(designator="$U1", library_ref=None, x_mil=0, y_mil=0)
    assert c.rotation == 0
    assert c.mirror == "none"
    assert c.part_count == 1
    assert c.undesignated is False
    assert c.pins == []
