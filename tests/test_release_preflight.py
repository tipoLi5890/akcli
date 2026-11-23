"""`akcli release preflight` — gate orchestration + traceable manifest."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.commands import release
from akcli.drivers import kicad_cli

# KiCad DRC ground truth: a minimal board + the REAL reports kicad-cli 10.0.4
# wrote for it, in English and in zh_TW (tests/fixtures/kicad/README.md).
DRC_FIX = Path(__file__).parent / "fixtures" / "kicad" / "drc_demo"
needs_cli = pytest.mark.skipif(kicad_cli.find() is None,
                               reason="kicad-cli not installed")

# the same known-good sch/pcb pair the sch-pcb tests use
_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid 44444444-4444-4444-4444-444444444444)
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
  (label "MID" (at 101.6 109.22 0))
  (wire (pts (xy 101.6 97.79) (xy 96.52 97.79)))
  (wire (pts (xy 96.52 97.79) (xy 96.52 120.65)))
  (wire (pts (xy 96.52 120.65) (xy 101.6 120.65)))
  (label "A" (at 96.52 109.22 0))
  (symbol (lib_id "Device:R") (at 101.6 101.6 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555551)
    (property "Reference" "R1" (at 104 101.6 0))
    (property "Value" "10k" (at 106 101.6 0))
    (property "Footprint" "Resistor_SMD:R_0402" (at 101.6 101.6 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000001))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000002))
    (instances (project "x"
      (path "/44444444-4444-4444-4444-444444444444" (reference "R1") (unit 1)))))
  (symbol (lib_id "Device:R") (at 101.6 116.84 0) (unit 1)
    (uuid 55555555-5555-5555-5555-555555555552)
    (property "Reference" "R2" (at 104 116.84 0))
    (property "Value" "10k" (at 106 116.84 0))
    (property "Footprint" "Resistor_SMD:R_0402" (at 101.6 116.84 0))
    (pin "1" (uuid 55555555-0000-0000-0000-000000000003))
    (pin "2" (uuid 55555555-0000-0000-0000-000000000004))
    (instances (project "x"
      (path "/44444444-4444-4444-4444-444444444444" (reference "R2") (unit 1)))))
)"""

