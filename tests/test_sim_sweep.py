"""CLI-layer tests for ``akcli sim --sweep`` (corner matrices).

The offline tests cover the usage guards (``--sweep`` with ``--deck-only`` or
``--wave``, a spec-less run, an unknown sweep name, and the 64-corner cap) that
all fail before the engine is ever consulted. The live tests (skipped without
libngspice) drive a real 3-corner RC sweep on the ``board_v8`` divider and prove
the measured node magnitude shifts monotonically as ``R1`` grows, plus the JSON
``corners`` envelope, a failing-corner exit 1, and a ``temp`` sweep.
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


def _write_sim(tmp_path, doc: dict) -> str:
    p = tmp_path / "sim.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _ac_divider_sim(tmp_path, bound: dict | None = None) -> str:
    # 1 V AC on +3V3; measure |v(MID)| at 1 kHz. The divider ratio (and the cap
    # shunt) fall as R1 grows, so vmid decreases monotonically across corners.
    assertion = {"name": "vmid", "meas": "FIND vm(MID) AT=1k"}
    assertion.update(bound or {"ge": "0"})
    return _write_sim(tmp_path, {
        "protocol_version": 1,
        "stimuli": [{"kind": "vsource", "name": "Vac",
                     "node": "+3V3", "node2": "0", "value": "AC 1"}],
        "analyses": {"ac": "dec 10 100 100k"},
        "assert": [assertion],
    })


# --------------------------------------------------------------------------- #
# usage guards (offline — fail before the engine is touched)
# --------------------------------------------------------------------------- #
def test_sweep_with_deck_only_is_usage_error(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim, "--deck-only",
                     "--sweep", "R1=10k,20k"]) == 2
    assert "--deck-only" in capsys.readouterr().err


def test_sweep_needs_a_sim_spec(capsys):
    assert cli.main(["sim", _BOARD, "--sweep", "R1=10k,20k"]) == 2
    assert "--sim" in capsys.readouterr().err


def test_sweep_with_wave_is_usage_error(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim, "--sweep", "R1=10k,20k",
                     "--wave", str(tmp_path / "w.csv")]) == 2
    assert "--wave" in capsys.readouterr().err


def test_sweep_unknown_name_is_usage_error(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "RNOPE=1,2,3"]) == 2
    err = capsys.readouterr().err
    assert "RNOPE" in err and "temp" in err


def test_sweep_bad_format_is_usage_error(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim, "--sweep", "R1-10k"]) == 2
    assert "NAME=v1" in capsys.readouterr().err


def test_sweep_corner_cap_exceeded(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    nine = ",".join(str(i) for i in range(1, 10))     # 9 values
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", f"R1={nine}", "--sweep", f"R2={nine}"]) == 2
    assert "64" in capsys.readouterr().err            # 9*9 = 81 > 64


# --------------------------------------------------------------------------- #
# warnings surfaced in sweep mode (offline — engine mocked, no libngspice)
# --------------------------------------------------------------------------- #
def _pin(n):
    return Pin(n, None, 0, 0, PinType.PASSIVE)


def _mock_engine(monkeypatch, meas_lines):
    """Make _require_engine pass and _measure return canned measurements."""
    monkeypatch.setattr(engine, "available", lambda: "/fake/libngspice.dylib")
    monkeypatch.setattr(
        engine, "run",
        lambda deck, cmds, **kw: engine.EngineResult(
            ok=True, meas_lines=list(meas_lines)))


def _sweep_sim(tmp_path) -> str:
    return _write_sim(tmp_path, {
        "protocol_version": 1,
        "analyses": {"tran": "10u 1m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID) from=0 to=1m",
                    "ge": "0"}],
    })


def test_sweep_ignored_component_value_warns(monkeypatch, tmp_path, capsys):
    # R1 resolves via Sim.Params, so a component-value --sweep on it changes
    # nothing — every corner is identical. That must raise SIM_SWEEP_IGNORED
    # rather than silently produce a byte-identical matrix.
    from akcli.commands import sim as sim_cmd

    def _sch(_p):
        r1 = Component("R1", "Device:R", 0, 0, value="10k",
                       parameters={"Sim.Device": "R", "Sim.Params": "5k"},
                       pins=[_pin("1"), _pin("2")])
        r2 = Component("R2", "Device:R", 0, 0, value="10k",
                       pins=[_pin("1"), _pin("2")])
        nets = [Net("IN", [("R1", "1")], source_names=["IN"]),
                Net("MID", [("R1", "2"), ("R2", "1")], source_names=["MID"]),
                Net("GND", [("R2", "2")], source_names=["GND"])]
        return Schematic("s.kicad_sch", "kicad", [r1, r2], nets)

    monkeypatch.setattr(sim_cmd, "_load_schematic", _sch)
    _mock_engine(monkeypatch, ["stdout vmid = 1.0"])
    sim = _sweep_sim(tmp_path)
    tgt = tmp_path / "s.kicad_sch"
    tgt.write_text("(kicad_sch)")
    rc = cli.main(["sim", str(tgt), "--sim", sim, "--sweep", "R1=1k,2k",
                   "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(w["code"] == "SIM_SWEEP_IGNORED" for w in doc["warnings"])


def test_sweep_surfaces_deck_build_warnings(monkeypatch, tmp_path, capsys):
    # A cap-only floating node makes the builder auto-add rshunt; single-run mode
    # prints SIM_FLOATING_NODE/SIM_RSHUNT_ADDED, so sweep mode must too (never
    # mask a mis-wire in sign-off mode). A temp sweep keeps it out of the
    # SIM_SWEEP_IGNORED path.
    from akcli.commands import sim as sim_cmd

    def _sch(_p):
        r1 = Component("R1", "Device:R", 0, 0, value="10k",
                       pins=[_pin("1"), _pin("2")])
        r2 = Component("R2", "Device:R", 0, 0, value="10k",
                       pins=[_pin("1"), _pin("2")])
        c1 = Component("C1", "Device:C", 0, 0, value="100n",
                       pins=[_pin("1"), _pin("2")])
        nets = [Net("IN", [("R1", "1")], source_names=["IN"]),
                Net("MID", [("R1", "2"), ("R2", "1"), ("C1", "1")],
                    source_names=["MID"]),
                Net("GND", [("R2", "2")], source_names=["GND"]),
                Net("AUX", [("C1", "2")], source_names=["AUX"])]  # cap-only
        return Schematic("f.kicad_sch", "kicad", [r1, r2, c1], nets)

    monkeypatch.setattr(sim_cmd, "_load_schematic", _sch)
    _mock_engine(monkeypatch, ["stdout vmid = 1.0"])
    sim = _sweep_sim(tmp_path)
    tgt = tmp_path / "f.kicad_sch"
    tgt.write_text("(kicad_sch)")
    rc = cli.main(["sim", str(tgt), "--sim", sim, "--sweep", "temp=0,25",
                   "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    codes = {w["code"] for w in doc["warnings"]}
    assert "SIM_FLOATING_NODE" in codes
    # deduped: one entry per (code, message) even across the two corners
    fn = [w for w in doc["warnings"] if w["code"] == "SIM_FLOATING_NODE"]
    assert len(fn) == 1


# --------------------------------------------------------------------------- #
# live corner matrix (skipped without libngspice)
# --------------------------------------------------------------------------- #
@_needs_ngspice
def test_sweep_rc_monotonic_fc_shift(tmp_path, capsys):
    # 3-corner RC sweep: as R1 grows, |v(MID)| at 1 kHz strictly decreases.
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "R1=10k,20k,40k", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    corners = doc["corners"]
    assert [c["params"]["R1"] for c in corners] == ["10k", "20k", "40k"]
    vals = [c["measured"]["vmid"] for c in corners]
    assert vals[0] > vals[1] > vals[2], vals       # monotonic decrease
    assert doc["ok"] is True and all(c["ok"] for c in corners)


@_needs_ngspice
def test_sweep_text_table(tmp_path, capsys):
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "R1=10k,20k,40k"]) == 0
    out = capsys.readouterr().out
    assert "sweep: 3 corner(s)" in out
    assert "R1" in out and "vmid" in out and "verdict" in out
    assert out.count("PASS") == 3
    assert "3/3 passed" in out


@_needs_ngspice
def test_sweep_any_corner_failure_exits_1(tmp_path, capsys):
    # An impossible bound fails every corner -> exit 1 (--exit-zero forces 0).
    sim = _ac_divider_sim(tmp_path, bound={"gt": "1.0"})
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "R1=10k,20k"]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "R1=10k,20k", "--exit-zero"]) == 0


@_needs_ngspice
def test_sweep_temp_option(tmp_path, capsys):
    # A temp sweep injects '.option temp='; the resistive divider is temp-flat,
    # so all three corners measure the same value and pass.
    sim = _ac_divider_sim(tmp_path)
    assert cli.main(["sim", _BOARD, "--sim", sim,
                     "--sweep", "temp=0,25,60", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["params"]["temp"] for c in doc["corners"]] == ["0", "25", "60"]
    assert doc["ok"] is True
