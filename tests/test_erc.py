"""Tests for the electrical-rule check (ERC) (SPEC §3.6).

Unit-tested with synthetic ``Schematic``/``Net``/``Component``/``Pin`` objects
covering every rule, plus a smoke run against committed ``*.SchDoc`` fixtures
read through the frozen Altium reader. Assertion style mirrors test_bom.py.
"""

from __future__ import annotations

import os

import pytest

from akcli.checks import erc
from akcli.config import Config
from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.readers import altium_sch
from akcli.report import Finding, Severity

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pin(
    number: str,
    *,
    name: str | None = None,
    etype: PinType = PinType.PASSIVE,
    x: float = 0.0,
    y: float = 0.0,
) -> Pin:
    return Pin(number=number, name=name, x_mil=x, y_mil=y, electrical_type=etype)


def _comp(designator: str, pins: list[Pin], *, undesignated: bool = False) -> Component:
    return Component(
        designator=designator,
        library_ref="Device:U",
        x_mil=0.0,
        y_mil=0.0,
        pins=pins,
        undesignated=undesignated,
    )


def _net(name, members, *, aliases=None, source_names=None) -> Net:
    return Net(
        name=name,
        members=sorted(members),
        aliases=list(aliases or []),
        source_names=list(source_names or ([name] if name else [])),
        is_named=name is not None,
    )


def _sch(
    components: list[Component],
    nets: list[Net],
    *,
    no_erc_points=None,
    source_format: str = "altium",
) -> Schematic:
    return Schematic(
        source_path="<test>",
        source_format=source_format,
        components=components,
        nets=nets,
        no_erc_points=list(no_erc_points or []),
    )


def _cfg(*, rails=None, waivers=None) -> Config:
    return Config(rails=list(rails or []), erc_waivers=list(waivers or []))


def _codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]


def _by_code(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


# ---------------------------------------------------------------------------
# return contract
# ---------------------------------------------------------------------------
def test_returns_list_of_findings():
    sch = _sch([_comp("U1", [_pin("1")])], [_net("STAT", [("U1", "1")])])
    out = erc.run(sch, _cfg())
    assert isinstance(out, list)
    assert all(isinstance(f, Finding) for f in out)


def test_runs_with_cfg_none():
    sch = _sch([_comp("U1", [_pin("1")])], [_net(None, [("U1", "1")])])
    assert isinstance(erc.run(sch, None), list)


# ---------------------------------------------------------------------------
# dangling single-pin net
# ---------------------------------------------------------------------------
def test_single_pin_net_is_dangling():
    sch = _sch([_comp("U1", [_pin("1")])], [_net(None, [("U1", "1")])])
    dang = _by_code(erc.run(sch, _cfg()), erc.ERC_DANGLING_NET)
    assert len(dang) == 1
    assert dang[0].refs == ["U1.1"]
    assert dang[0].severity is Severity.WARNING


def test_multi_pin_net_is_not_dangling():
    sch = _sch(
        [_comp("U1", [_pin("1")]), _comp("U2", [_pin("1")])],
        [_net("NET1", [("U1", "1"), ("U2", "1")])],
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_DANGLING_NET) == []


def test_no_connect_pin_is_not_dangling():
    # An explicit NO_CONNECT pin is intentionally unconnected -> not flagged.
    sch = _sch([_comp("U1", [_pin("1", etype=PinType.NO_CONNECT)])],
               [_net(None, [("U1", "1")])])
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_DANGLING_NET) == []


# ---------------------------------------------------------------------------
# No-ERC suppression (geo-match the pin tip within grid tolerance)
# ---------------------------------------------------------------------------
def test_no_erc_marker_suppresses_dangling():
    pin = _pin("1", x=1000.0, y=2000.0)
    sch = _sch([_comp("U1", [pin])], [_net(None, [("U1", "1")])],
               no_erc_points=[(1000.0, 2000.0)])
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_DANGLING_NET) == []


def test_no_erc_marker_elsewhere_does_not_suppress():
    pin = _pin("1", x=1000.0, y=2000.0)
    sch = _sch([_comp("U1", [pin])], [_net(None, [("U1", "1")])],
               no_erc_points=[(5000.0, 9000.0)])  # far from the pin tip
    assert len(_by_code(erc.run(sch, _cfg()), erc.ERC_DANGLING_NET)) == 1