_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (net 0 "")
  (net 1 "MID")
  (net 2 "A")
  (net 3 "B")
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu") (at 50 50 0)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID")))
  (footprint "Resistor_SMD:R_0402" (layer "F.Cu") (at 60 50 0)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A")))
)"""

_PROFILE = """
id = "test-profile"
vendor = "JLCPCB"
[source]
urls = ["https://example.com/rules"]
retrieved_at = "2026-07-14"
[via]
min_drill_mm = 0.30
"""

_ORDER = """
delivery_format = "single"
design_count = 1
rush = false
surface_finish = "HASL_LF"
via_covering = "tented"
board_material = "FR4"
copper_weight_oz = 1
"""


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "x.kicad_sch").write_text(_SCH)
    (tmp_path / "x.kicad_pcb").write_text(_PCB)
    (tmp_path / "profile.toml").write_text(_PROFILE)
    (tmp_path / "order.toml").write_text(_ORDER)
    return tmp_path


def test_preflight_pass_writes_manifest(proj, capsys):
    out = proj / "manifest.json"
    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(proj / "x.kicad_pcb"),
                 "--fab-profile", str(proj / "profile.toml"),
                 "--order", str(proj / "order.toml"),
                 "--out", str(out), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["result"] == "PASS"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sch-pcb"]["status"] == "pass"
    assert by["fab"]["status"] == "pass"
    assert by["order"]["status"] == "pass"
    assert by["intent"]["status"] == "skipped"       # skipped is explicit
    assert by["git"]["status"] == "skipped"          # tmp dir is not a repo
    # no .kicad_pro beside the schematic: an explicit skip, never a quiet pass
    assert by["project"]["status"] == "skipped"
    assert ".kicad_pro" in by["project"]["reason"]
    assert doc["inputs"]["sch"]["sha256"]
    assert json.loads(out.read_text())["result"] == "PASS"


def test_preflight_fails_on_schpcb_mismatch(proj, capsys):
    (proj / "x.kicad_pcb").write_text(_PCB.replace(
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "MID"))',
        '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "A"))'))
    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(proj / "x.kicad_pcb"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sch-pcb"]["status"] == "fail"
    assert any(f["code"].startswith("SCHPCB_NET")
               for f in by["sch-pcb"]["findings"])


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


def test_preflight_dirty_worktree_fails_unless_allowed(proj, capsys):
    _git("init", "-q", cwd=proj)
    _git("add", "-A", cwd=proj)
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init", cwd=proj)
    (proj / "x.kicad_sch").write_text(_SCH + "\n")   # dirty it

    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["git"]["status"] == "fail"
    assert doc["git"]["dirty"] is True

    code = main(["release", "preflight",
                 "--sch", str(proj / "x.kicad_sch"), "--allow-dirty", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert doc["git"]["dirty"] is True               # recorded, not hidden
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["git"].get("allow_dirty") is True


# --------------------------------------------------------------------------- #
# sim gate (opt-in via --sim; requested-but-unrunnable FAILS, never skips)
# --------------------------------------------------------------------------- #
def test_preflight_sim_gate_skipped_without_spec(proj, capsys):
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
          "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sim"]["status"] == "skipped"
    assert "--sim" in by["sim"]["reason"]


def test_preflight_sim_gate_fails_when_engine_missing(proj, capsys,
                                                      monkeypatch):
    import akcli.sim.engine as sim_engine
    monkeypatch.setattr(sim_engine, "available", lambda: None)
    spec = proj / "sim.json"
    spec.write_text(json.dumps({
        "protocol_version": 1, "analyses": {"op": ""}, "assert": [],
    }), encoding="utf-8")
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--sim", str(spec), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and doc["result"] == "FAIL"
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sim"]["status"] == "fail"
    assert by["sim"]["findings"][0]["code"] == "NGSPICE_MISSING"
    assert doc["inputs"]["sim"]["sha256"]      # the spec is manifest-bound


def _stub_sim_run(monkeypatch, deck_warnings):
    """Pretend the engine exists, the deck built, and every assert passed."""
    import types

    import akcli.commands.sim as sim_cmd
    import akcli.sim.deck as sim_deck
    import akcli.sim.engine as sim_engine

    monkeypatch.setattr(sim_engine, "available", lambda: "libngspice")
    d = types.SimpleNamespace(text="* deck\n.end\n", warnings=deck_warnings,
                              unmodeled=[], skipped=[])
    monkeypatch.setattr(sim_deck, "build", lambda sch, spec, **kw: d)
    monkeypatch.setattr(sim_cmd, "_measure",
                        lambda *a, **kw: ([], {}, True))


def _spec_file(proj) -> str:
    spec = proj / "sim.json"
    spec.write_text(json.dumps({
        "protocol_version": 1, "analyses": {"tran": "1u 1m"},
        "assert": [{"name": "vmid", "meas": "MAX v(MID)", "lt": "5"}],
    }), encoding="utf-8")
    return str(spec)


def test_preflight_sim_gate_blocks_on_deck_error(proj, capsys, monkeypatch):
    # A part absent from a measured node (deck ERROR) must fail the release
    # gate even when every assertion numerically passed — the numbers exclude
    # a series element. Deck WARNINGs stay out of the gate (any-WARNING-fails
    # here would make routine unmodeled parts a false red).
    from akcli.report import Finding, Severity
    _stub_sim_run(monkeypatch, [
        Finding("SIM_PART_SKIPPED", Severity.ERROR,
                "U1 is not in the deck but connects measured node(s) MID",
                refs=["U1", "MID"]),
        Finding("SIM_UNMODELED", Severity.WARNING,
                "U2: no SPICE model — omitted from deck", refs=["U2"]),
    ])
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--sim", _spec_file(proj), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sim"]["status"] == "fail"
    codes = [f["code"] for f in by["sim"]["findings"]]
    assert "SIM_PART_SKIPPED" in codes
    assert "SIM_UNMODELED" not in codes    # WARNING-level deck noise stays out


def test_preflight_sim_gate_waiver_demotes_deck_error(proj, capsys,
                                                      monkeypatch):
    # A config [[waiver]] can accept the known gap: the ERROR demotes to note
    # and the gate passes — same governance as check-gate waivers.
    from akcli.report import Finding, Severity
    _stub_sim_run(monkeypatch, [
        Finding("SIM_PART_SKIPPED", Severity.ERROR,
                "U1 is not in the deck but connects measured node(s) MID",
                refs=["U1", "MID"]),
    ])
    (proj / "akcli.toml").write_text(
        '[[waiver]]\ncode = "SIM_PART_SKIPPED"\nrefs = "U1"\n'
        'severity = "note"\nreason = "no vendor model; accepted"\n',
        encoding="utf-8")
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--sim", _spec_file(proj), "--json"])
    doc = json.loads(capsys.readouterr().out)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["sim"]["status"] == "pass"
    # visible-but-demoted, never silently dropped
    assert [f["code"] for f in by["sim"]["findings"]] == ["SIM_PART_SKIPPED"]
    assert code == 0


def test_preflight_sim_spec_missing_file_is_not_found(proj, capsys):
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--sim", str(proj / "nope.json")])
    assert code == 4


# --------------------------------------------------------------------------- #
# project gate — only when the release candidate really IS a KiCad project
# --------------------------------------------------------------------------- #
def _preflight(proj, capsys) -> dict:
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"), "--json"])
    return json.loads(capsys.readouterr().out)


def test_preflight_project_gate_passes_on_a_scaffolded_project(proj, capsys):
    assert main(["project", "init", str(proj), "--name", "x"]) == 0
    capsys.readouterr()
    doc = _preflight(proj, capsys)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["project"]["status"] == "pass"
    assert doc["inputs"]["project"]["sha256"]
    # library-audit is the gate that reports lib-table findings; the project
    # gate must not repeat them (with_lib_audit=False)
    assert not [f for f in by["project"]["findings"]
                if f["code"].startswith("LIB_")]


def test_preflight_project_gate_fails_on_a_broken_root_binding(proj, capsys):
    assert main(["project", "init", str(proj), "--name", "x"]) == 0
    capsys.readouterr()
    pro = proj / "x.kicad_pro"
    doc_json = json.loads(pro.read_text(encoding="utf-8"))
    doc_json["sheets"] = [["11111111-1111-4111-8111-111111111111", "Root"]]
    pro.write_text(json.dumps(doc_json), encoding="utf-8", newline="\n")
    doc = _preflight(proj, capsys)
    by = {g["gate"]: g for g in doc["gates"]}
    assert by["project"]["status"] == "fail"
    assert any(f["code"] == "PROJECT_ROOT_SHEET_MISMATCH"
               for f in by["project"]["findings"])
    assert doc["result"] == "FAIL"


# --------------------------------------------------------------------------- #
# fab gate — a bundled profile id is recorded as such
# --------------------------------------------------------------------------- #
_CLEAN_4L = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.0))
  (layers
    (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
)"""


