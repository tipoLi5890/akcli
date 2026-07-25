"""Power-check rules that turn on component *presence*: DNP handling and the
sourceless-rail rule (``POWER_RAIL_NO_SOURCE``).

Synthetic ``Schematic`` objects, same discipline as test_erc.py: one fixture
per decision the rule makes, plus the "stays quiet" negatives that keep the
generous source heuristics honest.
"""

from __future__ import annotations

from akcli.checks import power
from akcli.config import Config
from akcli.model import Component, Net, Pin, PinType, Schematic


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pin(number: str, *, etype: PinType = PinType.PASSIVE) -> Pin:
    return Pin(number=number, name=None, x_mil=0.0, y_mil=0.0,
               electrical_type=etype)


def _comp(designator, pins, *, lib="Device:R", value=None, dnp=False) -> Component:
    return Component(designator=designator, library_ref=lib, x_mil=0.0,
                     y_mil=0.0, value=value, pins=list(pins), dnp=dnp)


def _net(name, members) -> Net:
    return Net(name=name, members=sorted(members),
               source_names=[name] if name else [], is_named=name is not None)


def _sch(components, nets) -> Schematic:
    return Schematic(source_path="<test>", source_format="kicad",
                     components=list(components), nets=list(nets))


def _cfg(*, rails=None) -> Config:
    return Config(rails=list(rails or []))


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


# ---------------------------------------------------------------------------
# DNP bypass caps do not decouple a rail
# ---------------------------------------------------------------------------
def _decoupling_sch(*, dnp: bool):
    """U1 on 3V3 with one bypass cap C1 to GND (optionally DNP)."""
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    c1 = _comp("C1", [_pin("1"), _pin("2")], lib="Device:C", value="100n",
               dnp=dnp)
    j1 = _comp("J1", [_pin("1")], lib="Connector:Conn_01x01")
    return _sch(
        [u1, c1, j1],
        [_net("3V3", [("U1", "1"), ("C1", "1"), ("J1", "1")]),
         _net("GND", [("U1", "2"), ("C1", "2")])],
    )


def test_fitted_bypass_cap_decouples_the_rail():
    findings = power.run(_decoupling_sch(dnp=False), _cfg())
    assert _by_code(findings, power.POWER_NO_DECOUPLING) == []


def test_dnp_bypass_cap_does_not_decouple_the_rail():
    findings = power.run(_decoupling_sch(dnp=True), _cfg())
    hits = _by_code(findings, power.POWER_NO_DECOUPLING)
    assert len(hits) == 1 and "U1" in hits[0].refs


# ---------------------------------------------------------------------------
# POWER_RAIL_NO_SOURCE
# ---------------------------------------------------------------------------
def _sourceless_sch(extra_comps=(), extra_members=(), rail="3V3"):
    """Rail with two consumers (U1 + a bypass cap) and no source at all."""
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    c1 = _comp("C1", [_pin("1"), _pin("2")], lib="Device:C", value="100n")
    return _sch(
        [u1, c1, *extra_comps],
        [_net(rail, [("U1", "1"), ("C1", "1"), *extra_members]),
         _net("GND", [("U1", "2"), ("C1", "2")])],
    )


def test_rail_with_consumers_and_no_source_fires():
    hits = _by_code(power.run(_sourceless_sch(), _cfg()),
                    power.POWER_RAIL_NO_SOURCE)
    assert len(hits) == 1
    assert hits[0].severity.value == "warning"
    assert hits[0].refs[0] == "3V3" and "U1" in hits[0].refs


def test_single_consumer_rail_is_not_reported():
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    sch = _sch([u1], [_net("3V3", [("U1", "1")]), _net("GND", [("U1", "2")])])
    assert _by_code(power.run(sch, _cfg()), power.POWER_RAIL_NO_SOURCE) == []


def test_pwr_flag_is_a_source():
    flag = _comp("#FLG01", [_pin("1")], lib="power:PWR_FLAG")
    hits = _by_code(
        power.run(_sourceless_sch([flag], [("#FLG01", "1")]), _cfg()),
        power.POWER_RAIL_NO_SOURCE)
    assert hits == []


def test_connector_is_a_source():
    j1 = _comp("J1", [_pin("1")], lib="Connector:Conn_01x01")
    hits = _by_code(
        power.run(_sourceless_sch([j1], [("J1", "1")]), _cfg()),
        power.POWER_RAIL_NO_SOURCE)
    assert hits == []


def test_test_point_is_a_source():
    tp = _comp("TP1", [_pin("1")], lib="Connector:TestPoint")
    hits = _by_code(
        power.run(_sourceless_sch([tp], [("TP1", "1")]), _cfg()),
        power.POWER_RAIL_NO_SOURCE)
    assert hits == []