# ---------------------------------------------------------------------------
# driver conflict (TYPE-gated by confidence)
# ---------------------------------------------------------------------------
def test_driver_conflict_high_confidence_is_warning():
    # Two OUTPUT pins on one net, all pins typed -> confidence 1.0 -> WARNING.
    sch = _sch(
        [
            _comp("U1", [_pin("1", etype=PinType.OUTPUT)]),
            _comp("U2", [_pin("1", etype=PinType.OUTPUT)]),
        ],
        [_net("BUS", [("U1", "1"), ("U2", "1")])],
    )
    conf = _by_code(erc.run(sch, _cfg()), erc.ERC_DRIVER_CONFLICT)
    assert len(conf) == 1
    assert conf[0].severity is Severity.WARNING
    assert {"U1.1", "U2.1"} <= set(conf[0].refs)


def test_driver_conflict_low_confidence_is_downgraded_to_note():
    # Two OUTPUT pins drowned by many Passive pins -> confidence < 0.2 -> NOTE.
    extra = _comp("R1", [_pin(str(i)) for i in range(1, 10)])  # 9 passive pins
    sch = _sch(
        [
            _comp("U1", [_pin("1", etype=PinType.OUTPUT)]),
            _comp("U2", [_pin("1", etype=PinType.OUTPUT)]),
            extra,
        ],
        [_net("BUS", [("U1", "1"), ("U2", "1")])],
    )
    conf = _by_code(erc.run(sch, _cfg()), erc.ERC_DRIVER_CONFLICT)
    assert len(conf) == 1
    assert conf[0].severity is Severity.NOTE
    assert "type-confidence low" in conf[0].message


def test_two_bidirectional_pins_are_not_a_conflict():
    # Bidirectional (bus) pins may legitimately share a net.
    sch = _sch(
        [
            _comp("U1", [_pin("1", etype=PinType.BIDIRECTIONAL)]),
            _comp("U2", [_pin("1", etype=PinType.BIDIRECTIONAL)]),
        ],
        [_net("SDA", [("U1", "1"), ("U2", "1")])],
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_DRIVER_CONFLICT) == []


def test_driver_conflict_waiver_suppresses():
    sch = _sch(
        [
            _comp("U1", [_pin("1", etype=PinType.OUTPUT)]),
            _comp("U2", [_pin("1", etype=PinType.OUTPUT)]),
        ],
        [_net("LED1_GPIO_RD", [("U1", "1"), ("U2", "1")])],
    )
    cfg = _cfg(waivers=[{"net": "LED1_GPIO_RD", "rule": "driver_conflict",
                         "reason": "shared open-drain STAT by design"}])
    assert _by_code(erc.run(sch, cfg), erc.ERC_DRIVER_CONFLICT) == []
    # without the waiver it fires
    assert len(_by_code(erc.run(sch, _cfg()), erc.ERC_DRIVER_CONFLICT)) == 1


# ---------------------------------------------------------------------------
# floating input (TYPE-gated by confidence)
# ---------------------------------------------------------------------------
def test_floating_input_high_confidence_is_warning():
    # INPUT + PASSIVE, no driver -> floating. confidence 0.5 -> WARNING.
    sch = _sch(
        [_comp("U1", [_pin("1", name="P0.25", etype=PinType.INPUT)]),
         _comp("R1", [_pin("1")])],
        [_net("SIG", [("U1", "1"), ("R1", "1")])],
    )
    fl = _by_code(erc.run(sch, _cfg()), erc.ERC_FLOATING_INPUT)
    assert len(fl) == 1
    assert fl[0].refs == ["U1.1"]
    assert fl[0].severity is Severity.WARNING


def test_floating_input_low_confidence_is_downgraded_to_note():
    extra = _comp("R2", [_pin(str(i)) for i in range(1, 9)])  # dilute confidence
    sch = _sch(
        [_comp("U1", [_pin("1", etype=PinType.INPUT)]),
         _comp("R1", [_pin("1")]), extra],
        [_net("SIG", [("U1", "1"), ("R1", "1")])],
    )
    fl = _by_code(erc.run(sch, _cfg()), erc.ERC_FLOATING_INPUT)
    assert len(fl) == 1
    assert fl[0].severity is Severity.NOTE