def _gates(doc: dict) -> dict:
    return {g["gate"]: g for g in doc["gates"]}


def test_preflight_records_a_file_profile_with_its_hash(proj, capsys):
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
          "--pcb", str(proj / "x.kicad_pcb"),
          "--fab-profile", str(proj / "profile.toml"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["inputs"]["fab_profile"] == {
        "path": str(proj / "profile.toml"),
        "sha256": hashlib.sha256(
            (proj / "profile.toml").read_bytes()).hexdigest(),
        "id": "test-profile",
        "bundled": False,
    }


def test_preflight_accepts_a_bundled_profile_id(proj, capsys, monkeypatch):
    from akcli import fab

    profile, origin = fab.bundled_profiles()[0]
    (proj / "x.kicad_pcb").write_text(_CLEAN_4L, encoding="utf-8", newline="\n")
    argv = ["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
            "--pcb", str(proj / "x.kicad_pcb"),
            "--fab-profile", profile["id"], "--json"]

    monkeypatch.setattr(fab, "_today", lambda: fab.retrieved_at(profile))
    main(argv)
    doc = json.loads(capsys.readouterr().out)
    assert doc["inputs"]["fab_profile"] == {
        "path": None, "sha256": origin.sha256, "id": profile["id"],
        "bundled": True}
    assert _gates(doc)["fab"] == {"gate": "fab", "status": "pass",
                                  "findings": [], "worst_rank": 0}

    # Years later the same wheel still passes the gate, but says the profile
    # it judged against has aged: a NOTE in the manifest, never a failure.
    monkeypatch.setattr(fab, "_today", lambda: dt.date(2099, 1, 1))
    main(argv)
    fab_gate = _gates(json.loads(capsys.readouterr().out))["fab"]
    assert fab_gate["status"] == "pass"
    assert [(f["code"], f["severity"]) for f in fab_gate["findings"]] == [
        ("FAB_PROFILE_STALE", "note")]


def test_preflight_unknown_profile_id_lists_the_bundled_ones(proj, capsys):
    from akcli import fab

    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(proj / "x.kicad_pcb"),
                 "--fab-profile", "no-such-profile"])
    cap = capsys.readouterr()
    assert code == 2
    assert "BAD_CONFIG" in cap.err
    assert fab.bundled_profiles()[0][0]["id"] in cap.err


# --------------------------------------------------------------------------- #
# drc gate (opt-in via --drc; requested-but-unrunnable FAILS, never skips)
# --------------------------------------------------------------------------- #
def _drc_report(name: str = "drc_demo.drc.kicad10.json") -> dict:
    return json.loads((DRC_FIX / name).read_text(encoding="utf-8"))


def _drc_board(proj: Path) -> Path:
    """The DRC fixture board, copied beside the test project's schematic."""
    board = proj / "drc_demo.kicad_pcb"
    shutil.copyfile(DRC_FIX / "drc_demo.kicad_pcb", board)
    return board


