"""CLI-layer tests for `akcli sim` (deck emission, exit codes, live ngspice).

The offline tests cover the usage-error, ``--deck-only`` deck rendering,
``--gnd`` override, the unmodeled-warning path, the engine-missing exit-7
branch and the JSON envelope shape. The live tests (skipped when libngspice is
absent) drive the real engine end-to-end against the ``board_v8`` RC divider
and assert the measured node voltage and a written waveform file.
"""

from __future__ import annotations

import json

import pytest

from akcli import cli
from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.sim import engine

_BOARD = "tests/fixtures/kicad/board_v8.kicad_sch"
_HAVE_NGSPICE = engine.available() is not None
_needs_ngspice = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not installed on this machine"
)


def _write_sim(tmp_path, doc: dict):
    p = tmp_path / "sim.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _find_value(lines, name):
    import re
    pat = re.compile(re.escape(name) + r"\s*=\s*([-+0-9.eE]+)")
    for ln in lines:
        m = pat.search(ln)
        if m:
            return float(m.group(1))
    return None


# --------------------------------------------------------------------------- #
# usage / deck-only (offline)
# --------------------------------------------------------------------------- #
def test_sim_requires_sim_or_deck_only(capsys):
    # Neither --sim nor --deck-only is a usage error explaining both.
    assert cli.main(["sim", _BOARD]) == 2
    err = capsys.readouterr().err
    assert "--sim" in err and "--deck-only" in err


def test_sim_deck_only_stdout(capsys):
    assert cli.main(["sim", _BOARD, "--deck-only"]) == 0
    out = capsys.readouterr().out
    # RC divider fixture: +3V3 -> R1 -> MID -> R2 -> GND, C1 on MID.
    assert out.startswith("* akcli sim:")
    assert "R1 _3V3 MID 10k" in out
    assert "R2 MID 0 10k" in out
    assert "C1 MID 0 100n" in out
    assert out.rstrip().endswith(".end")


