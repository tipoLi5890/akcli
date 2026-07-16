"""Differential-pair / bus continuity checks (checks/pairs.py).

The asymmetry IS the contract: a lone `_P`/`_H`/`+` side fires (broken pair),
a lone `_N`/`_L`/`-` never does (active-low naming convention). Bus families
flag internal index gaps only — a family starting at A8 is fine.
"""

from __future__ import annotations

from akcli.checks import pairs
from akcli.config import Config
from akcli.model import Net, Schematic


def _net(name: str, n_pins: int = 2, aliases: list[str] | None = None) -> Net:
    members = sorted((f"U{i}", str(i)) for i in range(1, n_pins + 1))
    return Net(name=name, members=members, aliases=list(aliases or []),
               source_names=[name], is_named=True)


def _sch(nets: list[Net]) -> Schematic:
    return Schematic(source_path="<test>", source_format="kicad",
                     components=[], nets=nets)


def _codes(findings) -> list[str]:
    return [f.code for f in findings]


# --- differential pairs ------------------------------------------------------
def test_lone_p_side_fires():
    out = pairs.run(_sch([_net("USB_P")]))
    assert _codes(out) == ["PAIR_INCOMPLETE"]
    assert "USB_N" in out[0].message


def test_lone_n_side_is_active_low_convention():
    out = pairs.run(_sch([_net("RESET_N"), _net("CS_N"), _net("EN_L")]))
    assert out == []


def test_complete_pair_is_silent():
    out = pairs.run(_sch([_net("USB_P"), _net("USB_N")]))
    assert out == []


def test_plus_minus_pair():
    assert _codes(pairs.run(_sch([_net("D+")]))) == ["PAIR_INCOMPLETE"]
    assert pairs.run(_sch([_net("D+"), _net("D-")])) == []


def test_can_h_l_pair():
    assert _codes(pairs.run(_sch([_net("CAN_H")]))) == ["PAIR_INCOMPLETE"]
    assert pairs.run(_sch([_net("CAN_H"), _net("CAN_L")])) == []


def test_pair_found_via_alias():
    out = pairs.run(_sch([_net("ETH_P"), _net("X", aliases=["ETH_N"])]))
    assert _codes(out) == []


def test_pin_count_mismatch_is_note():
    out = pairs.run(_sch([_net("LVDS_P", n_pins=3), _net("LVDS_N", n_pins=2)]))
    assert _codes(out) == ["PAIR_PIN_MISMATCH"]
    assert out[0].severity.value == "note"


def test_custom_pair_suffixes_config():
    cfg = Config(check={"pair_suffixes": [["_POS", "_NEG"]]})
    out = pairs.run(_sch([_net("SENSE_POS")]), cfg)
    assert _codes(out) == ["PAIR_INCOMPLETE"]
    # default suffixes are replaced, not extended: a lone _P no longer fires
    assert pairs.run(_sch([_net("USB_P")]), cfg) == []


# --- bus continuity ----------------------------------------------------------
def test_bus_gap_flagged():
    nets = [_net(f"D{i}") for i in (0, 1, 3, 4, 5, 6, 7)]
    out = pairs.run(_sch(nets))
    assert _codes(out) == ["BUS_GAP"]
    assert "D2" in out[0].message
    assert out[0].severity.value == "warning"  # family >= 4 members


def test_small_family_gap_is_note():
    out = pairs.run(_sch([_net("LED1"), _net("LED2"), _net("LED4")]))
    assert _codes(out) == ["BUS_GAP"]
    assert out[0].severity.value == "note"


def test_contiguous_and_offset_families_silent():
    assert pairs.run(_sch([_net(f"D{i}") for i in range(8)])) == []
    assert pairs.run(_sch([_net(f"A{i}") for i in range(8, 16)])) == []  # A8..A15


def test_single_member_family_silent():
    assert pairs.run(_sch([_net("3V3"), _net("5V0")])) == []


def test_bus_min_family_config():
    cfg = Config(check={"bus_min_family": 4})
    out = pairs.run(_sch([_net("LED1"), _net("LED2"), _net("LED4")]), cfg)
    assert out == []