def test_input_with_driver_is_not_floating():
    sch = _sch(
        [_comp("U1", [_pin("1", etype=PinType.INPUT)]),
         _comp("U2", [_pin("1", etype=PinType.OUTPUT)])],
        [_net("SIG", [("U1", "1"), ("U2", "1")])],
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_FLOATING_INPUT) == []


def test_input_on_power_rail_is_not_floating():
    # An INPUT tied to a detected rail is driven by the rail, not floating.
    sch = _sch(
        [_comp("U1", [_pin("1", etype=PinType.INPUT)]),
         _comp("U2", [_pin("1")])],
        [_net("GND", [("U1", "1"), ("U2", "1")], source_names=["GND"])],
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_FLOATING_INPUT) == []


# ---------------------------------------------------------------------------
# IC power / ground by NET IDENTITY (robust; not type-gated)
# ---------------------------------------------------------------------------
def _powered_board() -> Schematic:
    # GND {U1.1, R1.1}; V3V3 {U1.2, U2.1}. U1 has both rails; U2 only power.
    # ICs carry >= 3 pins: parts below that are treated as header/jumper stubs
    # and skipped by the per-IC power/ground check.
    return _sch(
        [
            _comp("U1", [_pin("1"), _pin("2"), _pin("3")]),
            _comp("U2", [_pin("1"), _pin("2"), _pin("3")]),
            _comp("R1", [_pin("1")]),
        ],
        [
            _net("GND", [("U1", "1"), ("R1", "1")], source_names=["GND"]),
            _net("V3V3", [("U1", "2"), ("U2", "1")], source_names=["V3V3"]),
        ],
    )


def test_ic_with_power_and_ground_is_clean():
    out = erc.run(_powered_board(), _cfg())
    assert not any(f.refs == ["U1"] for f in out
                   if f.code in (erc.ERC_NO_POWER, erc.ERC_NO_GROUND))


def test_ic_missing_ground_flagged_by_identity():
    out = erc.run(_powered_board(), _cfg())
    ng = _by_code(out, erc.ERC_NO_GROUND)
    assert any(f.refs == ["U2"] for f in ng)          # U2 touches no ground net
    assert _by_code(out, erc.ERC_NO_POWER) == []       # U2 touches power -> ok


def test_two_pin_u_part_is_not_an_ic():
    # A "U"-designated 2-pin part (e.g. a buzzer or header U9) must not be flagged
    # for missing power/ground -- it is a header/jumper stub, not an IC.
    sch = _sch(
        [
            _comp("U1", [_pin("1"), _pin("2"), _pin("3")]),
            _comp("U9", [_pin("1"), _pin("2")]),
        ],
        [
            _net("GND", [("U1", "1")], source_names=["GND"]),
            _net("V3V3", [("U1", "2")], source_names=["V3V3"]),
            _net("OSC_OUT", [("U9", "1")]),
        ],
    )
    out = erc.run(sch, _cfg())
    assert not any(
        f.refs == ["U9"] for f in out
        if f.code in (erc.ERC_NO_POWER, erc.ERC_NO_GROUND)
    )


def test_no_power_or_ground_infra_skips_per_ic_checks():
    # No GND/power net anywhere -> do not spam every IC (vacuous-board guard).
    sch = _sch(
        [_comp("U1", [_pin("1")]), _comp("U2", [_pin("1")])],
        [_net("STAT", [("U1", "1"), ("U2", "1")])],
    )
    out = erc.run(sch, _cfg())
    assert _by_code(out, erc.ERC_NO_POWER) == []
    assert _by_code(out, erc.ERC_NO_GROUND) == []


def test_config_rail_name_detected_as_power():
    # "MIDRAIL" matches no built-in pattern; config [[rail]] makes it power.
    sch = _sch(
        [_comp("U1", [_pin("1"), _pin("2")]), _comp("R1", [_pin("1")])],
        [
            _net("GND", [("U1", "1"), ("R1", "1")], source_names=["GND"]),
            _net("MIDRAIL", [("U1", "2"), ("R1", "1")], source_names=["MIDRAIL"]),
        ],
    )
    cfg = _cfg(rails=[{"name": "MIDRAIL", "voltage": 1.65}])
    out = erc.run(sch, cfg)
    # U1 now touches both a (config) power net and ground -> not flagged.
    assert not any(f.refs == ["U1"] for f in out
                   if f.code in (erc.ERC_NO_POWER, erc.ERC_NO_GROUND))


