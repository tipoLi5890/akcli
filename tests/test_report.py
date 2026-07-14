"""Tests for findings rendering + metadata caveats (report.py)."""

from __future__ import annotations

import json

from akcli.report import Finding, Severity, render


def test_severity_enum():
    assert Severity.WARNING.value == "warning"
    assert Severity.NOTE.value == "note"


def test_text_report_always_has_metadata_header():
    out = render([], fmt="text", meta=None)
    assert "passive-pin ratio" in out
    assert "No-ERC suppressed" in out
    assert "unnamed nets" in out
    assert "frac coords present" in out
    assert "(none)" in out


def test_text_report_lists_findings_sorted_by_severity():
    findings = [
        Finding("A", Severity.NOTE, "a note"),
        Finding("B", Severity.ERROR, "an error", refs=["U3.7"]),
    ]
    out = render(findings, fmt="text", meta={"unnamed_net_count": 4})
    lines = out.splitlines()
    # error (more serious) appears before note
    err_idx = next(i for i, ln in enumerate(lines) if "[B]" in ln)
    note_idx = next(i for i, ln in enumerate(lines) if "[A]" in ln)
    assert err_idx < note_idx
    assert "ERROR [B] an error ['U3.7']" in out
    assert "unnamed nets: 4" in out


def test_json_report_shape():
    findings = [Finding("X", Severity.WARNING, "msg", refs=["R1.2"])]
    meta = {"passive_pin_ratio": 0.98, "frac_present": True}
    out = render(findings, fmt="json", meta=meta)
    payload = json.loads(out)
    assert payload["metadata"]["passive_pin_ratio"] == 0.98
    assert payload["metadata"]["frac_present"] is True
    assert payload["metadata"]["no_erc_suppressed"] == "0"  # default filled
    assert payload["findings"][0] == {
        "code": "X", "severity": "warning", "message": "msg", "refs": ["R1.2"],
    }


def test_metadata_defaults_filled():
    out = render([], fmt="json")
    meta = json.loads(out)["metadata"]
    assert meta["passive_pin_ratio"] == "n/a"
    assert meta["unnamed_net_count"] == "0"