def test_sim_deck_only_out_file(tmp_path, capsys):
    out_file = tmp_path / "board.cir"
    assert cli.main(["sim", _BOARD, "--deck-only", "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert "R1 _3V3 MID 10k" in text
    # nothing on stdout when writing to a file; the notice goes to stderr
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert str(out_file) in captured.err


def test_sim_gnd_override_remaps_ground(capsys):
    # --gnd MID makes MID become node 0; GND becomes an ordinary node.
    assert cli.main(["sim", _BOARD, "--deck-only", "--gnd", "MID"]) == 0
    out = capsys.readouterr().out
    assert "R1 _3V3 0 10k" in out
    assert "R2 0 GND 10k" in out


def test_sim_no_ground_exits_6(tmp_path, capsys, monkeypatch):
    # A deck with no ground net is SIM_NO_GROUND -> op-list-class exit 6.
    assert cli.main(["sim", _BOARD, "--deck-only", "--gnd", "NOSUCHNET"]) == 6
    err = capsys.readouterr().err
    assert "SIM_NO_GROUND" in err


def test_sim_deck_only_json(capsys):
    assert cli.main(["sim", _BOARD, "--deck-only", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "R1 _3V3 MID 10k" in doc["deck"]
    assert len(doc["deck_sha"]) == 12
    assert doc["warnings"] == []
    assert doc["unmodeled"] == []


# --------------------------------------------------------------------------- #
# unmodeled-warning path (offline, synthetic schematic)
# --------------------------------------------------------------------------- #
def _synthetic_with_diode() -> Schematic:
    d1 = Component("D1", "Device:D", 0, 0, value="1N4148",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    r1 = Component("R1", "Device:R", 0, 0, value="1k",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    nets = [
        Net("IN", [("R1", "1")]),
        Net("MID", [("R1", "2"), ("D1", "1")]),
        Net("GND", [("D1", "2")], source_names=["GND"]),
    ]
    return Schematic("synthetic.kicad_sch", "kicad", [d1, r1], nets)


def test_sim_deck_only_unmodeled_warns_but_passes(capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: _synthetic_with_diode())
    # An unmodeled diode warns on stderr but never fails --deck-only.
    assert cli.main(["sim", "synthetic.kicad_sch", "--deck-only"]) == 0
    captured = capsys.readouterr()
    assert "unmodeled D1" in captured.out       # deck comment
    assert "SIM_UNMODELED" in captured.err       # warning line
    assert "R1 IN MID 1k" in captured.out


def test_sim_deck_only_unmodeled_json_lists_it(capsys, monkeypatch):
    from akcli.commands import sim as sim_cmd
    monkeypatch.setattr(sim_cmd, "_load_schematic", lambda _p: _synthetic_with_diode())
    assert cli.main(["sim", "synthetic.kicad_sch", "--deck-only", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["unmodeled"] == ["D1"]
    assert any(w["code"] == "SIM_UNMODELED" for w in doc["warnings"])


# --------------------------------------------------------------------------- #
# engine-missing (offline, monkeypatched)
# --------------------------------------------------------------------------- #
def test_sim_engine_missing_exit_7(tmp_path, capsys, monkeypatch):
    from akcli.sim import engine as engine_mod
    monkeypatch.setattr(engine_mod, "available", lambda: None)
    sim = _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "approx": "1.65", "tol": "0.02"}],
    })
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 7
    assert "NGSPICE_MISSING" in capsys.readouterr().err


def test_sim_bad_spec_exits_2(tmp_path, capsys):
    # A malformed sim.json is BAD_CONFIG -> usage exit 2 via cli.main.
    sim = _write_sim(tmp_path, {"protocol_version": 1, "assert": [{"bogus": 1}]})
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 2


# --------------------------------------------------------------------------- #
# item 4: an engine failure (unparsed deck) exits 7 with the error visible,
# even for a zero-assert spec that would otherwise "pass" trivially.
# --------------------------------------------------------------------------- #
def test_sim_engine_failure_exits_7_even_with_zero_asserts(tmp_path, capsys,
                                                            monkeypatch):
    from akcli.sim import engine as engine_mod
    monkeypatch.setattr(engine_mod, "available", lambda: "/fake/libngspice.dylib")
    monkeypatch.setattr(
        engine_mod, "run",
        lambda *a, **k: engine_mod.EngineResult(
            ok=False, error="engine exited with code 1: Error: circuit not parsed."),
    )
    sim = _write_sim(tmp_path, {
        "protocol_version": 1, "analyses": {"op": ""}, "assert": [],
    })
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 7
    err = capsys.readouterr().err
    assert "NGSPICE_FAILED" in err
    assert "circuit not parsed" in err


# --------------------------------------------------------------------------- #
# item 1: a diode is emitted in SPICE anode/cathode order derived from pin
# NAMES, not schematic pin numbers (real models stage, no stub).
# --------------------------------------------------------------------------- #
def _rectifier_sch() -> Schematic:
    # D4 with KiCad stock numbering (pin1='K' cathode, pin2='A' anode).
    d4 = Component("D4", "Device:D_Schottky", 0, 0,
                   pins=[Pin("1", "K", 0, 0, PinType.PASSIVE),
                         Pin("2", "A", 0, 0, PinType.PASSIVE)])
    r1 = Component("R1", "Device:R", 0, 0, value="100k",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    c1 = Component("C1", "Device:C", 0, 0, value="100n",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    nets = [
        Net("HP", [("D4", "2")], source_names=["HP"]),               # anode
        Net("PEAK", [("D4", "1"), ("R1", "1"), ("C1", "1")],
            source_names=["PEAK"]),                                  # cathode
        Net("GND", [("R1", "2"), ("C1", "2")], source_names=["GND"]),
    ]
    return Schematic("rect.kicad_sch", "kicad", [d4, r1, c1], nets)


def _rectifier_spec():
    from akcli.sim import assertions
    return assertions.SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vin", "node": "HP",
                  "value": "SIN(0 1 10k)"}],
        analyses={"tran": "2u 2m"},
        models={"D4": {"device": "D", "model_name": "DBAT",
                       "model_card": ".model DBAT D(IS=2.4e-8 N=1.05)"}},
    )


def test_deck_diode_uses_pin_name_anode_cathode_order():
    from akcli.sim import deck as deckmod
    d = deckmod.build(_rectifier_sch(), _rectifier_spec())
    # anode (pin2 -> HP) first, cathode (pin1 -> PEAK) second.
    assert "D4 HP PEAK DBAT" in d.text
    assert not any(f.code == "SIM_PIN_ORDER_ASSUMED" for f in d.warnings)


# --------------------------------------------------------------------------- #
# fit-diode --apply round-trip: a native Sim.Device=D + Sim.Params (exactly what
# `fit-diode --apply --write` stamps) must resolve into a *modeled*, working deck
# — the tool's own applied fit must not brick its own simulation.
# --------------------------------------------------------------------------- #
def _native_diode_sch() -> Schematic:
    # D4 carries the native Sim.* fields fit-diode --apply writes: Sim.Device=D
    # + Sim.Params, NO Sim.Name (so a model must be synthesized).
    d4 = Component("D4", "Device:D_Schottky", 0, 0,
                   pins=[Pin("1", "K", 0, 0, PinType.PASSIVE),
                         Pin("2", "A", 0, 0, PinType.PASSIVE)],
                   parameters={"Sim.Device": "D",
                               "Sim.Params": "IS=2.4e-8 N=1.05"})
    r1 = Component("R1", "Device:R", 0, 0, value="100k",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    c1 = Component("C1", "Device:C", 0, 0, value="100n",
                   pins=[Pin("1", None, 0, 0, PinType.PASSIVE),
                         Pin("2", None, 0, 0, PinType.PASSIVE)])
    nets = [
        Net("HP", [("D4", "2")], source_names=["HP"]),               # anode
        Net("PEAK", [("D4", "1"), ("R1", "1"), ("C1", "1")],
            source_names=["PEAK"]),                                  # cathode
        Net("GND", [("R1", "2"), ("C1", "2")], source_names=["GND"]),
    ]
    return Schematic("native_rect.kicad_sch", "kicad", [d4, r1, c1], nets)


def _native_diode_spec():
    from akcli.sim import assertions
    return assertions.SimSpec(
        stimuli=[{"kind": "vsource", "name": "Vin", "node": "HP",
                  "value": "SIN(0 1 10k)"}],
        analyses={"tran": "2u 2m"},
    )


def test_deck_native_diode_sim_params_is_modeled():
    from akcli.sim import deck as deckmod
    d = deckmod.build(_native_diode_sch(), _native_diode_spec())
    # element references a synthesized model name; the matching card is injected.
    assert "D4 HP PEAK AKCLI_D4" in d.text
    assert ".model AKCLI_D4 D(IS=2.4e-8 N=1.05)" in d.text
    assert not any(f.code == "SIM_UNMODELED" for f in d.warnings)


@_needs_ngspice
def test_live_native_diode_sim_params_charges_positive(tmp_path):
    # The end-to-end proof of the round-trip fix: a bare Sim.Device=D + Sim.Params
    # simulates (the diode conducts, peak detector charges positive) instead of
    # emitting a modelless element ngspice rejects with 'circuit not parsed'.
    from akcli.sim import deck as deckmod, engine as engine_mod
    d = deckmod.build(_native_diode_sch(), _native_diode_spec())
    res = engine_mod.run(
        d.text, ["run", "meas tran vpk MAX v(PEAK)"],
        timeout=30, workdir=tmp_path,
    )
    assert res.ok, res.error or res.log
    vpk = _find_value(res.meas_lines, "vpk")
    assert vpk is not None and vpk > 0.1, f"diode did not conduct: {vpk!r}\n{res.log}"


@_needs_ngspice
def test_live_half_wave_rectifier_charges_positive(tmp_path):
    # The end-to-end proof of item 1: correct anode/cathode order => the peak
    # detector charges POSITIVE. Reversed polarity would drive it negative.
    from akcli.sim import deck as deckmod, engine as engine_mod
    d = deckmod.build(_rectifier_sch(), _rectifier_spec())
    res = engine_mod.run(
        d.text, ["run", "meas tran vpk MAX v(PEAK)", "meas tran vmin MIN v(PEAK)"],
        timeout=30, workdir=tmp_path,
    )
    assert res.ok, res.log
    vpk = _find_value(res.meas_lines, "vpk")
    assert vpk is not None, res.log
    assert vpk > 0.1, f"peak detector did not charge positive: vpk={vpk!r}\n{res.log}"


# --------------------------------------------------------------------------- #
# live engine (skipped without libngspice)
# --------------------------------------------------------------------------- #
def _divider_sim(tmp_path) -> str:
    return _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "approx": "1.65", "tol": "0.02"}],
    })