def test_no_power_waiver_suppresses():
    # U2 lacks ground; a no_ground waiver keyed by its designator silences it.
    cfg = _cfg(waivers=[{"net": "U2", "rule": "no_ground",
                         "reason": "analog-only part, no ground pin"}])
    assert _by_code(erc.run(_powered_board(), cfg), erc.ERC_NO_GROUND) == []


# ---------------------------------------------------------------------------
# net-alias conflict -> NOTE (never an error)
# ---------------------------------------------------------------------------
def test_net_alias_is_a_note():
    net = _net(
        "STAT",
        [("U1", "1"), ("U2", "1")],
        aliases=["LED1_GPIO_RD"],
        source_names=["STAT", "LED1_GPIO_RD"],
    )
    sch = _sch(
        [_comp("U1", [_pin("1")]), _comp("U2", [_pin("1")])],
        [net],
    )
    alias = _by_code(erc.run(sch, _cfg()), erc.ERC_NET_ALIAS)
    assert len(alias) == 1
    assert alias[0].severity is Severity.NOTE
    assert "STAT" in alias[0].message and "LED1_GPIO_RD" in alias[0].message


def test_single_name_net_has_no_alias_note():
    sch = _sch(
        [_comp("U1", [_pin("1")]), _comp("U2", [_pin("1")])],
        [_net("STAT", [("U1", "1"), ("U2", "1")])],
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_NET_ALIAS) == []


def test_net_alias_waiver_suppresses():
    net = _net("STAT", [("U1", "1"), ("U2", "1")],
               aliases=["LED1_GPIO_RD"], source_names=["STAT", "LED1_GPIO_RD"])
    sch = _sch([_comp("U1", [_pin("1")]), _comp("U2", [_pin("1")])], [net])
    cfg = _cfg(waivers=[{"net": "STAT", "rule": "net_alias", "reason": "ok"}])
    assert _by_code(erc.run(sch, cfg), erc.ERC_NET_ALIAS) == []


# ---------------------------------------------------------------------------
# unplaced units of multi-unit parts (KiCad-sourced only)
# ---------------------------------------------------------------------------
def _multi_unit_comp(
    designator: str,
    part_count: int,
    placed_units: list[int],
    *,
    common_pins: int = 0,
    undesignated: bool = False,
) -> Component:
    # Mirror the KiCad reader's merge: only placed units contribute pins,
    # each pin tagged with its owning unit (0 = common to all units).
    pins = [
        Pin(number=f"{u}{i}", name=None, x_mil=0.0, y_mil=0.0, owner_part_id=u)
        for u in placed_units
        for i in (1, 2)
    ]
    pins += [
        Pin(number=f"c{i}", name=None, x_mil=0.0, y_mil=0.0, owner_part_id=0)
        for i in range(common_pins)
    ]
    return Component(
        designator=designator,
        library_ref="Amplifier:LM324",
        x_mil=0.0,
        y_mil=0.0,
        part_count=part_count,
        pins=pins,
        undesignated=undesignated,
    )


def test_unplaced_unit_flagged_with_macro_hint():
    sch = _sch([_multi_unit_comp("U1", 4, [1, 3])], [], source_format="kicad")
    out = _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT)
    assert len(out) == 1
    assert out[0].severity is Severity.WARNING
    assert out[0].refs == ["U1"]
    # names the missing units by KiCad letter and points at the macro
    assert "B, D" in out[0].message
    assert "terminate_unused_unit" in out[0].message


def test_all_units_placed_is_clean():
    sch = _sch([_multi_unit_comp("U1", 2, [1, 2])], [], source_format="kicad")
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT) == []


def test_common_unit0_pins_do_not_count_as_placed():
    # A part whose only extra pins are _0_* common ones still misses unit B.
    sch = _sch(
        [_multi_unit_comp("U1", 2, [1], common_pins=2)], [], source_format="kicad"
    )
    out = _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT)
    assert len(out) == 1 and "B" in out[0].message