def _fake_drc(monkeypatch, report, **extra) -> list[str]:
    """Replace the kicad-cli spawn with a canned driver result (in-process)."""
    calls: list[str] = []

    def drc(path: str) -> dict:
        calls.append(path)
        return {"tool": "kicad-cli", "command": "pcb drc", "exit_code": 0,
                "report": report, "stdout": "", "stderr": "", "error": None,
                "error_code": None, **extra}

    monkeypatch.setattr(kicad_cli, "drc", drc)
    return calls


def _preflight_drc(proj: Path, capsys, *extra: str) -> tuple[int, dict]:
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--pcb", str(_drc_board(proj)), "--drc", "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_preflight_drc_gate_is_skipped_unless_requested(proj, capsys,
                                                       monkeypatch):
    calls = _fake_drc(monkeypatch, _drc_report())
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
          "--pcb", str(proj / "x.kicad_pcb"), "--json"])
    gate = _gates(json.loads(capsys.readouterr().out))["drc"]
    assert gate["status"] == "skipped" and "--drc" in gate["reason"]

    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"), "--json"])
    gate = _gates(json.loads(capsys.readouterr().out))["drc"]
    assert gate["status"] == "skipped" and "--pcb" in gate["reason"]
    assert calls == []                      # KiCad is never run uninvited


def test_preflight_drc_without_a_board_is_a_usage_error(proj, capsys,
                                                        monkeypatch):
    calls = _fake_drc(monkeypatch, _drc_report())
    code = main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
                 "--drc", "--json", "--out", str(proj / "m.json")])
    cap = capsys.readouterr()
    assert code == 2
    assert "--drc" in cap.err and "--pcb" in cap.err
    assert not (proj / "m.json").exists()   # no manifest for a non-run
    assert calls == []


def test_preflight_drc_gate_fails_when_kicad_cli_is_missing(proj, capsys,
                                                            monkeypatch):
    # The discovery ladder's first rung: KICAD_CLI set but pointing nowhere is
    # an explicit "no kicad-cli" (never a silent fallback to PATH).
    monkeypatch.setenv("KICAD_CLI", str(proj / "no-such-kicad-cli"))
    code, doc = _preflight_drc(proj, capsys)
    assert code == 1 and doc["result"] == "FAIL"
    gate = _gates(doc)["drc"]
    assert gate["status"] == "fail"         # requested + unrunnable != skipped
    assert [(f["code"], f["severity"]) for f in gate["findings"]] == [
        ("KICAD_CLI_MISSING", "error")]
    assert "KICAD_CLI" in gate["findings"][0]["message"]
    assert doc["tool"]["kicad_cli"] is None
    assert doc["inputs"]["pcb"]["sha256"]   # the board is still manifest-bound


def test_preflight_drc_gate_reports_kicads_findings(proj, capsys, monkeypatch):
    calls = _fake_drc(monkeypatch, _drc_report())
    code, doc = _preflight_drc(proj, capsys)
    assert code == 1
    assert calls == [str(proj / "drc_demo.kicad_pcb")]
    gate = _gates(doc)["drc"]
    assert gate["status"] == "fail"
    assert [(f["code"], f["severity"]) for f in gate["findings"]] == [
        ("KICAD_DRC_VIOLATION", "error"),       # clearance
        ("KICAD_DRC_VIOLATION", "warning"),     # track_dangling
        ("KICAD_DRC_UNCONNECTED", "error"),     # R1.1 <-> C1.1
    ]
    msgs = [f["message"] for f in gate["findings"]]
    # KiCad's rule type leads every message; its own wording follows verbatim
    assert msgs[0].startswith("clearance: Clearance violation")
    assert "Track [GND] on F.Cu" in msgs[0] and "@(100.51,102)mm" in msgs[0]
    assert msgs[1].startswith("track_dangling: ")
    assert msgs[2].startswith("unconnected_items: ")
    assert "Pad 1 [+3V3] of R1 on F.Cu @(99.49,100)mm" in msgs[2]
    assert gate["summary"] == (
        "KiCad 10.0.4 DRC: 2 violation(s), 1 unconnected item(s); zone fills "
        "evaluated as saved (not refilled)")


def test_preflight_drc_summary_is_printed_in_text_mode(proj, capsys,
                                                      monkeypatch):
    _fake_drc(monkeypatch, _drc_report())
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
          "--pcb", str(_drc_board(proj)), "--drc"])
    out = capsys.readouterr().out
    assert "FAIL     drc (3 finding(s))" in out
    assert "zone fills evaluated as saved (not refilled)" in out
    assert "ERROR KICAD_DRC_UNCONNECTED: unconnected_items: " in out
    assert "WARNING KICAD_DRC_VIOLATION: track_dangling: " in out


