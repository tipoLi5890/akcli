"""`akcli gate` — iteration-weight four-check bundle (check + intent +
sch-pcb + bom-lock), offline by default.

Mirrors ``tests/test_release_preflight.py``'s patterns (a tmp-path project +
CLI invocations through ``akcli.cli.main``), plus the bom-lock-specific
offline/live split and the config-fallback surface that is unique to
``gate`` (``[paths].schematic``/``pcb``/``intent``/``bom_lock``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.parts import search as parts_search
from akcli.readers import kicad as kreader

# A generic two-resistor divider, net names RAIL_A/SIG_N (neither matches the
# classify.POWER_NAMES/GROUND_NAMES vocabulary, so `check`'s power detector
# stays silent) with proper `(instances ...)` blocks so the `integrity`
# check — part of `gate`'s FULL default check set, unlike `release
# preflight`'s narrower erc/power/bom/nets tuple — is clean too. R1 carries
# an LCSC id for the bom-lock gate; R2 deliberately does not (a normal,
# not-yet-sourced BOM line).
_ROOT = "44444444-4444-4444-4444-444444444444"
_SCH = f"""(kicad_sch (version 20230121) (generator eeschema)
  (uuid {_ROOT})
  (paper "A4")
  (lib_symbols
    (symbol "Device:R" (pin_numbers hide) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 101.6 105.41) (xy 101.6 113.03)))
  (label "SIG_N" (at 101.6 109.22 0))
  (wire (pts (xy 101.6 97.79) (xy 76.2 97.79)))
  (wire (pts (xy 76.2 97.79) (xy 76.2 120.65)))
  (wire (pts (xy 76.2 120.65) (xy 101.6 120.65)))
  (label "RAIL_A" (at 76.2 109.22 0))
  (symbol (lib_id "Device:R") (at 101.6 101.6 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555551)
    (property "Reference" "R1" (at 104 101.6 0))
    (property "Value" "10k" (at 106 101.6 0))
    (property "Footprint" "Resistor_SMD:R_0402" (at 101.6 101.6 0))
    (property "LCSC" "C11111" (at 101.6 101.6 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000001))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000002))
    (instances
      (project "gate_test"
        (path "/{_ROOT}" (reference "R1") (unit 1)))))
  (symbol (lib_id "Device:R") (at 101.6 116.84 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555552)
    (property "Reference" "R2" (at 104 116.84 0))
    (property "Value" "10k" (at 106 116.84 0))
    (property "Footprint" "Resistor_SMD:R_0402" (at 101.6 116.84 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000003))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000004))
    (instances
      (project "gate_test"
        (path "/{_ROOT}" (reference "R2") (unit 1)))))
)"""

_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (net 0 "")
  (net 1 "SIG_N")
  (net 2 "RAIL_A")
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu") (at 50 50 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "RAIL_A"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG_N")))
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu") (at 60 50 0)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG_N"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "RAIL_A")))
)"""


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "x.kicad_sch").write_text(_SCH, encoding="utf-8")
    (tmp_path / "x.kicad_pcb").write_text(_PCB, encoding="utf-8")
    return tmp_path


def _part(lcsc, stock=5000, price=0.01, basic=True):
    return parts_search.Part(lcsc=lcsc, mpn="X" + lcsc,
                             description="10k 0402", package="0402",
                             stock=stock, price=price, basic=basic,
                             datasheet=None, category="", attributes={})


def _write_matching_lock(proj_dir) -> None:
    """A ``bom.lock.json`` frozen from the SAME schematic ``gate`` will read.

    Uses ``bom_jlc.check``'s injectable ``get``/``find`` params directly —
    no monkeypatching of ``akcli.parts.search`` module state, so building the
    lock never disturbs a caller's own patches or the conftest network guard.
    """
    from akcli.parts import bom_jlc

    sch = kreader.read_sch(str(proj_dir / "x.kicad_sch"))
    lines = bom_jlc.check(sch, get=lambda c: _part(c), find=lambda q, **k: [])
    lock = bom_jlc.make_lock(lines, qty=1)
    (proj_dir / "bom.lock.json").write_text(json.dumps(lock), encoding="utf-8")