def test_single_unit_part_never_flagged():
    sch = _sch([_multi_unit_comp("U1", 1, [1])], [], source_format="kicad")
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT) == []


def test_unplaced_unit_silent_for_altium_sources():
    # Altium attaches every unit's pins regardless of placement and PARTCOUNT
    # is unreliable -> the rule must stay silent for altium-format schematics.
    sch = _sch([_multi_unit_comp("U1", 4, [1])], [], source_format="altium")
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT) == []


def test_unplaced_unit_skips_undesignated():
    sch = _sch(
        [_multi_unit_comp("$U0", 2, [1], undesignated=True)], [],
        source_format="kicad",
    )
    assert _by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT) == []


def test_unplaced_unit_waiver_suppresses():
    sch = _sch([_multi_unit_comp("U1", 2, [1])], [], source_format="kicad")
    cfg = _cfg(waivers=[{"net": "U1", "rule": "unplaced_unit",
                         "reason": "spare gate intentionally unplaced"}])
    assert _by_code(erc.run(sch, cfg), erc.ERC_UNPLACED_UNIT) == []
    assert len(_by_code(erc.run(sch, _cfg()), erc.ERC_UNPLACED_UNIT)) == 1


# ---------------------------------------------------------------------------
# real-fixture smoke tests (parsed via the frozen Altium reader)
# ---------------------------------------------------------------------------
def test_real_fixture_runs():
    sch = altium_sch.read(os.path.join(FIXTURES, "shared_name_label.SchDoc"))
    out = erc.run(sch, _cfg())
    assert all(isinstance(f, Finding) for f in out)
    # All four pins are Passive -> the type-based rules must stay silent.
    assert _by_code(out, erc.ERC_DRIVER_CONFLICT) == []
    assert _by_code(out, erc.ERC_FLOATING_INPUT) == []


def test_real_fixture_no_erc_suppresses_dangling():
    # no_erc.SchDoc: U1.2 is an open pin carrying a RECORD-22 No-ERC marker.
    sch = altium_sch.read(os.path.join(FIXTURES, "no_erc.SchDoc"))
    assert sch.no_erc_points  # reader surfaced the marker
    out = erc.run(sch, _cfg())
    # the lone single-pin net (U1.2) is suppressed by the No-ERC marker
    assert _by_code(out, erc.ERC_DANGLING_NET) == []


@pytest.mark.parametrize(
    "fixture",
    [
        "shared_name_label.SchDoc",
        "junction_cross.SchDoc",
        "no_erc.SchDoc",
        "t_junction.SchDoc",
        "two_gnd_ports.SchDoc",
    ],
)
def test_all_fixtures_run_without_error(fixture):
    sch = altium_sch.read(os.path.join(FIXTURES, fixture))
    out = erc.run(sch, _cfg())
    assert isinstance(out, list)
    assert all(isinstance(f, Finding) for f in out)


