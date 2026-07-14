"""Tests for the Altium ``.SchDoc`` -> ``model.Schematic`` reader.

Asserts the committed net-regression fixtures parse to the SAME nets as their
hand-authored ``*.expected.json`` (membership-based, coordinate-independent), and
that component / pin / primitive extraction works on the demo container.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.model import PinType
from akcli.readers import altium_sch

FIX = Path(__file__).resolve().parent / "fixtures"

_NET_FIXTURES = [
    "shared_name_label",
    "junction_cross",
    "t_junction",
    "no_erc",
    "two_gnd_ports",
]


def _norm_expected(nets: list[dict]) -> set[tuple]:
    return {
        (n["name"], bool(n["is_named"]), tuple(tuple(m) for m in n["members"]))
        for n in nets
    }


def _norm_model(nets) -> set[tuple]:
    return {
        (n.name, bool(n.is_named), tuple(tuple(m) for m in n.members))
        for n in nets
    }


@pytest.mark.parametrize("name", _NET_FIXTURES)
def test_schdoc_nets_match_hand_authored_expected(name):
    expected = json.loads((FIX / f"{name}.expected.json").read_text(encoding="utf-8"))
    sch = altium_sch.read(FIX / f"{name}.SchDoc")
    assert _norm_model(sch.nets) == _norm_expected(expected["nets"])


@pytest.mark.parametrize("name", _NET_FIXTURES)
def test_schdoc_single_pin_net_count(name):
    expected = json.loads((FIX / f"{name}.expected.json").read_text(encoding="utf-8"))
    sch = altium_sch.read(FIX / f"{name}.SchDoc")
    single = sum(1 for n in sch.nets if len(n.members) == 1)
    assert single == expected["single_pin_net_count"]


def test_no_erc_points_extracted():
    sch = altium_sch.read(FIX / "no_erc.SchDoc")
    # one No-ERC marker in the fixture; coordinates are canonical mils (Y negated).
    assert len(sch.no_erc_points) == 1


def test_shared_name_label_merges_into_one_net():
    sch = altium_sch.read(FIX / "shared_name_label.SchDoc")
    named = [n for n in sch.nets if n.name == "STAT"]
    assert len(named) == 1
    assert sorted(named[0].members) == [("R12", "1"), ("R7", "1"), ("U2", "1"), ("U3", "2")]


def test_two_gnd_ports_collapse_to_one():
    sch = altium_sch.read(FIX / "two_gnd_ports.SchDoc")
    gnd = [n for n in sch.nets if n.name == "GND"]
    assert len(gnd) == 1
    assert len(gnd[0].members) == 4


# --- component / pin extraction on the demo container -----------------------
def test_demo_components_and_pins():
    sch = altium_sch.read(FIX / "ole_minifat.SchDoc")
    by_des = {c.designator: c for c in sch.components}
    assert set(by_des) == {"U1", "U2"}
    assert by_des["U1"].library_ref == "RES"
    assert by_des["U2"].library_ref == "CAP"
    # one pin each
    assert [p.number for p in by_des["U1"].pins] == ["1"]
    assert by_des["U1"].pins[0].name == "P0.25"
    # U2 pin is Electrical=7 -> POWER_IN; U1 pin is Electrical=4 -> PASSIVE
    assert by_des["U1"].pins[0].electrical_type is PinType.PASSIVE
    assert by_des["U2"].pins[0].electrical_type is PinType.POWER_IN


def test_demo_net_v3v3():
    sch = altium_sch.read(FIX / "ole_minifat.SchDoc")
    v = [n for n in sch.nets if n.name == "V3V3"]
    assert len(v) == 1
    assert sorted(v[0].members) == [("U1", "1"), ("U2", "1")]


def test_minifat_and_fatchain_yield_identical_nets():
    a = altium_sch.read(FIX / "ole_minifat.SchDoc")
    b = altium_sch.read(FIX / "ole_fatchain.SchDoc")
    assert _norm_model(a.nets) == _norm_model(b.nets)


def test_schematic_is_canonical_y_down():
    # The demo pins sit on a horizontal wire at Altium Y=1000 -> canonical Y=-10000 mil.
    sch = altium_sch.read(FIX / "ole_minifat.SchDoc")
    ys = {p.y_mil for c in sch.components for p in c.pins}
    assert ys == {-10000.0}


def test_metadata_present():
    sch = altium_sch.read(FIX / "ole_minifat.SchDoc")
    assert sch.metadata["component_count"] == 2
    assert sch.metadata["pin_count"] == 2
    assert sch.source_format == "altium"
    assert sch.export()["schema_version"] == "1.1"


def test_read_primitives_round_trips_into_same_nets():
    from akcli import netbuild

    prims = altium_sch.read_primitives(FIX / "shared_name_label.SchDoc")
    nets = netbuild.build_nets(prims)
    sch = altium_sch.read(FIX / "shared_name_label.SchDoc")
    assert _norm_model(nets) == _norm_model(sch.nets)
