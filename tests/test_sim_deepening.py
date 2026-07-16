"""Sim deepening: op-amp/MOSFET behavioral models + new deck diagnostics.

Offline half: the builtin library exposes the new subcircuits and the deck
builder emits the new solver-trap warnings (`SIM_ZERO_PASSIVE`,
`SIM_STIMULUS_SHORTED`). Engine half (skipped without libngspice): the
op-amp closes a unity-gain loop to the right DC point, and the NMOS switch
actually switches — the models are validated against ngspice, not just
inspected as text.
"""

from __future__ import annotations

import re

import pytest

from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.sim import assertions, deck, engine, models

_HAVE_NGSPICE = engine.available() is not None
_needs_engine = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not installed on this machine")

_MEAS_RX = re.compile(r"^(?:stdout\s+)?(\S+)\s*=\s*([-+0-9.eE]+)")


def _pin(number: str, name: str | None = None,
         etype: PinType = PinType.PASSIVE) -> Pin:
    return Pin(number, name, 0, 0, etype)


def _res(designator: str, value: str) -> Component:
    return Component(designator, "Device:R", 0, 0, value=value,
                     pins=[_pin("1"), _pin("2")])


def _meas(lines: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in lines:
        m = _MEAS_RX.match(line.strip())
        if m:
            out[m.group(1).lower()] = float(m.group(2))
    return out


# --------------------------------------------------------------------------- #
# offline: builtin library exposure
# --------------------------------------------------------------------------- #
def test_builtin_library_has_new_models():
    names = models.builtin_names()
    assert {"AKCLI_OPAMP", "AKCLI_NMOS_SW", "AKCLI_PMOS_SW"} <= set(names)
    block = models.load_builtin("AKCLI_OPAMP")
    assert block.startswith(".subckt AKCLI_OPAMP inp inn out vcc vee")
    assert models.load_builtin("AKCLI_NMOS_SW").startswith(
        ".subckt AKCLI_NMOS_SW d g s")


# --------------------------------------------------------------------------- #
# offline: new deck diagnostics
# --------------------------------------------------------------------------- #
def _divider_sch(r2_value: str) -> Schematic:
    r1, r2 = _res("R1", "10k"), _res("R2", r2_value)
    nets = [
        Net("VIN", [("R1", "1")], source_names=["VIN"]),
        Net("MID", [("R1", "2"), ("R2", "1")], source_names=["MID"]),
        Net("GND", [("R2", "2")], source_names=["GND"]),
    ]
    return Schematic("z.kicad_sch", "kicad", [r1, r2], nets)


def test_zero_passive_warned():
    spec = assertions.SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vin",
                  "node": "VIN", "node2": "0", "value": "5"}],
        analyses={"op": ""})
    d = deck.build(_divider_sch("0"), spec)
    hits = [w for w in d.warnings if w.code == "SIM_ZERO_PASSIVE"]
    assert len(hits) == 1 and "R2" in hits[0].refs


def test_nonzero_passive_not_warned():
    spec = assertions.SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vin",
                  "node": "VIN", "node2": "0", "value": "5"}],
        analyses={"op": ""})
    d = deck.build(_divider_sch("22k"), spec)
    assert not [w for w in d.warnings if w.code == "SIM_ZERO_PASSIVE"]


def test_shorted_stimulus_warned():
    spec = assertions.SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vbad",
                  "node": "GND", "node2": "0", "value": "5"}],
        analyses={"op": ""})
    d = deck.build(_divider_sch("22k"), spec)
    hits = [w for w in d.warnings if w.code == "SIM_STIMULUS_SHORTED"]
    assert len(hits) == 1 and "Vbad" in hits[0].refs