@_needs_ngspice
def test_sim_live_divider_passes(tmp_path, capsys):
    sim = _divider_sim(tmp_path)
    # Equal 10k/10k divider off 3.3 V settles MID at ~1.65 V.
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 0
    out = capsys.readouterr().out
    assert "vmid" in out and "PASS" in out
    assert "measured values:" in out
    # metadata header carries the engine lib path + short deck sha1
    assert "engine:" in out and "deck sha1:" in out


@_needs_ngspice
def test_sim_live_json_envelope(tmp_path, capsys):
    sim = _divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert len(doc["deck_sha"]) == 12
    assert doc["engine"].endswith(".dylib") or "ngspice" in doc["engine"]
    assert abs(doc["measured"]["vmid"] - 1.65) < 0.05
    assert doc["findings"] == []


@_needs_ngspice
def test_sim_live_assertion_failure_exits_1(tmp_path, capsys):
    # Same run, an impossible bound -> SIM_ASSERT_FAIL -> exit 1.
    sim = _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "gt": "3.0"}],
    })
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    # --exit-zero forces a clean exit even with a failed assertion.
    assert cli.main(["sim", _BOARD, "--sim", sim, "--exit-zero"]) == 0


@_needs_ngspice
def test_sim_live_multi_analysis_tran_and_ac(tmp_path, capsys):
    # tran + ac in one spec, one assert each: a single 'run' would run only tran
    # and the ac meas would error out the whole run (exit 7). Per-analysis
    # dispatch makes both pass end-to-end.
    sim = _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "dc 3.3 ac 1"}],
        "analyses": {"tran": "10u 10m", "ac": "dec 10 100 100k"},
        "assert": [
            {"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
             "approx": "1.65", "tol": "0.05"},
            {"name": "gain", "meas": "FIND vm(MID) AT=1k", "analysis": "ac",
             "ge": "0"},
        ],
    })
    assert cli.main(["sim", _BOARD, "--sim", sim]) == 0
    out = capsys.readouterr().out
    assert "vmid" in out and "gain" in out
    assert "FAIL" not in out


