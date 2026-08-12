"""``schemas/findings.schema.json`` — the findings-report contract (review M1).

Gates: packaged mirror byte-identical to the repo canonical; every render-json
output (legacy check shape AND review evidence envelope) validates; the
``datasheet_backed ⇒ evidence.datasheet`` invariant is enforced by the schema
itself; fingerprints are wording-immune.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from akcli import report
from akcli.report import Finding, Severity, anchor, compute_fingerprint

_ROOT = Path(__file__).parent.parent
_SCHEMA = json.loads((_ROOT / "schemas" / "findings.schema.json").read_text())


def _validate(payload: str) -> None:
    jsonschema.validate(json.loads(payload), _SCHEMA)


def test_packaged_mirror_is_byte_identical():
    repo = (_ROOT / "schemas" / "findings.schema.json").read_bytes()
    pkg = (_ROOT / "src" / "akcli" / "schemas" /
           "findings.schema.json").read_bytes()
    assert repo == pkg


def test_legacy_check_shape_validates():
    """A pre-review finding (no envelope fields) must validate unchanged."""
    f = Finding(code="ERC_NO_POWER", severity=Severity.WARNING,
                message="U1 shares no detected power net", refs=["U1"],
                pos=(100.0, 200.0), anchors=[anchor("component", "U1")])
    _validate(report.render([f], "json", meta={"unnamed_net_count": 2}))


def test_review_envelope_validates():
    f = Finding(
        code="REVIEW_XTAL_LOAD", severity=Severity.INFO,
        message="crystal Y1: CL≈9.9pF", refs=["Y1"],
        anchors=[anchor("component", "Y1")],
        detector="signal.crystal", confidence="heuristic",
        evidence={"source": "topology",
                  "assumptions": ["C_stray = 4 pF"]},
        rule_version="1",
        fingerprint=compute_fingerprint("REVIEW_XTAL_LOAD", "1",
                                        [anchor("component", "Y1")]),
        remediation="compare with the crystal CL spec",
        fix_params={"kind": "xtal"}, status="reported")
    _validate(report.render([f], "json"))


def test_datasheet_backed_requires_datasheet_evidence():
    """The schema itself refuses a datasheet_backed claim without evidence."""
    base = {"code": "REVIEW_X", "severity": "warning", "message": "m",
            "refs": [], "confidence": "datasheet_backed"}
    doc = {"schema_version": "2.0", "metadata": {},
           "findings": [dict(base, evidence={"source": "datasheet"})]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, _SCHEMA)          # evidence.datasheet missing
    doc["findings"][0]["evidence"]["datasheet"] = {
        "sha256": "0" * 64, "page": 12}
    jsonschema.validate(doc, _SCHEMA)              # now conformant


def test_fingerprint_is_wording_immune_and_anchor_sensitive():
    a = [anchor("component", "R1"), anchor("net", "FB")]
    fp1 = compute_fingerprint("REVIEW_FB_DIVIDER", "1", a)
    fp2 = compute_fingerprint("REVIEW_FB_DIVIDER", "1", list(reversed(a)))
    assert fp1 == fp2                              # order-free
    assert fp1 == compute_fingerprint("REVIEW_FB_DIVIDER", "1.4", a)  # minor-free
    assert fp1 != compute_fingerprint("REVIEW_FB_DIVIDER", "2", a)    # major bump
    assert fp1 != compute_fingerprint("REVIEW_FB_DIVIDER", "1",
                                      [anchor("component", "R2")])
    assert len(fp1) == 32 and int(fp1, 16) >= 0


def test_finding_json_roundtrip():
    f = Finding(code="REVIEW_RC_CUTOFF", severity=Severity.INFO,
                message="fc=1kHz", refs=["R5", "C1"],
                detector="signal.rc_filter", confidence="deterministic",
                evidence={"source": "calc"}, rule_version="1",
                fingerprint="ab" * 16, remediation="check",
                fix_params={"k": 1}, status="reported")
    back = report.finding_from_json(report._finding_json(f))
    assert back == f


def test_sarif_carries_v1_and_v2_fingerprints():
    f = Finding(code="REVIEW_X", severity=Severity.WARNING, message="m",
                refs=["R1"], fingerprint="cd" * 16, confidence="heuristic")
    doc = json.loads(report.render([f], "sarif", source="a.kicad_sch"))
    fps = doc["runs"][0]["results"][0]["partialFingerprints"]
    assert "akcliFinding/v1" in fps and fps["akcliFinding/v2"] == "cd" * 16