def test_power_out_pin_is_a_source():
    reg = _comp("U9", [_pin("1", etype=PinType.POWER_OUT), _pin("2"), _pin("3")],
                lib="Regulator:LDO")
    hits = _by_code(
        power.run(_sourceless_sch([reg], [("U9", "1")]), _cfg()),
        power.POWER_RAIL_NO_SOURCE)
    assert hits == []


def test_output_pin_is_a_source():
    drv = _comp("U9", [_pin("1", etype=PinType.OUTPUT), _pin("2"), _pin("3")],
                lib="Driver:SW")
    hits = _by_code(
        power.run(_sourceless_sch([drv], [("U9", "1")]), _cfg()),
        power.POWER_RAIL_NO_SOURCE)
    assert hits == []


def test_configured_rail_is_intentional_and_silent():
    cfg = _cfg(rails=[{"name": "3V3", "voltage": 3.3}])
    assert _by_code(power.run(_sourceless_sch(), cfg),
                    power.POWER_RAIL_NO_SOURCE) == []


def test_entry_named_rail_is_silent():
    # VBUS/VBAT/VIN arrive from off-board; a bare label with no symbol is the
    # idiomatic way to draw that, so an entry-named rail is never "sourceless".
    for rail in ("VBUS", "VBAT", "VIN", "VBUS_5V"):
        hits = _by_code(power.run(_sourceless_sch(rail=rail), _cfg()),
                        power.POWER_RAIL_NO_SOURCE)
        assert hits == [], rail


def test_rail_fed_through_a_series_part_inherits_its_source():
    # J1 -> VIN -> L1 -> 3V3: the source lives one series element upstream.
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    c1 = _comp("C1", [_pin("1"), _pin("2")], lib="Device:C", value="100n")
    l1 = _comp("L1", [_pin("1"), _pin("2")], lib="Device:L", value="10u")
    j1 = _comp("J1", [_pin("1")], lib="Connector:Conn_01x01")
    sch = _sch(
        [u1, c1, l1, j1],
        [_net("3V3", [("U1", "1"), ("C1", "1"), ("L1", "2")]),
         _net("SUPPLY_A", [("L1", "1"), ("J1", "1")]),
         _net("GND", [("U1", "2"), ("C1", "2")])],
    )
    assert _by_code(power.run(sch, _cfg()), power.POWER_RAIL_NO_SOURCE) == []


def test_dnp_series_part_does_not_carry_the_source_through():
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    c1 = _comp("C1", [_pin("1"), _pin("2")], lib="Device:C", value="100n")
    l1 = _comp("L1", [_pin("1"), _pin("2")], lib="Device:L", value="10u",
               dnp=True)
    j1 = _comp("J1", [_pin("1")], lib="Connector:Conn_01x01")
    sch = _sch(
        [u1, c1, l1, j1],
        [_net("3V3", [("U1", "1"), ("C1", "1"), ("L1", "2")]),
         _net("SUPPLY_A", [("L1", "1"), ("J1", "1")]),
         _net("GND", [("U1", "2"), ("C1", "2")])],
    )
    hits = _by_code(power.run(sch, _cfg()), power.POWER_RAIL_NO_SOURCE)
    assert len(hits) == 1 and hits[0].refs[0] == "3V3"


def test_ground_is_never_crossed_when_hunting_for_a_source():
    # A connector sitting on GND must not "source" every rail on the board.
    j1 = _comp("J1", [_pin("1")], lib="Connector:Conn_01x01")
    u1 = _comp("U1", [_pin("1"), _pin("2"), _pin("3")], lib="MCU:U")
    c1 = _comp("C1", [_pin("1"), _pin("2")], lib="Device:C", value="100n")
    sch = _sch(
        [u1, c1, j1],
        [_net("3V3", [("U1", "1"), ("C1", "1")]),
         _net("GND", [("U1", "2"), ("C1", "2"), ("J1", "1")])],
    )
    hits = _by_code(power.run(sch, _cfg()), power.POWER_RAIL_NO_SOURCE)
    assert len(hits) == 1 and hits[0].refs[0] == "3V3"


def test_corpus_boards_stay_quiet():
    """The committed corpus boards are real designs: the rule must not fire."""
    from pathlib import Path

    from akcli.readers import kicad

    corpus = Path(__file__).parent / "fixtures" / "corpus"
    for board in sorted(corpus.glob("*.kicad_sch")):
        sch = kicad.read_sch(str(board))
        hits = _by_code(power.run(sch, _cfg()), power.POWER_RAIL_NO_SOURCE)
        assert hits == [], (board.name, [f.message for f in hits])