def _write_intent(proj_dir) -> None:
    """A passing intent doc snapshotted straight from the fixture's netlist."""
    from akcli.checks import intent as intent_mod

    sch = kreader.read_sch(str(proj_dir / "x.kicad_sch"))
    doc = intent_mod.snapshot(sch)
    (proj_dir / "intent.json").write_text(json.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------------- #
# a genuinely clean run: every gate PASSes
# --------------------------------------------------------------------------- #
def test_gate_all_pass(proj, capsys, monkeypatch):
    _write_intent(proj)
    _write_matching_lock(proj)
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: _part(lcsc))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--pcb", str(proj / "x.kicad_pcb"),
                "--intent", str(proj / "intent.json"),
                "--lock", str(proj / "bom.lock.json"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["check"]["status"] == "pass"
    assert by["intent"]["status"] == "pass"
    assert by["sch-pcb"]["status"] == "pass"
    assert by["bom-lock"]["status"] == "pass"
    assert by["bom-lock"].get("unverified", 0) == 0
    assert by["bom-lock"]["offline"] is True    # --live not given


# --------------------------------------------------------------------------- #
# --json envelope shape
# --------------------------------------------------------------------------- #
def test_gate_json_envelope_shape(proj, capsys):
    code = main(["gate", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["schema_version"] == "1"
    assert doc["result"] in ("PASS", "FAIL")
    assert isinstance(doc["gates"], list)
    assert {g["gate"] for g in doc["gates"]} == {
        "check", "intent", "sch-pcb", "bom-lock"}
    for g in doc["gates"]:
        assert g["status"] in ("pass", "fail", "skipped")
        assert "findings" in g
    # near-preflight shape, but no release manifest machinery
    assert "inputs" not in doc
    assert "git" not in doc


# --------------------------------------------------------------------------- #
# each gate independently skipped-with-reason when its input is absent
# --------------------------------------------------------------------------- #
def test_gate_skips_each_gate_independently(proj, capsys):
    code = main(["gate", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["intent"]["status"] == "skipped"
    assert "[paths].intent" in by["intent"]["reason"]
    assert by["sch-pcb"]["status"] == "skipped"
    assert "[paths].pcb" in by["sch-pcb"]["reason"]
    assert by["bom-lock"]["status"] == "skipped"
    assert "[paths].bom_lock" in by["bom-lock"]["reason"]
    # check always runs (no input beyond the schematic itself to be absent)
    assert by["check"]["status"] in ("pass", "fail")
    assert code in (0, 1)


# --------------------------------------------------------------------------- #
# missing schematic anywhere -> structured USAGE error naming the config key
# --------------------------------------------------------------------------- #
def test_gate_missing_schematic_is_structured_usage_error(tmp_path, capsys):
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        code = main(["gate", "--json"])
    finally:
        os.chdir(cwd)
    doc = json.loads(capsys.readouterr().out)
    assert code == 2
    assert doc["error"]["code"] == "USAGE"
    assert "[paths].schematic" in doc["error"]["message"]


# --------------------------------------------------------------------------- #
# zero-arg config-driven run: [paths].schematic/pcb/intent/bom_lock
# --------------------------------------------------------------------------- #
def test_gate_zero_arg_config_driven(proj, capsys, monkeypatch):
    _write_intent(proj)
    _write_matching_lock(proj)
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: _part(lcsc))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    (proj / "akcli.toml").write_text(
        '[paths]\n'
        'schematic = "x.kicad_sch"\n'
        'pcb       = "x.kicad_pcb"\n'
        'intent    = "intent.json"\n'
        'bom_lock  = "bom.lock.json"\n',
        encoding="utf-8")

    code = main(["gate", "-C", str(proj / "akcli.toml"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"
    by = {g["gate"]: g for g in doc["gates"]}
    for name in ("check", "intent", "sch-pcb", "bom-lock"):
        assert by[name]["status"] == "pass"


# --------------------------------------------------------------------------- #
# explicit flags beat config-resolved paths
# --------------------------------------------------------------------------- #
def test_gate_flag_overrides_beat_config(proj, capsys, monkeypatch):
    _write_intent(proj)
    _write_matching_lock(proj)
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: _part(lcsc))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    # config points every key at a nonexistent file — if `gate` used these,
    # the run would blow up (FILE_NOT_FOUND); the CLI flags must win instead.
    (proj / "akcli.toml").write_text(
        '[paths]\n'
        'schematic = "does-not-exist.kicad_sch"\n'
        'pcb       = "does-not-exist.kicad_pcb"\n'
        'intent    = "does-not-exist.json"\n'
        'bom_lock  = "does-not-exist.json"\n',
        encoding="utf-8")

    code = main(["gate", str(proj / "x.kicad_sch"),
                "-C", str(proj / "akcli.toml"),
                "--pcb", str(proj / "x.kicad_pcb"),
                "--intent", str(proj / "intent.json"),
                "--lock", str(proj / "bom.lock.json"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"


# --------------------------------------------------------------------------- #
# worst-of exit codes
# --------------------------------------------------------------------------- #
def test_gate_worst_of_exit_fails_on_schpcb_mismatch(proj, capsys):
    bad_pcb = _PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG_N"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "RAIL_A"))')
    (proj / "bad.kicad_pcb").write_text(bad_pcb, encoding="utf-8")

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--pcb", str(proj / "bad.kicad_pcb"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    assert doc["result"] == "FAIL"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sch-pcb"]["status"] == "fail"
    assert by["check"]["status"] == "pass"          # the OTHER gates stay honest
    assert any(f["code"].startswith("SCHPCB_NET") for f in by["sch-pcb"]["findings"])


def test_gate_worst_of_exit_ok_when_all_pass_or_skipped(proj, capsys):
    code = main(["gate", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["check"]["status"] == "pass"
    assert by["intent"]["status"] == "skipped"
    assert by["sch-pcb"]["status"] == "skipped"
    assert by["bom-lock"]["status"] == "skipped"


# --------------------------------------------------------------------------- #
# waiver interaction on the check gate (board_v8 genuinely trips
# POWER_RAIL_NO_SOURCE; a config [[waiver]] must demote it and flip the gate)
# --------------------------------------------------------------------------- #
def test_gate_check_waiver_flips_fail_to_pass(tmp_path, capsys):
    src = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    sch_path = tmp_path / "board_v8.kicad_sch"
    shutil.copy(src, sch_path)

    code = main(["gate", str(sch_path), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert code == 1
    assert by["check"]["status"] == "fail"
    assert any(f["code"] == "POWER_RAIL_NO_SOURCE" for f in by["check"]["findings"])

    cfg = tmp_path / "akcli.toml"
    cfg.write_text(
        '[[waiver]]\ncode = "POWER_RAIL_NO_SOURCE"\nseverity = "note"\n'
        'reason = "test: board_v8 power ports are net-naming only, by design"\n',
        encoding="utf-8")

    code = main(["gate", str(sch_path), "-C", str(cfg), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert code == 0
    assert by["check"]["status"] == "pass"
    # visible-but-demoted, never silently dropped
    assert any(f["code"] == "POWER_RAIL_NO_SOURCE" for f in by["check"]["findings"])


# --------------------------------------------------------------------------- #
# bom-lock: offline by default (no network), --live opts in, honest degrade
# --------------------------------------------------------------------------- #
def test_gate_bomlock_default_offline_never_touches_network(proj, capsys):
    # AKCLI_JLC_CACHE=off (conftest autouse) guarantees a cache miss; NOT
    # monkeypatching parts_search.get/search means a real network attempt
    # would raise via the conftest `_no_network` guard (_default_opener).
    # Completing without that RuntimeError IS the proof no network was hit.
    _write_matching_lock(proj)

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--lock", str(proj / "bom.lock.json"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["bom-lock"]["offline"] is True
    assert by["bom-lock"].get("unverified", 0) >= 1
    assert "note" in by["bom-lock"]
    assert code in (0, 1)


def test_gate_bomlock_live_plumbs_offline_false(proj, monkeypatch, capsys):
    _write_matching_lock(proj)
    seen: dict = {}

    def spy_get(lcsc, **kw):
        seen["offline"] = kw.get("offline")
        return _part(lcsc)

    monkeypatch.setattr(parts_search, "get", spy_get)
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--lock", str(proj / "bom.lock.json"), "--live", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert seen["offline"] is False
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["bom-lock"]["offline"] is False
    assert by["bom-lock"]["status"] == "pass"
    assert by["bom-lock"].get("unverified", 0) == 0
    assert code == 0


def test_gate_bomlock_default_plumbs_offline_true(proj, monkeypatch, capsys):
    seen: dict = {}

    def spy_get(lcsc, **kw):
        seen["offline"] = kw.get("offline")
        return _part(lcsc)

    _write_matching_lock(proj)
    monkeypatch.setattr(parts_search, "get", spy_get)
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])

    main(["gate", str(proj / "x.kicad_sch"),
         "--lock", str(proj / "bom.lock.json"), "--json"])
    capsys.readouterr()
    assert seen["offline"] is True


def test_gate_bomlock_skipped_without_lock(proj, capsys):
    code = main(["gate", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["bom-lock"]["status"] == "skipped"
    assert code == 0


# --------------------------------------------------------------------------- #
# --fail-on plumbing
# --------------------------------------------------------------------------- #
def test_gate_fail_on_never_forces_exit_zero(proj, capsys):
    bad_pcb = _PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG_N"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "RAIL_A"))')
    (proj / "bad.kicad_pcb").write_text(bad_pcb, encoding="utf-8")

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--pcb", str(proj / "bad.kicad_pcb"),
                "--fail-on", "never", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    # the report stays honest even though the exit code was overridden
    assert doc["result"] == "FAIL"


def test_gate_exit_zero_alias_forces_exit_zero(proj, capsys):
    bad_pcb = _PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG_N"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "RAIL_A"))')
    (proj / "bad.kicad_pcb").write_text(bad_pcb, encoding="utf-8")

    code = main(["gate", str(proj / "x.kicad_sch"),
                "--pcb", str(proj / "bad.kicad_pcb"), "--exit-zero"])
    assert code == 0


# --------------------------------------------------------------------------- #
# intent gate PASS
# --------------------------------------------------------------------------- #
def test_gate_intent_gate_pass(proj, capsys):
    _write_intent(proj)
    code = main(["gate", str(proj / "x.kicad_sch"),
                "--intent", str(proj / "intent.json"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["intent"]["status"] == "pass"
    assert code == 0