def test_preflight_drc_gate_passes_a_clean_board(proj, capsys, monkeypatch):
    clean = {**_drc_report(), "violations": [], "unconnected_items": []}
    _fake_drc(monkeypatch, clean)
    _code, doc = _preflight_drc(proj, capsys)
    gate = _gates(doc)["drc"]
    assert gate["status"] == "pass" and gate["findings"] == []
    assert "0 violation(s), 0 unconnected item(s)" in gate["summary"]
    assert "not refilled" in gate["summary"]


@pytest.mark.parametrize("extra,want", [
    ({"report": None, "exit_code": 3,
      "error": "kicad-cli pcb drc exited 3: Failed to load board"},
     "KICAD_DRC_FAILED"),
    ({"report": None, "exit_code": None, "error_code": "KICAD_CLI_TIMEOUT",
      "error": "kicad-cli timed out after 600.0s"}, "KICAD_CLI_TIMEOUT"),
    # a JSON document that is not a DRC report is not a clean verdict either
    ({"report": {"source": "drc_demo.kicad_pcb"}}, "KICAD_DRC_FAILED"),
    ({"report": {"violations": [], "unconnected_items": None}},
     "KICAD_DRC_FAILED"),
])
def test_preflight_drc_gate_fails_without_a_usable_report(proj, capsys,
                                                          monkeypatch, extra,
                                                          want):
    extra = dict(extra)
    _fake_drc(monkeypatch, extra.pop("report", None), **extra)
    code, doc = _preflight_drc(proj, capsys)
    assert code == 1 and doc["result"] == "FAIL"
    gate = _gates(doc)["drc"]
    assert gate["status"] == "fail"
    assert [f["code"] for f in gate["findings"]] == [want]
    assert gate["findings"][0]["severity"] == "error"
    if extra.get("error"):
        assert extra["error"] in gate["findings"][0]["message"]


def test_preflight_drc_waivers_apply_by_rule_type_designator_and_net(
        proj, capsys, monkeypatch):
    _fake_drc(monkeypatch, _drc_report())
    (proj / "akcli.toml").write_text(
        # by KiCad rule type: every dangling-track warning, visibly demoted
        '[[waiver]]\ncode = "KICAD_DRC_VIOLATION"\nrefs = "track_dangling"\n'
        'severity = "note"\nreason = "test stub, removed at layout freeze"\n'
        # by net: the clearance pair involving SIG
        '[[waiver]]\ncode = "KICAD_DRC_VIOLATION"\nrefs = "SIG"\n'
        'severity = "off"\nreason = "guard trace, reviewed"\n'
        # by pin: the unrouted +3V3 connection at R1.1
        '[[waiver]]\ncode = "KICAD_DRC_UNCONNECTED"\nrefs = "R1.1"\n'
        'severity = "off"\nreason = "fitted by wire link"\n',
        encoding="utf-8", newline="\n")
    _code, doc = _preflight_drc(proj, capsys)
    gate = _gates(doc)["drc"]
    assert gate["status"] == "pass"
    # demoted stays visible; the note waiver matched first, so the dangling
    # SIG track is a note rather than dropped by the later SIG waiver
    assert [(f["code"], f["severity"]) for f in gate["findings"]] == [
        ("KICAD_DRC_VIOLATION", "note")]
    assert gate["findings"][0]["message"].startswith("track_dangling: ")
    # the summary still counts what KiCad reported, before any waiver
    assert "2 violation(s), 1 unconnected item(s)" in gate["summary"]


def test_preflight_drc_waiver_for_another_net_changes_nothing(proj, capsys,
                                                              monkeypatch):
    _fake_drc(monkeypatch, _drc_report())
    (proj / "akcli.toml").write_text(
        '[[waiver]]\ncode = "KICAD_DRC_*"\nrefs = "VBUS"\nseverity = "off"\n'
        'reason = "unrelated"\n', encoding="utf-8", newline="\n")
    _code, doc = _preflight_drc(proj, capsys)
    assert len(_gates(doc)["drc"]["findings"]) == 3


# ---- report -> findings: the parsing ground truth ---------------------------
def _findings(report: dict, board: Path = DRC_FIX / "drc_demo.kicad_pcb"):
    items, nets = release._board_items(board)
    return release._drc_findings(report, items, nets)


def _facts(findings) -> list:
    """Everything about a finding except its (localized) message."""
    return [(f.code, f.severity.value, f.refs, f.pos, f.anchors)
            for f in findings]


