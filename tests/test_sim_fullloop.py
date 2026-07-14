"""Closed-loop comparator channel — the reference "simulate a real comparator".

This is the end-to-end proof of the ``spec.models`` ``pin_order`` path against
the packaged behavioural comparator (``AKCLI_COMPARATOR`` in ``builtin.lib``): a
single-channel envelope/peak detector feeding a Schmitt-triggered open-collector
comparator built entirely in the test — no new ``src`` feature, no fixture file.

The circuit is a classic non-inverting Schmitt-trigger comparator channel wired
exactly the way a real LM339 unit is:

* a ``bsource`` drives node ``HP`` with an amplitude-triangle 50 kHz carrier;
* a BAT54-style Schottky ``D1`` rectifies it into ``PEAK`` with an
  ``R3||C1`` peak-hold (RC = 1 ms);
* ``U1`` is one LM339 unit placed as a 4-pin symbol (pins 5=IN+, 4=IN-,
  2=OUT, 3=V+) and mapped to the builtin ``AKCLI_COMPARATOR`` subckt through a
  ``spec.models`` ``pin_order`` that puts its symbol pins in the model's
  ``inp inn out vcc`` terminal order;
* an ``R1/R2`` divider sets the reference ``VREF`` on the model's ``inp``;
* the signal reaches the model's ``inn`` (the summing/non-inverting node
  ``SUMM``) through ``R4``, with an ``R5`` = 1 MEG hysteresis path from ``OUT``
  back to that same node — positive feedback that makes the edge regenerative;
* ``R6`` is the open-collector pull-up.

Because ``AKCLI_COMPARATOR`` *sinks* (drives OUT low) when ``inp > inn`` — the
opposite sense to a real LM339, which sinks when IN- > IN+ — the physical
non-inverting (+) input maps to the model's ``inn`` port and the reference to
``inp``; the ``pin_order`` encodes exactly that. The result behaves like a real
LM339 channel: OUT idles LOW, snaps HIGH when the detected peak clears the
threshold, and shows a measurable attack/release hysteresis band.

The offline half asserts the deck emits exactly one ``X`` element with the four
mapped nodes in ``inp inn out vcc`` order. The live half (skipped without
libngspice) drives the real engine and asserts (1) idle OUT low with PEAK below
threshold, (2) a regenerative HIGH snap far faster than the RC alone, and
(3) a release threshold shifted from the attack threshold by the ``R4/R5``
ratio predicted by the non-inverting Schmitt relation.

See :mod:`akcli.sim.deck`, :mod:`akcli.sim.models`
(``resolve``/``builtin.lib``) and :mod:`akcli.sim.engine`.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.sim import assertions, deck as deckmod, engine

_HAVE_NGSPICE = engine.available() is not None
_needs_ngspice = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not installed on this machine"
)

# Component values that set the measurable predictions (kept as numbers so the
# test asserts against them rather than restating magic constants).
_R_SIGNAL = 100e3        # R4: PEAK -> SUMM (Schmitt input resistor)
_R_FEEDBACK = 1e6        # R5: OUT -> SUMM (1 MEG hysteresis feedback)
_R_HOLD = 100e3          # R3: PEAK -> GND (peak-hold discharge)
_C_HOLD = 10e-9          # C1: PEAK -> GND
_RC_HOLD = _R_HOLD * _C_HOLD          # 1 ms envelope time constant
# Non-inverting Schmitt input-referred hysteresis = ΔV_out * (Rsignal / Rfeedback).
_HYST_RATIO = _R_SIGNAL / _R_FEEDBACK  # 0.1


# --------------------------------------------------------------------------- #
# circuit + spec construction (the reusable "comparator channel" recipe)
# --------------------------------------------------------------------------- #
def _pin(number: str, name: str | None = None,
         etype: PinType = PinType.PASSIVE) -> Pin:
    return Pin(number, name, 0, 0, etype)


def _res(designator: str, value: str) -> Component:
    return Component(designator, "Device:R", 0, 0, value=value,
                     pins=[_pin("1"), _pin("2")])


def _cap(designator: str, value: str) -> Component:
    return Component(designator, "Device:C", 0, 0, value=value,
                     pins=[_pin("1"), _pin("2")])


def _loop_schematic() -> Schematic:
    """Build the closed-loop comparator channel as a KiCad-shaped schematic.

    Designators are numeric-suffixed (``R1``..``R6``, ``C1``) so the passive
    prefix+value heuristic in :func:`akcli.sim.models.resolve` models
    them; ``D1`` carries A/K pin names so its anode/cathode SPICE order is
    derived from the names; ``U1`` is the multi-pin comparator symbol whose
    terminal order is supplied by ``spec.models`` (see :func:`_loop_spec`).
    """
    d1 = Component("D1", "Device:D_Schottky", 0, 0,
                   pins=[_pin("1", "K"), _pin("2", "A")])
    r1 = _res("R1", "40k")     # VCC -> VREF (divider top)
    r2 = _res("R2", "10k")     # VREF -> GND (divider bottom) => VREF ~ 1.0 V
    r3 = _res("R3", "100k")    # PEAK -> GND (peak-hold discharge)
    r4 = _res("R4", "100k")    # PEAK -> SUMM (Schmitt input resistor)
    r5 = _res("R5", "1meg")    # OUT  -> SUMM (hysteresis feedback)
    r6 = _res("R6", "10k")     # VCC  -> OUT  (open-collector pull-up)
    c1 = _cap("C1", "10n")     # PEAK -> GND (peak-hold capacitor)
    # One LM339 unit: 5=IN+, 4=IN-, 2=OUT, 3=V+ (stock single-channel numbering).
    u1 = Component("U1", "Comparator:LM339", 0, 0,
                   pins=[_pin("2", "OUT", PinType.OPEN_COLLECTOR),
                         _pin("3", "V+", PinType.POWER_IN),
                         _pin("4", "IN-", PinType.INPUT),
                         _pin("5", "IN+", PinType.INPUT)])

    nets = [
        Net("VCC", [("R1", "1"), ("R6", "1"), ("U1", "3")], source_names=["VCC"]),
        Net("VREF", [("R1", "2"), ("R2", "1"), ("U1", "4")]),
        Net("GND", [("R2", "2"), ("R3", "2"), ("C1", "2")], source_names=["GND"]),
        Net("PEAK", [("D1", "1"), ("R3", "1"), ("R4", "1"), ("C1", "1")],
            source_names=["PEAK"]),
        Net("SUMM", [("R4", "2"), ("R5", "2"), ("U1", "5")], source_names=["SUMM"]),
        Net("OUT", [("R5", "1"), ("R6", "2"), ("U1", "2")], source_names=["OUT"]),
        Net("HP", [("D1", "2")], source_names=["HP"]),
    ]
    return Schematic("comparator_loop.kicad_sch", "kicad",
                     [d1, r1, r2, r3, r4, r5, r6, c1, u1], nets)


def _loop_spec() -> assertions.SimSpec:
    """The ``sim.json`` for the loop: supply, carrier stimulus, models.

    ``U1`` maps to the builtin ``AKCLI_COMPARATOR`` via ``pin_order`` — symbol
    pins ordered as the model's ``inp inn out vcc`` terminals: ``["4","5","2",
    "3"]`` puts VREF (pin 4) on ``inp``, SUMM (pin 5) on ``inn``, OUT (pin 2) on
    ``out`` and VCC (pin 3) on ``vcc``. ``method=gear`` keeps the stiff
    tanh-gated feedback loop convergent over the full 10 ms transient.
    """
    return assertions.SimSpec(
        stimuli=[
            {"kind": "vsource", "name": "Vsup",
             "node": "VCC", "node2": "0", "value": "5"},
            # Amplitude-triangle 50 kHz carrier: envelope ramps 0 -> 2 V by 5 ms
            # then back to 0 by 10 ms, so PEAK rises through both comparator
            # thresholds on the way up and back down (one attack, one release).
            {"kind": "bsource", "name": "Bdrv",
             "node": "HP", "node2": "0", "quantity": "V",
             "expr": "2*(1-abs(time-5m)/5m)*sin(3.14159e5*time)"},
        ],
        analyses={"tran": "1u 10m"},
        models={
            "U1": {"device": "X", "model_name": "AKCLI_COMPARATOR",
                   "pin_order": ["4", "5", "2", "3"]},
            "D1": {"device": "D", "model_name": "DBAT",
                   "model_card": ".model DBAT D(IS=2.4e-8 N=1.05 RS=0.1)"},
        },
        options={"extra_cards": [".options method=gear"]},
    )


# The single measurement pass driven live: rail levels, idle state, the attack
# and release PEAK crossings (sampled at the OUT mid-swing point) and the 10-90%
# regenerative edge time.
_MEAS_CMDS = [
    "run",
    "meas tran vout_lo AVG v(OUT) from=0 to=1m",
    "meas tran vout_hi MAX v(OUT) from=4.9m to=5.1m",
    "meas tran peak_idle MAX v(PEAK) from=0 to=0.5m",
    "meas tran vref FIND v(VREF) AT=0.1m",
    "meas tran attack_peak FIND v(PEAK) WHEN v(OUT)=2.5 RISE=1",
    "meas tran release_peak FIND v(PEAK) WHEN v(OUT)=2.5 FALL=1",
    "meas tran trise TRIG v(OUT) VAL=0.5 RISE=1 TARG v(OUT) VAL=4.5 RISE=1",
]

_MEAS_RX = re.compile(r"^(?:stdout\s+)?(\S+)\s*=\s*([-+0-9.eE]+)")


def _parse_meas(lines: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in lines:
        m = _MEAS_RX.match(line.strip())
        if m and "failed!" not in line:
            out[m.group(1)] = float(m.group(2))
    return out


# --------------------------------------------------------------------------- #
# offline: the deck emits exactly one mapped X-element in inp/inn/out/vcc order
# --------------------------------------------------------------------------- #
def test_deck_emits_single_comparator_x_in_port_order():
    d = deckmod.build(_loop_schematic(), _loop_spec())
    x_lines = [ln for ln in d.text.splitlines() if ln.startswith("X")]
    # Exactly one X element, its four nodes in the model's inp inn out vcc order:
    # inp=VREF (pin4), inn=SUMM (pin5), out=OUT (pin2), vcc=VCC (pin3).
    assert x_lines == ["XU1 VREF SUMM OUT VCC AKCLI_COMPARATOR"]
    # The builtin subckt block is injected exactly once, with matching ports.
    assert d.text.count(".subckt AKCLI_COMPARATOR") == 1
    assert ".subckt AKCLI_COMPARATOR inp inn out vcc" in d.text
    # Every part is modeled and the mapping is explicit — no assumed-order or
    # unmodeled warnings anywhere in the deck.
    codes = {w.code for w in d.warnings}
    assert "SIM_PIN_ORDER_ASSUMED" not in codes
    assert "SIM_UNMODELED" not in codes
    # The BAT54-style detector keeps anode->cathode = HP->PEAK (name-derived).
    assert "D1 HP PEAK DBAT" in d.text


# --------------------------------------------------------------------------- #
# live: one shared transient, three assertions over its measurements
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def loop_meas() -> dict[str, float]:
    """Build the deck once, run the 10 ms transient live, return measurements."""
    if not _HAVE_NGSPICE:
        pytest.skip("libngspice not installed on this machine")
    d = deckmod.build(_loop_schematic(), _loop_spec())
    workdir = Path(tempfile.mkdtemp(prefix="akcli_fullloop_"))
    res = engine.run(d.text, _MEAS_CMDS, timeout=120, workdir=workdir)
    assert res.ok, res.log
    meas = _parse_meas(res.meas_lines)
    # Fail loudly (not KeyError) if the engine dropped a measurement.
    missing = {"vout_lo", "vout_hi", "peak_idle", "vref",
               "attack_peak", "release_peak", "trise"} - set(meas)
    assert not missing, f"missing measurements {missing}\n{res.log}"
    return meas


@_needs_ngspice
def test_live_idle_out_low_peak_below_threshold(loop_meas):
    # (1) Idle: no stimulus yet -> open-collector output sits LOW (comparator
    # sinking because VREF > SUMM) and the detected peak has settled well below
    # the reference threshold.
    assert loop_meas["vout_lo"] < 0.1, loop_meas
    assert loop_meas["peak_idle"] < loop_meas["vref"], loop_meas
    # A sane ~1 V reference from the 40k/10k divider off a 5 V rail.
    assert loop_meas["vref"] == pytest.approx(1.0, abs=0.05)


@_needs_ngspice
def test_live_stimulus_snaps_high_regeneratively(loop_meas):
    # (2) Stimulus window: OUT snaps to (near) the rail, and the positive
    # feedback makes that edge regenerative — orders of magnitude faster than
    # the RC peak-hold could ever slew the input across the threshold band.
    assert loop_meas["vout_hi"] > 4.5, loop_meas
    # The attack actually happened above the reference (peak cleared threshold).
    assert loop_meas["attack_peak"] > loop_meas["vref"], loop_meas
    trise = loop_meas["trise"]
    assert trise > 0, loop_meas
    # Regenerative edge << RC (1 ms). A bare RC crossing of the hysteresis band
    # would take on the order of the time constant; the snap is sub-microsecond.
    assert trise < 0.05 * _RC_HOLD, (trise, _RC_HOLD)


@_needs_ngspice
def test_live_hysteresis_matches_resistor_ratio(loop_meas):
    # (3) Hysteresis: the release threshold (OUT falling) sits below the attack
    # threshold (OUT rising). Measured input-referred band width vs the
    # non-inverting Schmitt prediction ΔV_in = ΔV_out * (Rsignal / Rfeedback).
    attack = loop_meas["attack_peak"]
    release = loop_meas["release_peak"]
    assert release < attack, (release, attack)          # real hysteresis
    measured_band = attack - release
    out_swing = loop_meas["vout_hi"] - loop_meas["vout_lo"]
    predicted_band = out_swing * _HYST_RATIO
    # Within 20% of the R-ratio prediction (measured ~0.43 V vs predicted
    # ~0.50 V on this engine — the midpoint sampling under-counts the last
    # sliver of the rail-to-rail feedback step).
    assert abs(measured_band - predicted_band) <= 0.20 * predicted_band, (
        f"measured band {measured_band:.4f} V vs predicted "
        f"{predicted_band:.4f} V (ratio {_HYST_RATIO})"
    )