@_needs_ngspice
def test_sim_live_wave_writes_file(tmp_path, capsys):
    sim = _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "options": {"wave_vectors": ["v(MID)"]},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "approx": "1.65", "tol": "0.02"}],
    })
    wave = tmp_path / "wave.csv"
    assert cli.main(["sim", _BOARD, "--sim", sim, "--wave", str(wave)]) == 0
    assert wave.exists() and wave.stat().st_size > 0
    # --wave now emits the tidy CSV from wave.rewrite_wrdata: a single 'time'
    # column + one verbatim column per options.wave_vectors entry.
    lines = wave.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "time,v(MID)"
    # every data row is a clean 2-field CSV (no repeated scale column).
    assert all(len(row.split(",")) == 2 for row in lines[1:] if row)


@_needs_ngspice
def test_sim_live_wave_path_with_spaces(tmp_path):
    # item 11: a --wave path containing spaces must still produce the file
    # (ngspice would split an unquoted path; we wrdata to a fixed name + move).
    sim = _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vsup",
                     "node": "+3V3", "node2": "0", "value": "3.3"}],
        "analyses": {"tran": "10u 10m"},
        "options": {"wave_vectors": ["v(MID)"]},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=5m to=10m",
                    "approx": "1.65", "tol": "0.02"}],
    })
    spaced = tmp_path / "has space"
    spaced.mkdir()
    wave = spaced / "out wave.csv"
    assert cli.main(["sim", _BOARD, "--sim", sim, "--wave", str(wave)]) == 0
    assert wave.exists() and wave.stat().st_size > 0