def test_drc_findings_carry_refs_positions_and_anchors():
    clearance, dangling, unconnected = _findings(_drc_report())

    assert clearance.code == "KICAD_DRC_VIOLATION"
    assert clearance.severity.value == "error"
    # rule type first, then what the item uuids resolve to in the board file
    assert clearance.refs == ["clearance", "GND", "SIG"]
    assert clearance.pos == (100.51, 102.0)
    assert clearance.anchors == [
        {"kind": "net", "id": "GND", "pos": (100.51, 102.0)},
        {"kind": "net", "id": "SIG", "pos": (103.0, 102.25)}]

    assert dangling.severity.value == "warning"
    assert dangling.refs == ["track_dangling", "SIG"]

    assert unconnected.code == "KICAD_DRC_UNCONNECTED"
    assert unconnected.severity.value == "error"
    assert unconnected.refs == ["unconnected_items", "R1", "R1.1", "+3V3",
                                "C1", "C1.1"]
    assert unconnected.pos == (99.49, 100.0)
    assert unconnected.anchors == [
        {"kind": "pin", "id": "R1.1", "pos": (99.49, 100.0)},
        {"kind": "pin", "id": "C1.1", "pos": (109.49, 100.0)}]


def test_drc_findings_do_not_depend_on_kicads_language():
    # Same board, KiCad's UI language set to zh_TW: every description is
    # translated ("Pad 1 [+3V3] of R1 on F.Cu" -> "1 上 [+3V3] 的焊盤 R1 F.Cu"),
    # and nothing the gate or a waiver keys on may move.
    en = _findings(_drc_report())
    zh = _findings(_drc_report("drc_demo.drc.kicad10.zh_TW.json"))
    assert _facts(en) == _facts(zh)
    assert [f.message for f in en] != [f.message for f in zh]
    assert all(f.message.split(":")[0] == g.message.split(":")[0]
               for f, g in zip(en, zh))            # same rule type up front


def test_board_items_index_the_fixture_by_uuid():
    items, nets = release._board_items(DRC_FIX / "drc_demo.kicad_pcb")
    u = "00000000-0000-4000-8000-000000000{}".format
    assert nets == {"GND", "+3V3", "SIG"}
    assert items[u("101")] == {"ref": "R1"}                     # footprint
    assert items[u("102")] == {"ref": "R1"}                     # its Reference
    assert items[u("111")] == {"ref": "R1", "pad": "1", "net": "+3V3"}
    assert items[u("212")] == {"ref": "C1", "pad": "2", "net": "GND"}
    assert items[u("402")] == {"net": "GND"}                    # (net 1) track
    assert items[u("404")] == {"net": "SIG"}
    assert u("301") not in items                                # the outline


def test_drc_items_without_a_file_uuid_fall_back_to_a_validated_net():
    # board.kicad_pcb gives its pads no uuid, so KiCad mints fresh ones at
    # every load and nothing resolves. These two entries are verbatim from a
    # real kicad-cli 10.0.4 run (zh_TW UI) on that board.
    board = DRC_FIX.parent / "board.kicad_pcb"
    report = {"coordinate_units": "mm", "violations": [], "unconnected_items": [
        {"description": "Missing connection between items",
         "severity": "error", "type": "unconnected_items", "items": [
             {"description": "2 上 [GND] 的焊盤 R1 F.Cu",
              "pos": {"x": 100.51, "y": 100.0},
              "uuid": "2dd87560-60f6-4fdf-b918-719848ed41b4"},
             {"description": "2 上 [GND] 的焊盤 C1 F.Cu",
              "pos": {"x": 120.0, "y": 99.49},
              "uuid": "fbea0c78-1ad8-4a64-b861-9bfa0211da7f"}]},
        # a bracketed token that is NOT one of the board's nets is not a net
        {"description": "Missing connection between items",
         "severity": "error", "type": "unconnected_items", "items": [
             {"description": "Pad 3 [<no net>] of J9 on F.Cu",
              "pos": {"x": 1.0, "y": 2.0},
              "uuid": "00000000-0000-0000-0000-00000000dead"}]},
    ]}
    known, unknown = _findings(report, board)
    assert known.refs == ["unconnected_items", "GND"]
    assert known.anchors == [
        {"kind": "net", "id": "GND", "pos": (100.51, 100.0)},
        {"kind": "net", "id": "GND", "pos": (120.0, 99.49)}]
    assert unknown.refs == ["unconnected_items"] and unknown.anchors == []
    assert unknown.pos == (1.0, 2.0)