# --------------------------------------------------------------------------- #
# engine: the op-amp closes a unity-gain loop
# --------------------------------------------------------------------------- #
def _opamp_buffer_sch() -> Schematic:
    # U1 pin numbering: 1=inp 2=inn 3=out 4=vcc 5=vee; OUT wired back to inn.
    u1 = Component("U1", "Amplifier:OPAMP", 0, 0,
                   pins=[_pin("1", "IN+", PinType.INPUT),
                         _pin("2", "IN-", PinType.INPUT),
                         _pin("3", "OUT", PinType.OUTPUT),
                         _pin("4", "V+", PinType.POWER_IN),
                         _pin("5", "V-", PinType.POWER_IN)])
    rload = _res("R1", "10k")
    nets = [
        Net("SET", [("U1", "1")], source_names=["SET"]),
        Net("OUT", [("U1", "2"), ("U1", "3"), ("R1", "1")],
            source_names=["OUT"]),
        Net("VCC", [("U1", "4")], source_names=["VCC"]),
        Net("GND", [("U1", "5"), ("R1", "2")], source_names=["GND"]),
    ]
    return Schematic("buf.kicad_sch", "kicad", [u1, rload], nets)


@_needs_engine
def test_opamp_unity_buffer_tracks_input(tmp_path):
    spec = assertions.SimSpec(
        stimuli=[
            {"kind": "vsource", "name": "Vsup",
             "node": "VCC", "node2": "0", "value": "3.3"},
            {"kind": "vsource", "name": "Vset",
             "node": "SET", "node2": "0", "value": "1.65"},
        ],
        analyses={"tran": "10u 1m"},
        models={"U1": {"device": "X", "model_name": "AKCLI_OPAMP",
                       "pin_order": ["1", "2", "3", "4", "5"]}},
    )
    d = deck.build(_opamp_buffer_sch(), spec)
    res = engine.run(d.text, ["run", "meas tran vout FIND v(OUT) AT=0.9m"],
                     timeout=60, workdir=str(tmp_path))
    assert res.ok, res.log
    got = _meas(res.meas_lines)
    assert abs(got["vout"] - 1.65) < 0.05  # buffer tracks within 50 mV


# --------------------------------------------------------------------------- #
# engine: the NMOS switch switches
# --------------------------------------------------------------------------- #
def _nmos_sch() -> Schematic:
    q1 = Component("Q1", "Transistor_FET:NMOS", 0, 0,
                   pins=[_pin("1", "D"), _pin("2", "G"), _pin("3", "S")])
    rpull = _res("R1", "1k")
    nets = [
        Net("VCC", [("R1", "1")], source_names=["VCC"]),
        Net("DRAIN", [("R1", "2"), ("Q1", "1")], source_names=["DRAIN"]),
        Net("GATE", [("Q1", "2")], source_names=["GATE"]),
        Net("GND", [("Q1", "3")], source_names=["GND"]),
    ]
    return Schematic("sw.kicad_sch", "kicad", [q1, rpull], nets)


@_needs_engine
@pytest.mark.parametrize("vgate,expect_low", [("5", True), ("0", False)])
def test_nmos_switch_switches(tmp_path, vgate, expect_low):
    spec = assertions.SimSpec(
        stimuli=[
            {"kind": "vsource", "name": "Vsup",
             "node": "VCC", "node2": "0", "value": "5"},
            {"kind": "vsource", "name": "Vg",
             "node": "GATE", "node2": "0", "value": vgate},
        ],
        analyses={"tran": "10u 1m"},
        models={"Q1": {"device": "X", "model_name": "AKCLI_NMOS_SW",
                       "pin_order": ["1", "2", "3"]}},
    )
    d = deck.build(_nmos_sch(), spec)
    res = engine.run(d.text, ["run", "meas tran vdrain FIND v(DRAIN) AT=0.9m"],
                     timeout=60, workdir=str(tmp_path))
    assert res.ok, res.log
    got = _meas(res.meas_lines)
    if expect_low:
        assert got["vdrain"] < 0.05   # Ron=0.1 vs 1k pull-up -> ~0.5 mV
    else:
        assert got["vdrain"] > 4.9    # off: 10 meg vs 1k -> rail
