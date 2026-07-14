"""Tests for `check --format sarif|junit` (report renderers + CLI wiring)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from akcli import cli, report
from akcli.report import Finding, Severity

FIX = Path(__file__).parent / "fixtures"

_FINDINGS = [
    Finding("ERC_FLOATING_INPUT", Severity.ERROR, 'pin <U1.3> floats & "dangles"',
            refs=["U1.3"]),
    Finding("BOM_REFDES_GAP", Severity.NOTE, "refdes gap R1..R3", refs=["R2"]),
    Finding("POWER_RAIL", Severity.INFO, "rail 3V3 ~3.3V", refs=["+3V3"]),
]


# --------------------------------------------------------------------------- #
# SARIF
# --------------------------------------------------------------------------- #
def test_sarif_structure_and_levels():
    doc = json.loads(report.render(_FINDINGS, "sarif", {}, source="a/b.SchDoc"))
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "akcli"
    rules = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert rules == {"ERC_FLOATING_INPUT", "BOM_REFDES_GAP", "POWER_RAIL"}
    by_rule = {r["ruleId"]: r for r in run["results"]}
    assert by_rule["ERC_FLOATING_INPUT"]["level"] == "error"
    assert by_rule["BOM_REFDES_GAP"]["level"] == "note"
    # location carries the schematic path; fingerprints are stable
    loc = by_rule["ERC_FLOATING_INPUT"]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "a/b.SchDoc"
    fp1 = by_rule["ERC_FLOATING_INPUT"]["partialFingerprints"]["akcliFinding/v1"]
    doc2 = json.loads(report.render(_FINDINGS, "sarif", {}, source="a/b.SchDoc"))
    fp2 = {r["ruleId"]: r for r in doc2["runs"][0]["results"]}[
        "ERC_FLOATING_INPUT"]["partialFingerprints"]["akcliFinding/v1"]
    assert fp1 == fp2


def test_sarif_empty_run_is_valid():
    doc = json.loads(report.render([], "sarif", {}, source="x.kicad_sch"))
    assert doc["runs"][0]["results"] == []
    assert doc["runs"][0]["tool"]["driver"]["rules"] == []


# --------------------------------------------------------------------------- #
# JUnit
# --------------------------------------------------------------------------- #
def test_junit_counts_and_escaping():
    xml_text = report.render(_FINDINGS, "junit", {}, source="a/b.SchDoc")
    root = ET.fromstring(xml_text)  # must be well-formed despite <>& and quotes
    assert root.tag == "testsuites"
    assert root.get("tests") == "3" and root.get("failures") == "1"
    suite = root[0]
    assert suite.get("name") == "a/b.SchDoc"
    failures = suite.findall("./testcase/failure")
    assert len(failures) == 1
    assert 'floats & "dangles"' in failures[0].text  # escaped-then-parsed round trip
    # NOTE/INFO ride along as passed cases with system-out
    outs = suite.findall("./testcase/system-out")
    assert len(outs) == 2


def test_junit_clean_run_has_one_passed_case():
    root = ET.fromstring(report.render([], "junit", {}, source=None))
    assert root.get("failures") == "0"
    assert root[0][0].get("name") == "no findings"


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def test_cli_check_sarif(capsys):
    rc = cli.main(["check", str(FIX / "t_junction.SchDoc"), "--format", "sarif",
                   "--exit-zero"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0"
    uri = doc["runs"][0]["results"][0]["locations"][0][
        "physicalLocation"]["artifactLocation"]["uri"]
    assert uri.endswith("t_junction.SchDoc")


def test_cli_check_junit_keeps_exit_semantics(capsys):
    rc = cli.main(["check", str(FIX / "t_junction.SchDoc"), "--format", "junit"])
    out = capsys.readouterr().out
    root = ET.fromstring(out)
    # findings exist on this fixture -> lint-style exit 1 unless --exit-zero
    assert rc == 1
    assert int(root.get("failures")) >= 1