@pytest.mark.parametrize("kicad,ours", [
    ("error", "error"), ("warning", "warning"), ("WARNING", "warning"),
    ("exclusion", "note"), ("info", "note"),
    # a severity KiCad may add later must keep blocking, not read as harmless
    ("catastrophic", "warning"), (None, "warning"),
])
def test_drc_severity_mapping(kicad, ours):
    report = {"violations": [{"type": "clearance", "severity": kicad,
                              "description": "x", "items": []}],
              "unconnected_items": []}
    (finding,) = release._drc_findings(report, {}, set())
    assert finding.severity.value == ours
    assert finding.message == "clearance: x" and finding.pos is None


def test_drc_tolerates_a_ragged_report():
    report = {"coordinate_units": "mils", "unconnected_items": [], "violations": [
        "not-an-entry",
        {"type": "silk_overlap", "severity": "warning",
         "items": ["junk", {"description": "Text", "pos": {"x": "bad"}},
                   {"description": "Segment", "pos": {"x": 5, "y": 6}}]}]}
    (finding,) = release._drc_findings(report, {}, set())
    assert finding.refs == ["silk_overlap"]
    # non-mm coordinates stay in the message, but anchor nothing in the board
    # frame (which is mm)
    assert finding.message == "silk_overlap:  — Text; Segment @(5,6)mils"
    assert finding.pos is None


@needs_cli
def test_preflight_drc_gate_end_to_end_with_real_kicad(proj, capsys):
    """The whole path: real kicad-cli, real report, same findings as the
    committed ground truth — and the project directory is left untouched."""
    shutil.copyfile(DRC_FIX / "drc_demo.kicad_pro", proj / "drc_demo.kicad_pro")
    _drc_board(proj)
    before = sorted(p.name for p in proj.iterdir())
    code, doc = _preflight_drc(proj, capsys)
    assert code == 1
    gate = _gates(doc)["drc"]
    assert [(f["code"], f["severity"], f["message"].split(":")[0])
            for f in gate["findings"]] == [
        ("KICAD_DRC_VIOLATION", "error", "clearance"),
        ("KICAD_DRC_VIOLATION", "warning", "track_dangling"),
        ("KICAD_DRC_UNCONNECTED", "error", "unconnected_items"),
    ]
    assert "zone fills evaluated as saved (not refilled)" in gate["summary"]
    assert doc["tool"]["kicad_cli"] == kicad_cli.version_text()
    assert sorted(p.name for p in proj.iterdir()) == before   # no .kicad_prl


# --------------------------------------------------------------------------- #
# manifest evidence: tool versions, per-gerber hashes, dirty-state hash
# --------------------------------------------------------------------------- #
def test_manifest_records_the_kicad_cli_version(proj, capsys, monkeypatch):
    monkeypatch.setattr(kicad_cli, "version_text", lambda: "10.0.4-test")
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    from akcli import __version__
    assert doc["tool"] == {"akcli": __version__, "kicad_cli": "10.0.4-test"}


def test_manifest_kicad_cli_is_null_when_undiscoverable(proj, capsys,
                                                        monkeypatch):
    monkeypatch.setenv("KICAD_CLI", str(proj / "no-such-kicad-cli"))
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert "kicad_cli" in doc["tool"] and doc["tool"]["kicad_cli"] is None


def test_manifest_hashes_every_gerber_file(proj, capsys):
    fab_dir = proj / "fab"
    fab_dir.mkdir()
    payload = {
        "b-F_Cu.gbr": b"%TF.FileFunction,Copper,L1,Top*%\n%MOMM*%\n"
                      b"%FSLAX46Y46*%\nM02*\n",
        "a-Edge_Cuts.gbr": b"%TF.FileFunction,Profile,NP*%\n%MOMM*%\n"
                           b"%FSLAX46Y46*%\nM02*\n",
        "c.drl": b"M48\nMETRIC\nT1C0.800\n%\nT1\nX1.00Y1.00\nM30\n",
    }
    for name, data in payload.items():
        (fab_dir / name).write_bytes(data)
    (fab_dir / "README.txt").write_bytes(b"not a fab file\n")
    main(["release", "preflight", "--sch", str(proj / "x.kicad_sch"),
          "--gerbers", str(fab_dir), "--json"])
    gerbers = json.loads(capsys.readouterr().out)["inputs"]["gerbers"]
    assert gerbers["files"] == sorted(payload)          # still the name list
    assert gerbers["file_hashes"] == {
        name: hashlib.sha256(data).hexdigest()
        for name, data in payload.items()}
    assert list(gerbers["file_hashes"]) == sorted(payload)    # sorted keys


def _repo(proj: Path) -> None:
    _git("init", "-q", cwd=proj)
    (proj / ".gitignore").write_text("*.log\n", encoding="utf-8", newline="\n")
    _git("add", "-A", cwd=proj)
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init", cwd=proj)


def _git_bytes(*argv: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *argv], cwd=cwd, check=True,
                          capture_output=True).stdout