# --------------------------------------------------------------------------- #
# measured-value table: a two-sided window shows BOTH bounds (offline)
# --------------------------------------------------------------------------- #
def test_bound_desc_two_sided_window_shows_both_bounds():
    from akcli.commands import sim as sim_cmd
    assert sim_cmd._bound_desc({"ge": 3.0, "le": 3.6}) == ">= 3 & <= 3.6"
    assert sim_cmd._bound_desc({"gt": 0.35}) == "> 0.35"
    assert sim_cmd._bound_desc({"approx": 1.65, "tol": 0.02}) == "~1.65 (tol 2%)"


def test_measured_table_window_fail_shows_the_upper_half_too():
    # Value 3.8 fails the 'le 3.6' side; the table must not print only '>= 3'
    # next to a FAIL verdict (the earlier first-key-wins bug).
    from akcli.commands import sim as sim_cmd
    from akcli import report as _report

    class _Spec:
        asserts = [{"name": "v33", "meas": "MAX v(x)", "ge": 3.0, "le": 3.6}]

    findings = [_report.Finding(
        "SIM_ASSERT_FAIL", _report.Severity.ERROR,
        "v33 = 3.8 violates <= 3.6", refs=["v33"])]
    table = sim_cmd._measured_table(_Spec(), {"v33": 3.8}, findings)
    assert ">= 3" in table and "<= 3.6" in table
    assert "FAIL" in table


# --------------------------------------------------------------------------- #
# fit-diode mode ('akcli sim fit-diode ...') — offline, no ngspice needed
# --------------------------------------------------------------------------- #
def test_fit_diode_single_point_text(capsys):
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m"]) == 0
    out = capsys.readouterr().out
    assert ".model DFIT D(" in out
    assert "IS=" in out and "N=1.0500" in out
    assert "Sim.Params:" in out


def test_fit_diode_json_envelope(capsys):
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["name"] == "DFIT"
    assert doc["model_card"].startswith(".model DFIT D(")
    assert abs(doc["params"]["N"] - 1.05) < 1e-9
    assert doc["params"]["IS"] > 0
    # 'Sim.Params' mirrors the model card's inner parameters.
    assert "IS=" in doc["sim_params"] and "N=" in doc["sim_params"]


def test_fit_diode_rs_and_cjo_and_name(capsys):
    # A high-current rs-point solves RS; --cjo adds CJO; --name renames the card.
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m",
                     "--rs-point", "1.0@1", "--cjo", "50p",
                     "--name", "DBAT", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["name"] == "DBAT"
    assert doc["model_card"].startswith(".model DBAT D(")
    assert doc["params"]["RS"] > 0
    assert doc["params"]["CJO"] == 50e-12
    assert "RS=" in doc["sim_params"] and "CJO=" in doc["sim_params"]


def test_fit_diode_requires_a_point(capsys):
    assert cli.main(["sim", "fit-diode"]) == 2
    assert "--point" in capsys.readouterr().err


def test_fit_diode_bad_point_format_exits_2(capsys):
    # No '@' separator is a usage error naming the offending token.
    assert cli.main(["sim", "fit-diode", "--point", "0.3"]) == 2
    err = capsys.readouterr().err
    assert "V@I" in err and "0.3" in err


def test_fit_diode_apply_dry_run_prints_oplist(capsys):
    # --apply without --write prints the op-list it WOULD apply (nothing written).
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m",
                     "--apply", _BOARD, "--designator", "R1"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert '"set_component_parameters"' in out
    assert '"Sim.Device": "D"' in out
    assert '"designator": "R1"' in out
    assert "--write" in out


def test_fit_diode_apply_needs_designator(capsys):
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m",
                     "--apply", _BOARD]) == 2
    assert "--designator" in capsys.readouterr().err


def test_fit_diode_apply_write_commits(tmp_path, capsys):
    # --write commits the Sim.* fields through the KiCad writer with a .bak.
    import shutil as _sh
    target = tmp_path / "board.kicad_sch"
    _sh.copy(_BOARD, target)
    assert cli.main(["sim", "fit-diode", "--point", "0.3@1m",
                     "--apply", str(target), "--designator", "R1",
                     "--write"]) == 0
    text = target.read_text(encoding="utf-8")
    assert "Sim.Params" in text and "Sim.Device" in text
    assert (tmp_path / "board.kicad_sch.bak").exists()
    assert "applied" in capsys.readouterr().out
