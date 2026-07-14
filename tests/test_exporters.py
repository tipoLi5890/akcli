"""Tests for the netlist emitters (``exporters.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import exporters
from akcli.errors import AkcliError
from akcli.readers import altium_sch

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str):
    return altium_sch.read(str(FIXTURES / name))


def test_csv_has_header_and_rows():
    sch = _read("two_gnd_ports.SchDoc")
    text = exporters.to_csv(sch)
    lines = text.strip().splitlines()
    assert lines[0] == "net,ref,pin"
    # 4 components, each one pin, all on GND -> 4 membership rows
    assert lines[1:] == ["GND,U1,1", "GND,U2,1", "GND,U3,1", "GND,U4,1"]


def test_protel_has_component_and_net_blocks():
    sch = _read("shared_name_label.SchDoc")
    text = exporters.to_protel(sch)
    # one [ ] block per component (4) and one ( ) net block
    assert text.count("[\n") == 4
    assert text.count("]\n") == 4
    assert "(\nSTAT\n" in text
    # every member appears as DESIGNATOR-PIN
    for token in ("U2-1", "U3-2", "R7-1", "R12-1"):
        assert token in text
    assert text.endswith("\n")


def test_kicad_netlist_is_parseable_shape():
    sch = _read("two_gnd_ports.SchDoc")
    text = exporters.to_kicad_netlist(sch)
    assert text.startswith('(export (version "E")')
    assert "(components" in text
    assert "(nets" in text
    assert '(net (code "1") (name "GND")' in text
    assert '(node (ref "U1") (pin "1"))' in text
    # balanced parens
    assert text.count("(") == text.count(")")


def test_unnamed_net_uses_stable_id():
    # junction_cross has a single UNNAMED net -> emitted under its stable_id
    sch = _read("junction_cross.SchDoc")
    assert all(n.name is None for n in sch.nets)
    sid = sch.nets[0].stable_id
    assert sid.startswith("net_")
    for emitter in (exporters.to_csv, exporters.to_protel, exporters.to_kicad_netlist):
        assert sid in emitter(sch)


def test_export_netlist_dispatch():
    sch = _read("two_gnd_ports.SchDoc")
    assert exporters.export_netlist(sch, "csv") == exporters.to_csv(sch)
    assert exporters.export_netlist(sch, "protel") == exporters.to_protel(sch)
    assert exporters.export_netlist(sch, "kicad") == exporters.to_kicad_netlist(sch)


def test_export_netlist_bad_format_raises():
    sch = _read("two_gnd_ports.SchDoc")
    with pytest.raises(AkcliError) as exc:
        exporters.export_netlist(sch, "nonsense")
    assert exc.value.code == "BAD_CONFIG"


def test_deterministic_output():
    sch = _read("shared_name_label.SchDoc")
    assert exporters.to_csv(sch) == exporters.to_csv(_read("shared_name_label.SchDoc"))
    assert exporters.to_kicad_netlist(sch) == exporters.to_kicad_netlist(
        _read("shared_name_label.SchDoc")
    )
