"""``akcli review`` engine + CLI (M1 skeleton / M2 detectors, end to end).

Covers: registry integrity, deterministic + stamped engine output, the
advisory exit-code contract (0 unless --fail-on), the findings-JSON envelope
validating against the shipped schema, and the format-agnostic promise (the
same engine runs on an Altium .SchDoc read).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from akcli import cli
from akcli.review import DETECTORS, rules_index
from akcli.review import engine
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"
SCHDOC = Path(__file__).parent / "fixtures" / "t_junction.SchDoc"
_SCHEMA = json.loads((Path(__file__).parent.parent / "schemas" /
                      "findings.schema.json").read_text())


def _oplist(*ops):
    return {"protocol_version": 1, "target_format": "kicad", "ops": list(ops)}


def _seed_review_sheet(tmp_path: Path) -> Path:
    """Divider with a lying tap name + an RC pole: two findings by design."""
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "66666666-7777-8888-9999-aaaaaaaaaaaa") (paper "A4"))\n')
    rs = kw.apply(_oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 2000, "y_mil": 1000, "value": "10k"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 1400, "value": "30k"},
        {"op": "add_net_label", "name": "+5V", "at": "R1.1"},
        {"op": "add_net_label", "name": "2V5_REF", "at": "R1.2"},
        {"op": "add_net_label", "name": "2V5_REF", "at": "R2.1"},
        {"op": "add_net_label", "name": "GND", "at": "R2.2"},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R5",
         "x_mil": 4000, "y_mil": 1000, "value": "1k"},
        {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
         "x_mil": 4000, "y_mil": 1400, "value": "100n"},
        {"op": "add_net_label", "name": "SIG_IN", "at": "R5.1"},
        {"op": "add_net_label", "name": "SIG_F", "at": "R5.2"},
        {"op": "add_net_label", "name": "SIG_F", "at": "C1.1"},
        {"op": "add_net_label", "name": "GND", "at": "C1.2"},
    ), str(tgt), apply=True, sources=[str(DEVICE)])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    return tgt


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_and_rule_codes():
    rules = rules_index()                     # imports detectors as a side effect
    assert {"signal.divider", "signal.rc_filter", "signal.crystal",
            "signal.protection", "signal.opamp", "validation.i2c_pullup",
            "validation.vdomain", "validation.enable_pin", "pcb.routing",
            "pcb.decap", "pcb.thermal", "pcb.trace_width", "emc.planes",
            "emc.stitching", "emc.edge", "emc.diffpair", "emc.protection",
            "domain.usb", "gerber.package"} <= set(DETECTORS)
    assert all(code.startswith("REVIEW_") for code in rules)
    for det in DETECTORS.values():
        assert det.family in ("signal", "validation", "pcb", "emc",
                              "domain", "gerber")
        assert det.rules, det.name


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def test_engine_stamps_and_orders_deterministically(tmp_path):
    from akcli.readers import kicad
    sch = kicad.read_sch(str(_seed_review_sheet(tmp_path)))
    f1, m1 = engine.analyze(sch, profile="standard")
    f2, m2 = engine.analyze(sch, profile="standard")
    assert [ (f.code, f.fingerprint) for f in f1 ] == \
           [ (f.code, f.fingerprint) for f in f2 ]     # run-to-run stable
    codes = [f.code for f in f1]
    assert codes == ["REVIEW_DIVIDER_TAP_MISMATCH", "REVIEW_RC_CUTOFF"]
    for f in f1:
        assert f.detector and f.fingerprint and f.rule_version
    assert m1["trust_summary"] == {"deterministic": 1, "heuristic": 1}
    assert "signal.divider" in m1["detectors_run"]


def test_engine_detector_filter_and_unknown(tmp_path):
    from akcli.readers import kicad
    sch = kicad.read_sch(str(_seed_review_sheet(tmp_path)))
    fs, meta = engine.analyze(sch, detectors=["signal.rc_filter"])
    assert [f.code for f in fs] == ["REVIEW_RC_CUTOFF"]
    assert meta["detectors_run"] == ["signal.rc_filter"]
    try:
        engine.analyze(sch, detectors=["nope.nothing"])
    except KeyError as e:
        assert "nope.nothing" in str(e)
    else:  # pragma: no cover
        raise AssertionError("unknown detector must raise")


def test_engine_runs_on_altium_schdoc():
    """Format-agnostic promise: the same detectors accept an Altium read."""
    from akcli.readers import altium_sch
    sch = altium_sch.read(str(SCHDOC))
    assert sch.source_format == "altium"
    findings, meta = engine.analyze(sch, profile="deep")
    assert meta["source_format"] == "altium"
    assert isinstance(findings, list)          # no crash = the contract here


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_analyze_is_advisory_and_schema_valid(tmp_path, capsys):
    tgt = _seed_review_sheet(tmp_path)
    out = tmp_path / "review.findings.json"
    assert cli.main(["review", "analyze", str(tgt), "--json",
                     "--out", str(out)]) == 0          # advisory: exit 0
    doc = json.loads(capsys.readouterr().out)
    jsonschema.validate(doc, _SCHEMA)
    assert doc == json.loads(out.read_text(encoding="utf-8"))
    assert {f["code"] for f in doc["findings"]} == \
           {"REVIEW_DIVIDER_TAP_MISMATCH", "REVIEW_RC_CUTOFF"}
    assert doc["metadata"]["review_profile"] == "standard"


def test_cli_fail_on_opts_into_gating(tmp_path, capsys):
    tgt = _seed_review_sheet(tmp_path)
    assert cli.main(["review", "analyze", str(tgt),
                     "--fail-on", "warning"]) == 1
    capsys.readouterr()
    assert cli.main(["review", "analyze", str(tgt),
                     "--fail-on", "error"]) == 0       # only a WARNING present
    capsys.readouterr()


def test_cli_report_rerenders_all_formats(tmp_path, capsys):
    tgt = _seed_review_sheet(tmp_path)
    out = tmp_path / "r.json"
    cli.main(["review", "analyze", str(tgt), "--out", str(out)])
    capsys.readouterr()
    assert cli.main(["review", "report", str(out), "--format", "sarif"]) == 0
    sarif = json.loads(capsys.readouterr().out)
    fps = sarif["runs"][0]["results"][0]["partialFingerprints"]
    assert "akcliFinding/v2" in fps            # wording-immune identity
    assert cli.main(["review", "report", str(out), "--format", "markdown"]) == 0
    md = capsys.readouterr().out
    assert "REVIEW_DIVIDER_TAP_MISMATCH" in md and "heuristic" in md


def test_cli_explain_known_and_unknown(capsys):
    assert cli.main(["review", "explain", "REVIEW_RC_CUTOFF"]) == 0
    out = capsys.readouterr().out
    assert "fc = 1/(2πRC)" in out and "reference" in out
    assert cli.main(["review", "explain", "REVIEW_NOPE"]) == 2
    assert "unknown rule" in capsys.readouterr().err


def test_cli_waivers_apply_to_review(tmp_path, capsys):
    tgt = _seed_review_sheet(tmp_path)
    (tmp_path / "akcli.toml").write_text(
        '[[waiver]]\ncode = "REVIEW_DIVIDER_*"\nseverity = "off"\n')
    assert cli.main(["review", "analyze", str(tgt), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {f["code"] for f in doc["findings"]} == {"REVIEW_RC_CUTOFF"}
    assert doc["metadata"]["config_waived"].startswith("1")


def test_bare_review_is_usage_exit_2(capsys):
    # regression: `sys.stderr.write(...) or EXIT` returned the 49-char count
    assert cli.main(["review"]) == 2
    assert "review analyze" in capsys.readouterr().err


def test_bare_review_facts_is_usage_exit_2(capsys):
    assert cli.main(["review", "facts"]) == 2
    assert "facts add" in capsys.readouterr().err


def test_fail_on_never_is_explicit_advisory(tmp_path, capsys):
    # `never` unifies the ladder with check/diff/pinmap: explicit advisory
    tgt = _seed_review_sheet(tmp_path)
    assert cli.main(["review", "analyze", str(tgt),
                     "--fail-on", "never"]) == 0
    capsys.readouterr()
    assert cli.main(["review", "analyze", str(tgt),
                     "--fail-on", "note"]) == 1        # WARNING >= note
