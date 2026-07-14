"""Tests for checks.pinmap (SPEC §3.6).

Schematics are built directly from ``model`` dataclasses (the netbuild tests set
the precedent: construct the normalized model without going through a reader),
plus one end-to-end test that parses a committed ``.SchDoc`` fixture via
``readers.altium_sch.read``. The ``expected`` cross-check table is supplied
in-line (standing in for a CSV/JSON the caller would load) -- this check never
parses a DTS/pinout itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import model
from akcli.checks import pinmap
from akcli.config import Config
from akcli.readers import altium_sch
from akcli.report import Severity

FIXTURES = Path(__file__).parent / "fixtures"


# --- helpers -----------------------------------------------------------------

def _pin(number, name=None):
    return model.Pin(number=number, name=name, x_mil=0.0, y_mil=0.0)


def _mcu(designator="U3", pins=()):
    return model.Component(
        designator=designator,
        library_ref="MCU",
        x_mil=0.0,
        y_mil=0.0,
        pins=list(pins),
    )


def _net(name, members, **kw):
    return model.Net(name=name, members=list(members), **kw)


def _sch(components, nets):
    return model.Schematic(
        source_path="synthetic",
        source_format="altium",
        components=list(components),
        nets=list(nets),
    )


def _cfg(mcu_designator="U3"):
    return Config(mcu_designator=mcu_designator)


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


# --- Pn.mm parser ------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("P0.25", (0, 25)),
        ("P1.10", (1, 10)),
        ("P0_25", (0, 25)),
        ("p0.25", (0, 25)),
        ("P0.25/AIN1", (0, 25)),
        ("  P2.3  ", (2, 3)),
    ],
)
def test_parse_pin_name_valid(name, expected):
    assert pinmap.parse_pin_name(name) == expected


@pytest.mark.parametrize("name", [None, "", "VDD", "GND", "STAT", "PA0", "XP0.25", "P0"])
def test_parse_pin_name_invalid(name):
    assert pinmap.parse_pin_name(name) is None


# --- generic emission (no expected table) -----------------------------------

def test_emits_pin_to_net_map():
    mcu = _mcu(pins=[_pin("1", "P0.25"), _pin("2", "P0.26")])
    sch = _sch(
        [mcu],
        [
            _net("STAT", [("U3", "1"), ("R7", "1")]),
            _net("LED_CTRL", [("U3", "2"), ("R8", "1")]),
        ],
    )
    findings = pinmap.run(sch, _cfg("U3"), None)

    maps = _by_code(findings, "PINMAP")
    assert len(maps) == 2
    assert all(f.severity is Severity.INFO for f in maps)
    msgs = " | ".join(f.message for f in maps)
    assert "U3.1 (P0.25) -> STAT" in msgs
    assert "U3.2 (P0.26) -> LED_CTRL" in msgs


def test_floating_mcu_pin_is_noted():
    mcu = _mcu(pins=[_pin("1", "P0.25"), _pin("2", "P0.26")])
    # pin 2 is on no net
    sch = _sch([mcu], [_net("STAT", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg("U3"), None)

    floats = _by_code(findings, "PINMAP_FLOATING")
    assert len(floats) == 1
    assert floats[0].severity is Severity.NOTE
    assert "U3.2" in floats[0].message


def test_pins_emitted_in_numeric_order():
    mcu = _mcu(pins=[_pin("10"), _pin("2"), _pin("1")])
    sch = _sch(
        [mcu],
        [
            _net("A", [("U3", "1")]),
            _net("B", [("U3", "2")]),
            _net("C", [("U3", "10")]),
        ],
    )
    findings = _by_code(pinmap.run(sch, _cfg("U3"), None), "PINMAP")
    order = [f.refs[0] for f in findings]
    assert order == ["U3.1", "U3.2", "U3.10"]


# --- no/invalid MCU ----------------------------------------------------------

def test_no_mcu_designator_configured():
    sch = _sch([_mcu()], [_net("A", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg(None), None)
    assert len(_by_code(findings, "PINMAP_NO_MCU")) == 1
    assert findings[0].severity is Severity.WARNING


def test_no_cfg_object_at_all():
    sch = _sch([_mcu()], [_net("A", [("U3", "1")])])
    findings = pinmap.run(sch, None, None)
    assert len(_by_code(findings, "PINMAP_NO_MCU")) == 1


def test_mcu_not_found():
    sch = _sch([_mcu("U3")], [_net("A", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg("U99"), None)
    nf = _by_code(findings, "PINMAP_MCU_NOT_FOUND")
    assert len(nf) == 1
    assert nf[0].severity is Severity.WARNING


def test_mcu_with_no_pins():
    sch = _sch([_mcu("U3", pins=[])], [])
    findings = pinmap.run(sch, _cfg("U3"), None)
    assert len(_by_code(findings, "PINMAP_NO_PINS")) == 1


# --- cross-check against expected table -------------------------------------

def test_cross_check_match_by_pin_name():
    mcu = _mcu(pins=[_pin("1", "P0.25"), _pin("2", "P0.26")])
    sch = _sch(
        [mcu],
        [
            _net("STAT", [("U3", "1")]),
            _net("LED_CTRL", [("U3", "2")]),
        ],
    )
    expected = {"P0.25": "STAT", "P0.26": "LED_CTRL"}
    findings = pinmap.run(sch, _cfg("U3"), expected)

    matches = _by_code(findings, "PINMAP_MATCH")
    assert len(matches) == 2
    assert all(f.severity is Severity.INFO for f in matches)
    assert _by_code(findings, "PINMAP_MISMATCH") == []


def test_cross_check_match_by_pin_number():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [_net("STAT", [("U3", "1")])])
    # expected keyed by the pin NUMBER, not name
    findings = pinmap.run(sch, _cfg("U3"), {"1": "STAT"})
    assert len(_by_code(findings, "PINMAP_MATCH")) == 1


def test_cross_check_underscore_name_variant_matches():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [_net("STAT", [("U3", "1")])])
    # expected uses the P0_25 spelling; canonicalization must still match.
    findings = pinmap.run(sch, _cfg("U3"), {"P0_25": "STAT"})
    assert len(_by_code(findings, "PINMAP_MATCH")) == 1


def test_cross_check_mismatch_is_advisory_warning():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [_net("STAT", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg("U3"), {"P0.25": "SOMETHING_ELSE"})

    mm = _by_code(findings, "PINMAP_MISMATCH")
    assert len(mm) == 1
    # schematic is authoritative -> WARNING, never higher
    assert mm[0].severity is Severity.WARNING
    assert "authoritative" in mm[0].message
    assert "STAT" in mm[0].message and "SOMETHING_ELSE" in mm[0].message


def test_cross_check_matches_net_alias():
    """STAT == LED1_GPIO_RD: expecting the alias still matches the net."""
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch(
        [mcu],
        [_net("STAT", [("U3", "1")], aliases=["LED1_GPIO_RD"])],
    )
    findings = pinmap.run(sch, _cfg("U3"), {"P0.25": "LED1_GPIO_RD"})
    assert len(_by_code(findings, "PINMAP_MATCH")) == 1
    assert _by_code(findings, "PINMAP_MISMATCH") == []


def test_cross_check_case_insensitive_signal():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [_net("Stat", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg("U3"), {"P0.25": "STAT"})
    assert len(_by_code(findings, "PINMAP_MATCH")) == 1


def test_cross_check_expected_pin_not_on_mcu():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [_net("STAT", [("U3", "1")])])
    findings = pinmap.run(sch, _cfg("U3"), {"P9.99": "MYSTERY"})
    miss = _by_code(findings, "PINMAP_EXPECTED_PIN_MISSING")
    assert len(miss) == 1
    assert miss[0].severity is Severity.WARNING


def test_cross_check_mismatch_when_pin_floating():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    sch = _sch([mcu], [])  # pin on no net
    findings = pinmap.run(sch, _cfg("U3"), {"P0.25": "STAT"})
    mm = _by_code(findings, "PINMAP_MISMATCH")
    assert len(mm) == 1
    assert "no net" in mm[0].message


def test_cross_check_unexpected_pin_noted():
    mcu = _mcu(pins=[_pin("1", "P0.25"), _pin("2", "P0.26")])
    sch = _sch(
        [mcu],
        [
            _net("STAT", [("U3", "1")]),
            _net("EXTRA", [("U3", "2")]),
        ],
    )
    # expected only covers pin 1
    findings = pinmap.run(sch, _cfg("U3"), {"P0.25": "STAT"})
    unexp = _by_code(findings, "PINMAP_UNEXPECTED")
    assert len(unexp) == 1
    assert "U3.2" in unexp[0].message
    assert unexp[0].severity is Severity.NOTE


def test_schematic_is_authoritative_does_not_mutate():
    mcu = _mcu(pins=[_pin("1", "P0.25")])
    net = _net("STAT", [("U3", "1")])
    sch = _sch([mcu], [net])
    pinmap.run(sch, _cfg("U3"), {"P0.25": "WRONG"})
    # the schematic net is untouched by the advisory check
    assert net.name == "STAT"
    assert net.members == [("U3", "1")]


# --- end-to-end on a committed binary fixture -------------------------------

def test_end_to_end_on_real_schdoc():
    sch = altium_sch.read(str(FIXTURES / "shared_name_label.SchDoc"))
    # U2 pin 1 (name STAT) is on the merged STAT net in this fixture.
    findings = pinmap.run(sch, _cfg("U2"), {"1": "STAT"})
    assert len(_by_code(findings, "PINMAP")) == 1
    assert len(_by_code(findings, "PINMAP_MATCH")) == 1
    assert _by_code(findings, "PINMAP_MISMATCH") == []