def _manifest_git(proj: Path, capsys, sch: Path | None = None) -> dict:
    main(["release", "preflight", "--sch", str(sch or proj / "x.kicad_sch"),
          "--allow-dirty", "--json"])
    return json.loads(capsys.readouterr().out)["git"]


def test_clean_worktree_has_no_dirty_hash(proj, capsys):
    _repo(proj)
    git = _manifest_git(proj, capsys)
    assert git["available"] is True and git["dirty"] is False
    assert len(git["revision"]) == 40
    assert "dirty_sha256" not in git


def test_non_repository_has_no_dirty_hash(proj, capsys):
    git = _manifest_git(proj, capsys)
    assert git == {"available": False, "revision": None, "dirty": None}


def test_dirty_hash_is_deterministic_and_tracks_the_dirty_state(proj, capsys):
    _repo(proj)
    (proj / "x.kicad_sch").write_text(_SCH + "\n")          # tracked change
    first = _manifest_git(proj, capsys)
    assert first["dirty"] is True
    assert len(first["dirty_sha256"]) == 64
    int(first["dirty_sha256"], 16)

    # same state -> same hash (it is a function of the state, not of the run)
    assert _manifest_git(proj, capsys)["dirty_sha256"] == first["dirty_sha256"]

    seen = {first["dirty_sha256"]}

    def changed(why: str) -> str:
        digest = _manifest_git(proj, capsys)["dirty_sha256"]
        assert digest not in seen, f"hash did not move after: {why}"
        seen.add(digest)
        return digest

    (proj / "x.kicad_sch").write_text(_SCH + "\n\n")
    changed("a further edit to a tracked file")
    (proj / "notes.txt").write_text("todo\n")
    with_untracked = changed("a new untracked file")
    (proj / "build.log").write_text("ignored\n")             # *.log is ignored
    assert _manifest_git(proj, capsys)["dirty_sha256"] == with_untracked
    _git("add", "x.kicad_pcb", cwd=proj)                     # no-op: unchanged
    assert _manifest_git(proj, capsys)["dirty_sha256"] == with_untracked
    (proj / "x.kicad_pcb").write_text(_PCB + "\n")
    _git("add", "x.kicad_pcb", cwd=proj)                     # STAGED change
    changed("a staged change")
    (proj / "notes.txt").unlink()
    changed("the untracked file going away")


def test_dirty_hash_follows_the_documented_recipe(proj, capsys):
    """sha256(`git diff HEAD --binary` bytes, then NUL + path for every
    untracked non-ignored path in byte order) — git output taken as BYTES."""
    _repo(proj)
    (proj / "x.kicad_sch").write_text(_SCH + "\n")
    (proj / "zeta.txt").write_text("z\n")
    (proj / "alpha.txt").write_text("a\n")
    (proj / "skip.log").write_text("ignored\n")

    diff = _git_bytes("diff", "HEAD", "--binary", "--no-ext-diff",
                      "--no-textconv", "--no-color", cwd=proj)
    assert diff.startswith(b"diff --git ")
    want = hashlib.sha256(diff + b"\0alpha.txt" + b"\0zeta.txt").hexdigest()
    assert _manifest_git(proj, capsys)["dirty_sha256"] == want


def test_dirty_hash_covers_the_whole_worktree_from_a_subdirectory(proj, capsys):
    # The schematic lives in hw/; `git ls-files --others` would by default
    # only see untracked files under hw/, while `git diff HEAD` and `git
    # status` are repo-wide. The hash must not mix the two scopes.
    hw = proj / "hw"
    hw.mkdir()
    shutil.move(str(proj / "x.kicad_sch"), str(hw / "x.kicad_sch"))
    _repo(proj)
    sch = hw / "x.kicad_sch"
    sch.write_text(_SCH + "\n")
    before = _manifest_git(proj, capsys, sch)["dirty_sha256"]
    (proj / "docs").mkdir()
    (proj / "docs" / "new.md").write_text("outside hw/\n")
    after = _manifest_git(proj, capsys, sch)["dirty_sha256"]
    assert after != before

    diff = _git_bytes("diff", "HEAD", "--binary", "--no-ext-diff",
                      "--no-textconv", "--no-color", cwd=proj)
    assert after == hashlib.sha256(diff + b"\0docs/new.md").hexdigest()


def test_untracked_only_worktree_is_dirty_with_a_hash(proj, capsys):
    _repo(proj)
    (proj / "notes.txt").write_text("todo\n")
    git = _manifest_git(proj, capsys)
    assert git["dirty"] is True
    # an empty diff followed by the one untracked path
    assert git["dirty_sha256"] == hashlib.sha256(b"\0notes.txt").hexdigest()