# ---------------------------------------------------------------------------
# pin-type conflict matrix (ERC_PIN_CONFLICT)
# ---------------------------------------------------------------------------
def _two_pin_net(etype_a: PinType, etype_b: PinType):
    u1 = _comp("U1", [_pin("1", etype=etype_a)])
    u2 = _comp("U2", [_pin("1", etype=etype_b)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    return _sch([u1, u2], [net])


@pytest.mark.parametrize(
    "a,b,sev",
    [
        (PinType.OPEN_COLLECTOR, PinType.OUTPUT, Severity.WARNING),
        (PinType.OPEN_COLLECTOR, PinType.POWER_OUT, Severity.WARNING),
        (PinType.OPEN_EMITTER, PinType.OUTPUT, Severity.WARNING),
        (PinType.OPEN_EMITTER, PinType.POWER_OUT, Severity.WARNING),
        (PinType.TRI_STATE, PinType.POWER_OUT, Severity.WARNING),
        (PinType.TRI_STATE, PinType.OUTPUT, Severity.NOTE),
        (PinType.OPEN_COLLECTOR, PinType.OPEN_EMITTER, Severity.NOTE),
    ],
)
def test_pin_conflict_matrix_cells(a, b, sev):
    findings = erc.run(_two_pin_net(a, b))
    hits = _by_code(findings, "ERC_PIN_CONFLICT")
    assert len(hits) == 1
    assert hits[0].severity is sev
    assert "U1.1" in hits[0].refs and "U2.1" in hits[0].refs


def test_pin_conflict_safe_pairs_are_silent():
    for a, b in [
        (PinType.OPEN_COLLECTOR, PinType.OPEN_COLLECTOR),  # wired-OR is fine
        (PinType.INPUT, PinType.OUTPUT),
        (PinType.PASSIVE, PinType.OUTPUT),
        (PinType.TRI_STATE, PinType.TRI_STATE),
    ]:
        findings = erc.run(_two_pin_net(a, b))
        assert _by_code(findings, "ERC_PIN_CONFLICT") == [], (a, b)


def test_pin_conflict_demoted_at_low_type_confidence():
    # 2 typed pins out of 12 -> confidence < 0.2 -> NOTE with the caveat text
    passives = _comp("R1", [_pin(str(i), etype=PinType.PASSIVE, x=float(i))
                            for i in range(1, 11)])
    u1 = _comp("U1", [_pin("1", etype=PinType.OPEN_COLLECTOR)])
    u2 = _comp("U2", [_pin("1", etype=PinType.OUTPUT)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    findings = erc.run(_sch([passives, u1, u2], [net]))
    hits = _by_code(findings, "ERC_PIN_CONFLICT")
    assert len(hits) == 1 and hits[0].severity is Severity.NOTE
    assert "type-confidence low" in hits[0].message


def test_pin_conflict_waiver():
    findings = erc.run(
        _two_pin_net(PinType.OPEN_COLLECTOR, PinType.OUTPUT),
        _cfg(waivers=[{"rule": "pin_conflict", "net": "SIG"}]),
    )
    assert _by_code(findings, "ERC_PIN_CONFLICT") == []


def test_pin_conflict_no_erc_suppression():
    u1 = _comp("U1", [_pin("1", etype=PinType.OPEN_COLLECTOR, x=100, y=100)])
    u2 = _comp("U2", [_pin("1", etype=PinType.OUTPUT, x=200, y=100)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    findings = erc.run(_sch([u1, u2], [net], no_erc_points=[(100.0, 100.0)]))
    assert _by_code(findings, "ERC_PIN_CONFLICT") == []


# ---------------------------------------------------------------------------
# undriven POWER_IN (ERC_POWER_IN_UNDRIVEN)
# ---------------------------------------------------------------------------
def test_power_in_undriven_flagged():
    u1 = _comp("U1", [_pin("1", name="VDD", etype=PinType.POWER_IN)])
    u2 = _comp("U2", [_pin("1", etype=PinType.OUTPUT)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    findings = erc.run(_sch([u1, u2], [net]))
    hits = _by_code(findings, "ERC_POWER_IN_UNDRIVEN")
    assert len(hits) == 1 and hits[0].severity is Severity.WARNING
    assert "U1.1" in hits[0].refs


def test_power_in_on_named_rail_is_trusted():
    u1 = _comp("U1", [_pin("1", name="VDD", etype=PinType.POWER_IN)])
    u2 = _comp("U2", [_pin("1", etype=PinType.PASSIVE)])
    net = _net("3V3", [("U1", "1"), ("U2", "1")])
    findings = erc.run(_sch([u1, u2], [net]))
    assert _by_code(findings, "ERC_POWER_IN_UNDRIVEN") == []


def test_power_in_driven_by_power_out_is_silent():
    u1 = _comp("U1", [_pin("1", name="VDD", etype=PinType.POWER_IN)])
    u2 = _comp("U2", [_pin("1", etype=PinType.POWER_OUT)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    findings = erc.run(_sch([u1, u2], [net]))
    assert _by_code(findings, "ERC_POWER_IN_UNDRIVEN") == []


def test_power_in_undriven_waiver():
    u1 = _comp("U1", [_pin("1", etype=PinType.POWER_IN)])
    u2 = _comp("U2", [_pin("1", etype=PinType.OUTPUT)])
    net = _net("SIG", [("U1", "1"), ("U2", "1")])
    findings = erc.run(
        _sch([u1, u2], [net]),
        _cfg(waivers=[{"rule": "power_in_undriven", "net": "SIG"}]),
    )
    assert _by_code(findings, "ERC_POWER_IN_UNDRIVEN") == []
