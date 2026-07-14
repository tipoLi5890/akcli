"""Tests for the schematic -> SPICE deck builder (sim/deck.py).

The deck builder depends on the sibling ``sim.models`` stage (``resolve`` +
``spice_value``).  To keep these tests offline and independent of that stage's
landing order, a small but *faithful* stub models module is injected into
``sys.modules`` — in particular it implements the engineering-notation ->
SPICE ``M``/``MEG`` fix so the 1M -> 1MEG regression is exercised end to end.

Fixture builders mirror tests/test_checks_intent.py.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from akcli.errors import AkcliError
from akcli.model import Component, Net, Pin, Schematic
from akcli.report import Severity


# --------------------------------------------------------------------------- #
# stub models stage (resolve / spice_value) — injected before importing deck
# --------------------------------------------------------------------------- #
@dataclass
class _Card:
    letter: str = ""
    value: str | None = None
    model_name: str | None = None
    model_card: str | None = None
    pin_order: list | None = None
    status: str = "ok"
    note: str = ""


_SI = {
    "T": "e12", "G": "e9", "MEG": "e6", "K": "e3", "k": "e3",
    "m": "e-3", "u": "e-6", "n": "e-9", "p": "e-12", "f": "e-15",
}


def _spice_value(text: str) -> str:
    """Engineering notation -> SPICE-safe value; the M/MEG fix lives here.

    SPICE reads a bare ``M`` suffix as *milli*, never mega, so ``1M`` becomes
    ``1MEG`` here.  Everything else is passed through unchanged.
    """
    s = str(text).strip()
    if s.upper().endswith("MEG"):
        return s
    if s and s[-1] in ("M", "m") and not s.upper().endswith("MEG"):
        # a trailing lone 'M'/'m' is milli in SPICE; upper-case 'M' means mega
        # in schematic notation -> promote to MEG.
        if s[-1] == "M":
            return s[:-1] + "MEG"
    return s


def _resolve(comp, spec):
    ref = (comp.library_ref or "").lower()
    des = comp.designator
    if des.startswith("R") or "device:r" in ref:
        return _Card(letter="R", value=_spice_value(comp.value or "1k"))
    if des.startswith("C") or "device:c" in ref:
        return _Card(letter="C", value=_spice_value(comp.value or "100n"))
    if des.startswith("D") or "device:d" in ref:
        return _Card(
            letter="D", model_name="DMOD",
            model_card=".model DMOD D(IS=1e-14 N=1.05)",
        )
    if des.startswith("TP") or ref == "skip":
        return _Card(status="skip", note="test point")
    if des.startswith("U") or ref == "unmodeled":
        return _Card(status="unmodeled", note="no model available")
    return _Card(letter="R", value=_spice_value(comp.value or "1k"))


_stub = types.ModuleType("akcli.sim.models")
_stub.resolve = _resolve
_stub.spice_value = _spice_value

from akcli.sim import deck  # noqa: E402


@pytest.fixture(autouse=True)
def _install_stub(monkeypatch):
    """Swap the stub models stage in for each test, restoring afterwards.

    ``deck.build`` resolves ``sim.models`` lazily via ``from . import models``,
    which consults ``sys.modules`` at call time — so a per-test setitem is all
    the isolation needed, and the real ``sim.models`` module (plus the package
    attribute) is restored for every other test file in the session.
    """
    import akcli.sim as _sim

    monkeypatch.setitem(sys.modules, "akcli.sim.models", _stub)
    monkeypatch.setattr(_sim, "models", _stub, raising=False)


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
def _comp(designator, pin_numbers, *, value=None, library_ref="Device:R"):
    pins = [Pin(number=n, name=None, x_mil=0.0, y_mil=0.0) for n in pin_numbers]
    return Component(
        designator=designator, library_ref=library_ref,
        x_mil=0.0, y_mil=0.0, value=value, pins=pins,
    )


def _net(name, members, *, is_named=True, source_names=None):
    return Net(
        name=name,
        members=sorted(members),
        source_names=source_names if source_names is not None else ([name] if name else []),
        is_named=is_named,
    )


def _sch(components, nets):
    return Schematic(
        source_path="board.kicad_sch", source_format="kicad",
        components=components, nets=nets,
    )


class _Spec:
    """Minimal SimSpec stand-in (stimuli / analyses / options)."""

    def __init__(self, stimuli=None, analyses=None, options=None):
        self.stimuli = stimuli or []
        self.analyses = analyses or {}
        self.options = options or {}


def _rc_sch():
    """R1 + C1 across VIN/GND: a two-node RC low-pass."""
    return _sch(
        [
            _comp("R1", ["1", "2"], value="10k"),
            _comp("C1", ["1", "2"], value="100n", library_ref="Device:C"),
        ],
        [
            _net("VIN", [("R1", "1")]),
            _net("OUT", [("R1", "2"), ("C1", "1")]),
            _net("GND", [("C1", "2")]),
        ],
    )


# --------------------------------------------------------------------------- #
# ground mapping + --gnd override
# --------------------------------------------------------------------------- #
def test_ground_maps_to_zero():
    d = deck.build(_rc_sch(), _Spec())
    assert d.node_of["GND"] == "0"
    assert d.node_of["VIN"] == "VIN"
    assert d.node_of["OUT"] == "OUT"
    # C1 pin 2 sits on GND -> node 0
    assert "C1 OUT 0 100n" in d.text


def test_gnd_override_picks_named_net():
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k")],
        [
            _net("VCC", [("R1", "1")]),
            _net("VSS", [("R1", "2")]),
        ],
    )
    d = deck.build(sch, _Spec(), gnd="VSS")
    assert d.node_of["VSS"] == "0"
    assert d.node_of["VCC"] == "VCC"
    assert "R1 VCC 0 1k" in d.text


def test_no_ground_raises():
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k")],
        [_net("A", [("R1", "1")]), _net("B", [("R1", "2")])],
    )
    with pytest.raises(AkcliError) as ei:
        deck.build(sch, _Spec())
    assert ei.value.code == "SIM_NO_GROUND"
    assert "--gnd" in ei.value.message


# --------------------------------------------------------------------------- #
# node collision
# --------------------------------------------------------------------------- #
def test_named_N1_and_unnamed_net_stay_distinct():
    # A net literally named 'N1' must NOT be merged with a generated N<i> node
    # (item 2): the unnamed net skips to N2, so R1 and R2 land on distinct nodes.
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k"), _comp("R2", ["1", "2"], value="2k")],
        [
            _net("N1", [("R1", "1")]),
            _net(None, [("R2", "1")], is_named=False, source_names=[]),
            _net("GND", [("R1", "2"), ("R2", "2")]),
        ],
    )
    d = deck.build(sch, _Spec())
    assert d.node_of["N1"] == "N1"
    assert "R1 N1 0 1k" in d.text
    assert "R2 N2 0 2k" in d.text     # unnamed net skipped N1 -> N2
    assert "R2 N1 0" not in d.text     # never shorted onto the named N1


def test_sanitize_non_ascii_becomes_ascii_underscore():
    # item 10: isalnum() would keep 'µ' (and upper-case it to Greek Mu); the
    # explicit ASCII regex maps every non-[A-Za-z0-9_] char to '_'.
    from akcli.sim.deck import _sanitize
    tok = _sanitize("Vµ_SENSE")
    assert tok.isascii()
    assert tok == "V__SENSE"


def test_node_collision_raises():
    # "VIN+" and "VIN-" both sanitize to "VIN_" -> collision.
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k")],
        [
            _net("GND", [("R1", "1")]),
            _net("VIN+", [("R1", "2")]),
            _net("VIN-", [("R1", "2")]),
        ],
    )
    with pytest.raises(AkcliError) as ei:
        deck.build(sch, _Spec())
    assert ei.value.code == "SIM_NODE_COLLISION"
    assert "VIN+" in ei.value.message and "VIN-" in ei.value.message


# --------------------------------------------------------------------------- #
# M/MEG regression
# --------------------------------------------------------------------------- #
def test_one_mega_resistor_renders_meg():
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1M")],
        [_net("A", [("R1", "1")]), _net("GND", [("R1", "2")])],
    )
    d = deck.build(sch, _Spec())
    assert "R1 A 0 1MEG" in d.text
    assert "1M " not in d.text  # never a bare milli-M


# --------------------------------------------------------------------------- #
# layout: title first, .end last
# --------------------------------------------------------------------------- #
def test_title_is_first_line():
    d = deck.build(_rc_sch(), _Spec())
    lines = d.text.splitlines()
    assert lines[0] == "* akcli sim: board.kicad_sch"
    assert lines[-1] == ".end"


# --------------------------------------------------------------------------- #
# stimuli: bsource whitespace stripping + V/I sources
# --------------------------------------------------------------------------- #
def test_bsource_strips_all_whitespace():
    spec = _Spec(stimuli=[
        {"kind": "bsource", "name": "photo", "node": "OUT", "node2": "GND",
         "expr": "0.2m + 0.5m*sin(2*pi*100*time)"},
    ])
    d = deck.build(_rc_sch(), spec)
    assert "Bphoto OUT 0 I=0.2m+0.5m*sin(2*pi*100*time)" in d.text
    # no spaces survive anywhere in the I= token
    for ln in d.text.splitlines():
        if ln.startswith("Bphoto"):
            assert " I=" in ln and "= " not in ln
            assert "0.2m +" not in ln


def test_stimulus_unknown_node_warns_with_suggestions():
    # item 6: a stimulus node matching no net still emits, but warns with
    # close-match suggestions so a typo is obvious (rc_sch nets: VIN/OUT/GND).
    spec = _Spec(stimuli=[
        {"kind": "vsource", "name": "V1", "node": "VIN0", "value": "5"},
    ])
    d = deck.build(_rc_sch(), spec)
    hits = [f for f in d.warnings if f.code == "SIM_UNKNOWN_STIMULUS_NODE"]
    assert len(hits) == 1
    assert "VIN0" in hits[0].message
    assert "VIN" in hits[0].message           # difflib suggestion
    assert "V1 VIN0 0 5" in d.text            # still emitted (deck-only workflows)


def test_stimulus_known_node_does_not_warn():
    spec = _Spec(stimuli=[
        {"kind": "vsource", "name": "V1", "node": "VIN", "value": "5"},
    ])
    d = deck.build(_rc_sch(), spec)
    assert not any(f.code == "SIM_UNKNOWN_STIMULUS_NODE" for f in d.warnings)


def test_vsource_and_isource():
    spec = _Spec(stimuli=[
        {"kind": "vsource", "name": "1", "node": "VIN", "value": "dc 3 ac 1"},
        {"kind": "isource", "name": "bias", "node": "OUT", "value": "dc 1m"},
    ])
    d = deck.build(_rc_sch(), spec)
    assert "V1 VIN 0 dc 3 ac 1" in d.text
    assert "Ibias OUT 0 dc 1m" in d.text


# --------------------------------------------------------------------------- #
# unmodeled + skip findings
# --------------------------------------------------------------------------- #
def test_unmodeled_component_finding():
    sch = _sch(
        [_comp("U1", ["1", "2"], library_ref="unmodeled")],
        [_net("GND", [("U1", "1")]), _net("NET1", [("U1", "2")])],
    )
    d = deck.build(sch, _Spec())
    assert "U1" in d.unmodeled
    codes = [f.code for f in d.warnings]
    assert "SIM_UNMODELED" in codes
    assert all(f.severity == Severity.WARNING for f in d.warnings if f.code == "SIM_UNMODELED")
    assert "* unmodeled U1" in d.text


def test_skip_component_becomes_comment():
    sch = _sch(
        [_comp("TP1", ["1"], library_ref="skip")],
        [_net("GND", [("TP1", "1")])],
    )
    d = deck.build(sch, _Spec())
    assert "* skip TP1: test point" in d.text
    assert d.unmodeled == []


# --------------------------------------------------------------------------- #
# dangling pin -> NC node + warning
# --------------------------------------------------------------------------- #
def test_dangling_pin_gets_nc_node():
    # R1 pin 2 is on no net.
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k")],
        [_net("GND", [("R1", "1")])],
    )
    d = deck.build(sch, _Spec())
    assert "NC_R1_2" in d.text
    assert any(f.code == "SIM_DANGLING_PIN" for f in d.warnings)


# --------------------------------------------------------------------------- #
# unnamed nets -> N<index> nodes
# --------------------------------------------------------------------------- #
def test_unnamed_net_gets_indexed_node():
    sch = _sch(
        [_comp("R1", ["1", "2"], value="1k")],
        [
            _net(None, [("R1", "1")], is_named=False, source_names=[]),
            _net("GND", [("R1", "2")]),
        ],
    )
    d = deck.build(sch, _Spec())
    # R1 pin 1 -> unnamed node N1, pin 2 -> ground 0
    assert "R1 N1 0 1k" in d.text
    assert d.node_of["N1"] == "N1"


# --------------------------------------------------------------------------- #
# analyses dot-cards
# --------------------------------------------------------------------------- #
def test_analyses_emitted_as_dot_cards():
    spec = _Spec(analyses={"tran": "5u 100m", "ac": "dec 40 10 100k", "op": ""})
    d = deck.build(_rc_sch(), spec)
    assert ".tran 5u 100m" in d.text
    assert ".ac dec 40 10 100k" in d.text
    assert ".op" in d.text
    # analyses come after elements but before .end
    lines = d.text.splitlines()
    assert lines.index(".tran 5u 100m") < lines.index(".end")


def test_inline_analyses_can_be_suppressed():
    spec = _Spec(analyses={"tran": "5u 100m"}, options={"inline_analyses": False})
    d = deck.build(_rc_sch(), spec)
    assert ".tran" not in d.text


# --------------------------------------------------------------------------- #
# model-card dedup + extra_cards
# --------------------------------------------------------------------------- #
def test_model_cards_deduped_and_extra_appended():
    sch = _sch(
        [
            _comp("D1", ["1", "2"], library_ref="Device:D"),
            _comp("D2", ["1", "2"], library_ref="Device:D"),
        ],
        [
            _net("A", [("D1", "1"), ("D2", "1")]),
            _net("GND", [("D1", "2"), ("D2", "2")]),
        ],
    )
    spec = _Spec(options={"extra_cards": [".model DMOD D(IS=1e-14 N=1.05)", ".ic v(A)=0"]})
    d = deck.build(sch, spec)
    # the diode model card appears exactly once despite two diodes + the dup extra
    assert d.text.count(".model DMOD D(IS=1e-14 N=1.05)") == 1
    assert ".ic v(A)=0" in d.text
    # diode element names use the designator (already starts with D)
    assert "D1 A 0 DMOD" in d.text
    assert "D2 A 0 DMOD" in d.text


# --------------------------------------------------------------------------- #
# floating-node detection + auto-rshunt
# --------------------------------------------------------------------------- #
def _cap_only_sch():
    """CV net (C1 hi + C2, no source, no DC path) stranded by a skipped U1."""
    return _sch(
        [
            _comp("C1", ["1", "2"], value="10n", library_ref="Device:C"),
            _comp("C2", ["1", "2"], value="1u", library_ref="Device:C"),
            _comp("U1", ["1", "2"], library_ref="skip"),  # 555-like, skipped
        ],
        [
            _net("CV", [("C1", "1"), ("C2", "1"), ("U1", "1")]),
            _net("GND", [("C1", "2"), ("C2", "2"), ("U1", "2")]),
        ],
    )


def test_floating_node_detected_and_names_strander():
    d = deck.build(_cap_only_sch(), _Spec())
    hits = [f for f in d.warnings if f.code == "SIM_FLOATING_NODE"]
    assert len(hits) == 1
    assert hits[0].severity == Severity.WARNING
    assert "CV" in hits[0].message
    assert "U1" in hits[0].message              # the skipped strander is named
    assert "U1" in hits[0].refs


def test_auto_rshunt_added_when_floating_and_notes_it():
    d = deck.build(_cap_only_sch(), _Spec())  # options.rshunt absent -> auto
    assert ".option rshunt=1e12" in d.text
    notes = [f for f in d.warnings if f.code == "SIM_RSHUNT_ADDED"]
    assert len(notes) == 1
    assert notes[0].severity == Severity.NOTE
    assert "* akcli: rshunt auto-added" in d.text


def test_auto_rshunt_not_added_without_floating():
    d = deck.build(_rc_sch(), _Spec())  # RC has a resistor DC path -> no floating
    assert "rshunt" not in d.text
    assert not any(f.code == "SIM_RSHUNT_ADDED" for f in d.warnings)


def test_rshunt_false_never_emitted_even_when_floating():
    d = deck.build(_cap_only_sch(), _Spec(options={"rshunt": False}))
    assert "rshunt" not in d.text
    # the floating-node WARNING still fires; only the fix is suppressed
    assert any(f.code == "SIM_FLOATING_NODE" for f in d.warnings)
    assert not any(f.code == "SIM_RSHUNT_ADDED" for f in d.warnings)


def test_rshunt_explicit_value_always_emitted():
    d = deck.build(_rc_sch(), _Spec(options={"rshunt": "5e11"}))
    assert ".option rshunt=5e11" in d.text
    # explicit value emits no auto NOTE
    assert not any(f.code == "SIM_RSHUNT_ADDED" for f in d.warnings)


def test_rshunt_numeric_value_rendered_without_dot_zero():
    d = deck.build(_rc_sch(), _Spec(options={"rshunt": 1e12}))
    assert ".option rshunt=1000000000000" in d.text


def test_floating_node_suppressed_by_stimulus():
    # A vsource on the cap-only net gives it a DC path -> not floating.
    d = deck.build(_cap_only_sch(), _Spec(stimuli=[
        {"kind": "vsource", "name": "Vcv", "node": "CV", "value": "1"},
    ]))
    assert not any(f.code == "SIM_FLOATING_NODE" for f in d.warnings)
    assert "rshunt" not in d.text


# --------------------------------------------------------------------------- #
# undriven power-rail diagnostic
# --------------------------------------------------------------------------- #
def _rail_sch(rail_name):
    """A power-named rail (decoupling cap + load R to GND) alongside a driven IN
    signal — the classic 'drove the input but forgot the rail source' trap."""
    return _sch(
        [
            _comp("C1", ["1", "2"], value="100n", library_ref="Device:C"),
            _comp("R1", ["1", "2"], value="10k"),   # rail -> GND load
            _comp("R2", ["1", "2"], value="1k"),     # IN -> GND load
        ],
        [
            _net(rail_name, [("C1", "1"), ("R1", "1")]),
            _net("IN", [("R2", "1")]),
            _net("GND", [("C1", "2"), ("R1", "2"), ("R2", "2")]),
        ],
    )


# A stimulus that drives IN (not the rail): the deck is "driven", so the
# undriven-rail check is active, but the rail itself has no source.
def _driven_in():
    return _Spec(stimuli=[{"kind": "vsource", "name": "Vin", "node": "IN", "value": "1"}])


@pytest.mark.parametrize("rail", ["+3V", "VCC", "VDD", "VBAT", "VSUP", "vcc_5v"])
def test_undriven_rail_warns_for_power_names(rail):
    d = deck.build(_rail_sch(rail), _driven_in())
    hits = [f for f in d.warnings if f.code == "SIM_UNDRIVEN_RAIL"]
    assert len(hits) == 1, rail
    assert rail in hits[0].message


def test_undriven_rail_silent_when_vsource_drives_it():
    d = deck.build(_rail_sch("VCC"), _Spec(stimuli=[
        {"kind": "vsource", "name": "Vsup", "node": "VCC", "value": "3.3"},
    ]))
    assert not any(f.code == "SIM_UNDRIVEN_RAIL" for f in d.warnings)


def test_undriven_rail_not_checked_without_stimuli():
    # A deck-only build (no stimuli at all) must not flag rails — every rail
    # would be 'undriven' by definition.
    d = deck.build(_rail_sch("VCC"), _Spec())
    assert not any(f.code == "SIM_UNDRIVEN_RAIL" for f in d.warnings)


def test_non_power_named_net_never_flagged_as_rail():
    d = deck.build(_rail_sch("SIGNAL"), _driven_in())
    assert not any(f.code == "SIM_UNDRIVEN_RAIL" for f in d.warnings)


# --------------------------------------------------------------------------- #
# golden mini-deck (normalized whole-string compare)
# --------------------------------------------------------------------------- #
def test_golden_mini_deck():
    spec = _Spec(
        stimuli=[{"kind": "vsource", "name": "1", "node": "VIN", "value": "dc 1 ac 1"}],
        analyses={"ac": "dec 20 1 1meg"},
    )
    d = deck.build(_rc_sch(), spec)
    expected = "\n".join([
        "* akcli sim: board.kicad_sch",
        "R1 VIN OUT 10k",
        "C1 OUT 0 100n",
        "V1 VIN 0 dc 1 ac 1",
        ".ac dec 20 1 1meg",
        ".end",
    ]) + "\n"
    assert d.text == expected
