"""Simulation-hardening regressions: the four silent-wrong-answer traps.

Each group here pins a failure mode that used to produce a *confident wrong
answer* rather than an error:

1. **Multi-token component values.** ``"10k 1%"`` reached ngspice verbatim, which
   read it as ``10`` — a 10k/10k divider off 3.3 V then measured 3.30 V instead
   of 1.65 V and still exited ``0`` / PASS. Values now go through
   ``models.passive_value``: trailing rating tokens are dropped with a
   ``SIM_VALUE_NORMALIZED`` warning, and anything not confidently parseable (or
   an ohms-marked value on an L — the ferrite-bead ``600R`` = 600 H trap) raises
   ``SIM_VALUE_INVALID``.
2. **Hierarchical node collisions.** Local labels are sheet-scoped, so two
   instances of one child sheet each own a net named ``MID``; SPICE is flat, so
   both became node ``MID`` and were silently shorted. Now
   ``SIM_NODE_COLLISION``, naming the two sheet scopes.
3. **Dropped parts gating the verdict.** ``Sim.Enable=0`` produced no finding at
   all, and the verdict was computed from assertion findings only — so a skipped
   ``R2`` in a measured divider printed PASS. Every dropped part is now visible
   (``SIM_PART_SKIPPED``) and one in series on a measured node is an ERROR.
4. **``op`` + assertions.** ngspice cannot ``.meas`` an operating point, so the
   documented combination always exited 7 as an "engine failure"; it is now
   rejected up front with ``SIM_OP_ASSERT_UNSUPPORTED`` (exit 2).

The live test at the bottom re-runs the original ``"10k 1%"`` divider repro
through the real engine; it is skipped when libngspice is absent.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from akcli import cli
from akcli.errors import AkcliError
from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.report import Severity
from akcli.sim import assertions, deck as deckmod, engine
from akcli.sim.models import passive_value, resolve

_HAVE_NGSPICE = engine.available() is not None
_needs_ngspice = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not installed on this machine"
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _pin(number: str, name: str | None = None) -> Pin:
    return Pin(number, name, 0, 0, PinType.PASSIVE)


def _part(designator: str, value: str | None = None, *, lib="Device:R",
          pins=("1", "2"), params=None, sheet="") -> Component:
    return Component(designator, lib, 0, 0, value=value, sheet=sheet,
                     parameters=dict(params or {}),
                     pins=[_pin(n) for n in pins])


def _divider_sch(r2_value: str | None = "10k", *, r2_params=None,
                 extra: list[Component] | None = None,
                 extra_nets: list[Net] | None = None) -> Schematic:
    """+3V3 -> R1 -> MID -> R2 -> GND (the repro circuit)."""
    comps = [_part("R1", "10k"), _part("R2", r2_value, params=r2_params)]
    nets = [
        Net("+3V3", [("R1", "1")], source_names=["+3V3"]),
        Net("MID", [("R1", "2"), ("R2", "1")], source_names=["MID"]),
        Net("GND", [("R2", "2")], source_names=["GND"]),
    ]
    return Schematic("divider.kicad_sch", "kicad",
                     comps + list(extra or []), nets + list(extra_nets or []))


def _divider_spec_doc() -> dict:
    return {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup", "node": "+3V3",
                     "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "approx": "1.65", "tol": "0.02"}],
    }


def _write_spec(tmp_path, doc: dict) -> str:
    p = tmp_path / "sim.json"
    p.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    return str(p)


def _spec(tmp_path, doc: dict | None = None):
    return assertions.load(_write_spec(tmp_path, doc or _divider_spec_doc()))


# --------------------------------------------------------------------------- #
# 1. component values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("10k", "10k"),
    ("10k 1%", "10k"),                 # tolerance token dropped
    ("100n 50V X7R", "100n"),          # rating + dielectric dropped
    ("4.7k, 1%", "4.7k"),
    ("10k/1%", "10k"),
    ("4.7 k", "4.7k"),                 # internal space is NOT a dropped token
    ("220 ohm", "220"),
    ("600R", "600"),                 # IEC 60062 R = ohms marker
    ("4R7", "4.7"),                    # R = decimal marker
    ("1k5", "1.5k"),
    ("2M2", "2.2MEG"),
    ("1M", "1MEG"),
])
def test_passive_value_table(raw, expected):
    value, _note = passive_value(raw, "R", "R1")
    assert value == expected


def test_passive_value_notes_the_dropped_tokens():
    value, note = passive_value("10k 1%", "R", "R2")
    assert value == "10k"
    assert "1%" in note and "10k" in note


def test_passive_value_reports_nothing_when_whole_value_consumed():
    assert passive_value("4.7 k", "R", "R2") == ("4.7k", "")
    # a split unit tail is rejoined for capacitors/inductors too
    assert passive_value("100 nF", "C", "C1") == ("100n", "")
    assert passive_value("10 uH", "L", "L1") == ("10u", "")


@pytest.mark.parametrize("raw", ["ABC", "1%", "TBD", "=DNP", "?", "LED red"])
def test_passive_value_rejects_unparseable(raw):
    with pytest.raises(AkcliError) as ei:
        passive_value(raw, "R", "R9")
    assert ei.value.code == "SIM_VALUE_INVALID"
    assert "R9" in ei.value.message


# --- 1a. the gate must not reject what ngspice itself accepts ---------------
# SPICE suffixes are case-insensitive, and the tool's main on-ramp (Altium
# import) routinely carries upper-cased values. The gate used to refuse "10U",
# "100N", "4U7", "1T" and a greek-mu "1μF" — values ngspice reads without
# complaint — which turned a legal schematic into exit 6.
@pytest.mark.parametrize("raw,letter,expected", [
    ("10U", "C", "10u"),
    ("1U", "C", "1u"),
    ("4U7", "C", "4.7u"),          # IEC embedded decimal, upper case
    ("100N", "C", "100n"),
    ("1P", "C", "1p"),
    ("10UF", "C", "10u"),          # upper-cased farad unit too
    ("1μF", "C", "1u"),            # U+03BC GREEK SMALL LETTER MU
    ("1µF", "C", "1u"),            # U+00B5 MICRO SIGN
    ("1T", "R", "1T"),             # tera
    ("1t", "R", "1T"),
    ("10K", "R", "10k"),
    ("10G", "R", "10G"),
    ("10g", "R", "10G"),
    ("10UH", "L", "10u"),
    ("2M2", "R", "2.2MEG"),        # M is still mega...
    ("2m2", "R", "2.2m"),          # ...and m is still milli
])
def test_passive_value_accepts_spice_legal_case_variants(raw, letter, expected):
    assert passive_value(raw, letter, "X1")[0] == expected


def test_passive_value_case_folding_never_touches_milli_vs_mega():
    # m/M is the one pair where case carries the meaning; the deck renders the
    # unambiguous SPICE spellings so ngspice can never re-read them either way.
    assert passive_value("1M", "R", "R1")[0] == "1MEG"
    assert passive_value("1m", "R", "R1")[0] == "1m"
    assert passive_value("1MEG", "R", "R1")[0] == "1MEG"


# --- 1b. values Python's float() accepts but a circuit can never mean -------
# ``float()`` was the gate's first test, and it happily returns a number for
# ``inf``/``nan``/``1_000``/``1e400``. Those strings then reached the deck
# VERBATIM: ngspice reads "1_000" as *1*, so a 10k/1_000 divider off 3.3 V
# measured 3.3e-4 V and still exited 0/PASS — the exact class of confident
# wrongness this gate exists to stop.
@pytest.mark.parametrize("raw", [
    "1_000", "1_0", "inf", "-inf", "INF", "Infinity", "nan", "NaN", "1e400",
])
def test_passive_value_rejects_what_python_float_would_accept(raw):
    with pytest.raises(AkcliError) as ei:
        passive_value(raw, "R", "R9")
    assert ei.value.code == "SIM_VALUE_INVALID"
    assert "R9" in ei.value.message


def test_value_gate_error_lists_the_accepted_formats():
    # A refusal has to teach the fix, not just say no.
    with pytest.raises(AkcliError) as ei:
        passive_value("1_000", "R", "R9")
    msg = ei.value.message
    for hint in ("4700", "1e-7", "4.7k", "4k7", "m=milli", "M=mega", "1_000"):
        assert hint in msg, msg


def test_ohms_marked_value_on_inductor_is_invalid():
    # A ferrite bead's "600R" is 600 ohms at its test frequency; emitting
    # "L1 ... 600" would simulate a 600 HENRY inductor.
    with pytest.raises(AkcliError) as ei:
        passive_value("600R", "L", "L1")
    assert ei.value.code == "SIM_VALUE_INVALID"
    assert "600 H" in ei.value.message
    # ... while the same marker on a resistor is exactly what it says.
    assert passive_value("600R", "R", "R1")[0] == "600"


def test_farad_marked_value_on_resistor_is_invalid():
    with pytest.raises(AkcliError) as ei:
        passive_value("100nF", "R", "R1")
    assert ei.value.code == "SIM_VALUE_INVALID"
    assert passive_value("100nF", "C", "C1")[0] == "100n"


@pytest.mark.parametrize("marker", ["DNP", "dnp", "NP", "OPEN", "no pop"])
def test_not_populated_value_is_skipped_not_an_error(marker):
    card = resolve(_part("R4", marker), None)
    assert card.status == "skip"


def test_resolve_normalizes_multi_token_value_and_notes_it():
    card = resolve(_part("R2", "10k 1%"), None)
    assert card.status == "ok"
    assert card.value == "10k"
    assert "1%" in card.value_note


def test_deck_emits_normalized_value_and_warns(tmp_path):
    d = deckmod.build(_divider_sch("10k 1%"), _spec(tmp_path), gnd="GND")
    assert "R2 MID 0 10k" in d.text
    assert "1%" not in d.text
    hits = [f for f in d.warnings if f.code == "SIM_VALUE_NORMALIZED"]
    assert len(hits) == 1
    assert hits[0].severity == Severity.WARNING
    assert "R2" in hits[0].refs


def test_deck_refuses_unparseable_value(tmp_path):
    with pytest.raises(AkcliError) as ei:
        deckmod.build(_divider_sch("what?"), _spec(tmp_path), gnd="GND")
    assert ei.value.code == "SIM_VALUE_INVALID"


def test_cli_unparseable_value_exits_6(tmp_path, capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    monkeypatch.setattr(sim_cmd, "_load_schematic",
                        lambda _p: _divider_sch("bogus value"))
    assert cli.main(["sim", "divider.kicad_sch", "--deck-only"]) == 6
    assert "SIM_VALUE_INVALID" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 2. hierarchical node collision
# --------------------------------------------------------------------------- #
def _two_instance_sch() -> Schematic:
    """Two instances of one child sheet, each with a sheet-local label MID."""
    comps = [
        _part("R1", "10k", sheet="child.kicad_sch:A"),
        _part("R2", "10k", sheet="child.kicad_sch:A"),
        _part("R3", "10k", sheet="child.kicad_sch:B"),
        _part("R4", "10k", sheet="child.kicad_sch:B"),
    ]
    nets = [
        Net("GND", [("R2", "2"), ("R4", "2")], source_names=["GND"]),
        Net("+3V3", [("R1", "1"), ("R3", "1")], source_names=["+3V3"]),
        # two DISTINCT nets that share the sheet-local name
        Net("MID", [("R1", "2"), ("R2", "1")], source_names=["MID"]),
        Net("MID", [("R3", "2"), ("R4", "1")], source_names=["MID"]),
    ]
    return Schematic("top.kicad_sch", "kicad", comps, nets)


def test_same_name_nets_from_two_sheet_scopes_collide(tmp_path):
    with pytest.raises(AkcliError) as ei:
        deckmod.build(_two_instance_sch(), _spec(tmp_path), gnd="GND")
    assert ei.value.code == "SIM_NODE_COLLISION"
    msg = ei.value.message
    assert "MID" in msg
    # the message must name BOTH scopes so the user can tell them apart
    assert "child.kicad_sch:A" in msg and "child.kicad_sch:B" in msg


def test_single_scope_net_does_not_collide(tmp_path):
    d = deckmod.build(_divider_sch(), _spec(tmp_path), gnd="GND")
    assert "R1 _3V3 MID 10k" in d.text


def test_cli_scope_collision_exits_6(tmp_path, capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: _two_instance_sch())
    assert cli.main(["sim", "top.kicad_sch", "--deck-only"]) == 6
    assert "SIM_NODE_COLLISION" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 3. dropped parts gate the verdict
# --------------------------------------------------------------------------- #
def test_skipped_series_part_on_measured_net_is_an_error(tmp_path):
    sch = _divider_sch(r2_params={"Sim.Device": "R", "Sim.Enable": "0"})
    d = deckmod.build(sch, _spec(tmp_path), gnd="GND")
    hits = [f for f in d.warnings if f.code == "SIM_PART_SKIPPED"]
    assert len(hits) == 1
    assert hits[0].severity == Severity.ERROR
    assert "R2" in hits[0].refs and "MID" in hits[0].refs
    assert [p["designator"] for p in d.skipped] == ["R2"]
    assert d.skipped[0]["measured"] is True
    assert d.skipped[0]["status"] == "skip"


def test_skipped_part_off_the_measured_nets_is_only_a_warning(tmp_path):
    # A test point hanging on MID has ONE terminal: it cannot carry current, so
    # dropping it changes nothing measurable.
    tp = _part("TP1", None, lib="Connector:TestPoint", pins=("1",))
    sch = _divider_sch(extra=[tp],
                       extra_nets=[Net("TP_NET", [("TP1", "1")],
                                       source_names=["TP_NET"])])
    d = deckmod.build(sch, _spec(tmp_path), gnd="GND")
    hits = [f for f in d.warnings if f.code == "SIM_PART_SKIPPED"]
    assert len(hits) == 1
    assert hits[0].severity == Severity.WARNING
    assert d.skipped[0]["measured"] is False


def test_unmodeled_series_part_on_measured_net_escalates(tmp_path):
    # An unparameterized IC bridging MID and GND: SIM_UNMODELED stays a warning,
    # but the deck also reports the ERROR that the measurement excluded it.
    u1 = _part("U1", None, lib="Amplifier:LM358")
    sch = _divider_sch(extra=[u1], extra_nets=[])
    sch.nets[1].members = sorted([*sch.nets[1].members, ("U1", "1")])
    sch.nets[2].members = sorted([*sch.nets[2].members, ("U1", "2")])
    d = deckmod.build(sch, _spec(tmp_path), gnd="GND")
    codes = {f.code: f.severity for f in d.warnings}
    assert codes["SIM_UNMODELED"] == Severity.WARNING
    assert codes["SIM_PART_SKIPPED"] == Severity.ERROR
    assert {p["designator"] for p in d.skipped} == {"U1"}


def test_acknowledged_unmodeled_downgrades_series_error(tmp_path):
    # The same unmodeled series part, explicitly acknowledged in sim.json:
    # still reported, no longer an ERROR.
    u1 = _part("U1", None, lib="Amplifier:LM358")
    sch = _divider_sch(extra=[u1], extra_nets=[])
    sch.nets[1].members = sorted([*sch.nets[1].members, ("U1", "1")])
    sch.nets[2].members = sorted([*sch.nets[2].members, ("U1", "2")])
    doc = _divider_spec_doc()
    doc["acknowledged_unmodeled"] = ["U1"]
    d = deckmod.build(sch, _spec(tmp_path, doc), gnd="GND")
    hits = [f for f in d.warnings if f.code == "SIM_PART_SKIPPED"]
    assert [f.severity for f in hits] == [Severity.WARNING]
    assert "acknowledged" in hits[0].message
    # the acceptance is honest: the finding still names the measured node
    assert "MID" in hits[0].refs
    # no stale-ack note: the entry matched a real absent part
    assert not any(f.code == "SIM_UNMODELED_ACK_UNUSED" for f in d.warnings)


def test_acknowledgment_of_other_part_does_not_downgrade(tmp_path):
    # Acking U9 must not forgive U1 — and the unused entry is called out.
    u1 = _part("U1", None, lib="Amplifier:LM358")
    sch = _divider_sch(extra=[u1], extra_nets=[])
    sch.nets[1].members = sorted([*sch.nets[1].members, ("U1", "1")])
    sch.nets[2].members = sorted([*sch.nets[2].members, ("U1", "2")])
    doc = _divider_spec_doc()
    doc["acknowledged_unmodeled"] = ["U9"]
    d = deckmod.build(sch, _spec(tmp_path, doc), gnd="GND")
    skipped = [f for f in d.warnings if f.code == "SIM_PART_SKIPPED"]
    assert [f.severity for f in skipped] == [Severity.ERROR]
    stale = [f for f in d.warnings if f.code == "SIM_UNMODELED_ACK_UNUSED"]
    assert len(stale) == 1 and stale[0].severity == Severity.NOTE
    assert "U9" in stale[0].refs


def test_stale_acknowledgment_on_clean_deck_raises_note(tmp_path):
    # Every part modeled: the ack list is pure rot and must say so.
    doc = _divider_spec_doc()
    doc["acknowledged_unmodeled"] = ["U1"]
    d = deckmod.build(_divider_sch(), _spec(tmp_path, doc), gnd="GND")
    stale = [f for f in d.warnings if f.code == "SIM_UNMODELED_ACK_UNUSED"]
    assert len(stale) == 1
    assert "U1" in stale[0].message


def test_no_asserts_means_no_escalation(tmp_path):
    # Nothing is measured, so a skipped part is a warning, never an error.
    doc = _divider_spec_doc()
    doc["assert"] = []
    sch = _divider_sch(r2_params={"Sim.Device": "R", "Sim.Enable": "0"})
    d = deckmod.build(sch, _spec(tmp_path, doc), gnd="GND")
    sev = [f.severity for f in d.warnings if f.code == "SIM_PART_SKIPPED"]
    assert sev == [Severity.WARNING]


def test_deck_only_json_reports_parts_skipped(capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    sch = _divider_sch(r2_params={"Sim.Device": "R", "Sim.Enable": "0"})
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: sch)
    assert cli.main(["sim", "divider.kicad_sch", "--deck-only", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [p["designator"] for p in doc["parts_skipped"]] == ["R2"]
    assert doc["parts_skipped"][0]["nodes"] == ["0", "MID"]


def _fake_engine(monkeypatch, value: float):
    """Stub libngspice so the verdict logic can be tested without the engine."""
    from akcli.sim import engine as engine_mod
    monkeypatch.setattr(engine_mod, "available", lambda: "/fake/libngspice.dylib")
    monkeypatch.setattr(
        engine_mod, "run",
        lambda *a, **k: engine_mod.EngineResult(
            ok=True, meas_lines=[f"stdout vmid = {value:.6e}"]),
    )


def test_pass_is_suppressed_when_a_measured_series_part_was_skipped(
        tmp_path, capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    sch = _divider_sch(r2_params={"Sim.Device": "R", "Sim.Enable": "0"})
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: sch)
    # The measurement itself satisfies its bound — but R2 was not in the deck.
    _fake_engine(monkeypatch, 1.65)
    sim = _write_spec(tmp_path, _divider_spec_doc())
    assert cli.main(["sim", "divider.kicad_sch", "--sim", sim]) == 1
    out = capsys.readouterr().out
    assert "SIM_PART_SKIPPED" in out
    assert "FAIL" in out


def test_json_verdict_false_when_measured_series_part_was_skipped(
        tmp_path, capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    sch = _divider_sch(r2_params={"Sim.Device": "R", "Sim.Enable": "0"})
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: sch)
    _fake_engine(monkeypatch, 1.65)
    sim = _write_spec(tmp_path, _divider_spec_doc())
    assert cli.main(["sim", "divider.kicad_sch", "--sim", sim, "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert [p["designator"] for p in doc["parts_skipped"]] == ["R2"]


def test_complete_deck_still_passes(tmp_path, capsys, monkeypatch):
    # Control: nothing dropped -> the assert alone decides, and it passes.
    from akcli.commands import sim as sim_cmd
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: _divider_sch())
    _fake_engine(monkeypatch, 1.65)
    sim = _write_spec(tmp_path, _divider_spec_doc())
    assert cli.main(["sim", "divider.kicad_sch", "--sim", sim, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["parts_skipped"] == []


# --------------------------------------------------------------------------- #
# 4. op + assertions is rejected up front
# --------------------------------------------------------------------------- #
def test_explicit_op_analysis_on_an_assert_is_rejected(tmp_path):
    doc = _divider_spec_doc()
    doc["analyses"] = {"op": "", "tran": "10u 10m"}
    doc["assert"] = [{"name": "vmid", "meas": "FIND v(MID) AT=0",
                      "analysis": "op", "approx": "1.65"}]
    with pytest.raises(AkcliError) as ei:
        assertions.load(_write_spec(tmp_path, doc))
    assert ei.value.code == "SIM_OP_ASSERT_UNSUPPORTED"
    assert "dc" in ei.value.message


def test_op_only_spec_with_asserts_is_rejected(tmp_path):
    doc = _divider_spec_doc()
    doc["analyses"] = {"op": ""}
    with pytest.raises(AkcliError) as ei:
        assertions.load(_write_spec(tmp_path, doc))
    assert ei.value.code == "SIM_OP_ASSERT_UNSUPPORTED"


def test_op_analysis_without_asserts_is_still_allowed(tmp_path):
    doc = {"protocol_version": 1, "analyses": {"op": ""}, "assert": []}
    spec = assertions.load(_write_spec(tmp_path, doc))
    assert assertions.run_commands(spec) == ["op"]


def test_dc_sweep_assert_is_the_supported_replacement(tmp_path):
    doc = _divider_spec_doc()
    doc["analyses"] = {"dc": "Vsup 3.3 3.3 1"}
    doc["assert"] = [{"name": "vmid", "meas": "FIND v(MID) AT=3.3",
                      "analysis": "dc", "approx": "1.65", "tol": "0.02"}]
    spec = assertions.load(_write_spec(tmp_path, doc))
    assert assertions.run_commands(spec) == [
        "dc Vsup 3.3 3.3 1", "meas dc vmid FIND v(MID) AT=3.3",
    ]


def test_cli_op_assert_exits_2(tmp_path, capsys):
    doc = _divider_spec_doc()
    doc["analyses"] = {"op": ""}
    sim = _write_spec(tmp_path, doc)
    assert cli.main(["sim", "tests/fixtures/kicad/board_v8.kicad_sch",
                     "--sim", sim]) == 2
    assert "SIM_OP_ASSERT_UNSUPPORTED" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# live: the original "10k 1%" repro, through the real engine
# --------------------------------------------------------------------------- #
def test_deck_refuses_the_digit_separator_value(tmp_path):
    # "1_000" never reaches ngspice now: it would have been read as 1 ohm, and
    # the 10k/1_000 divider would have measured 3.3e-4 V with a PASS verdict.
    with pytest.raises(AkcliError) as ei:
        deckmod.build(_divider_sch("1_000"), _spec(tmp_path), gnd="GND")
    assert ei.value.code == "SIM_VALUE_INVALID"


def test_deck_emits_upper_case_value_as_canonical_spice(tmp_path):
    d = deckmod.build(_divider_sch("10K"), _spec(tmp_path), gnd="GND")
    assert "R2 MID 0 10k" in d.text


@_needs_ngspice
def test_live_upper_case_suffix_simulates_identically(tmp_path):
    """ngspice reads "10K" as 10k; so does the gate. Same divider, same volts.

    ``4K7`` is the stronger half: an upper-cased embedded decimal that used to
    be refused outright must now land on ngspice as 4.7 kΩ, so the 10k/4.7k
    divider off 3.3 V has to measure 3.3·4.7/14.7 = 1.055 V — proof the *number*
    (not just the string) survived the gate.
    """
    spec = _spec(tmp_path)

    def measure(raw: str) -> float:
        d = deckmod.build(_divider_sch(raw), spec, gnd="GND")
        with tempfile.TemporaryDirectory(prefix="akcli-sim-test-") as td:
            result = engine.run(d.text, assertions.run_commands(spec),
                                timeout=60.0, workdir=Path(td))
        assert result.ok, result.error
        _findings, values = assertions.evaluate(
            spec, assertions.parse_meas_output(result.meas_lines))
        return values["vmid"]

    lower, upper = measure("10k"), measure("10K")
    assert upper == pytest.approx(1.65, rel=0.02)
    assert upper == pytest.approx(lower, rel=1e-9)
    assert measure("4K7") == pytest.approx(3.3 * 4.7 / 14.7, rel=0.01)


@_needs_ngspice
def test_live_tolerance_token_divider_reads_half_the_rail(tmp_path):
    """The repro: "10k 1%" used to read 3.30 V (R2 seen as 10 ohms) and PASS."""
    spec = _spec(tmp_path)
    d = deckmod.build(_divider_sch("10k 1%"), spec, gnd="GND")
    with tempfile.TemporaryDirectory(prefix="akcli-sim-test-") as td:
        result = engine.run(d.text, assertions.run_commands(spec),
                            timeout=60.0, workdir=Path(td))
    assert result.ok, result.error
    findings, measured = assertions.evaluate(
        spec, assertions.parse_meas_output(result.meas_lines))
    assert measured["vmid"] == pytest.approx(1.65, rel=0.02)
    assert findings == []
