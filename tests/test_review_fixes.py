"""Regression tests for the external-review findings (Altium analysis side).

Each test pins a fix from the review so the old behavior can't silently return.
"""

from __future__ import annotations

import json

import pytest

from akcli import cli, report
from akcli.checks import _rails
from akcli.errors import EXIT


# --- #2 rail voltage inference: the underscore-suffix `\b` bug ----------------
@pytest.mark.parametrize(
    "name,volts",
    [
        ("V3V3", 3.3), ("V3V3_AUX", 3.3), ("V3V3_IO", 3.3),  # the reported bug
        ("3V3", 3.3), ("3V3_MCU", 3.3), ("1V8", 1.8),
        ("5V", 5.0), ("5V_USB", 5.0), ("3.3V", 3.3), ("12V", 12.0),
        ("V5", 5.0), ("V3.3", 3.3), ("+3V3", 3.3),
        ("GND", None), ("SDA", None), ("NET3", None),
    ],
)
def test_implied_voltage(name, volts):
    assert _rails.implied_voltage(name) == volts


# --- #2c rail name matching: exact + `<rail>_suffix` -------------------------
def test_rail_matches_prefix():
    rails = {"V3V3"}
    assert _rails.rail_matches("V3V3", rails)
    assert _rails.rail_matches("V3V3_AUX", rails)
    assert _rails.rail_matches("V3V3-IO", rails)
    assert not _rails.rail_matches("V3V3X", rails)   # no separator -> not a match
    assert not _rails.rail_matches("V5", rails)


# --- #1 footprint falls back to the RECORD-41 parameter ----------------------
def test_footprint_falls_back_to_parameter():
    from akcli.readers import altium_sch as A

    recs = [
        {"RECORD": str(A.RECORD_COMPONENT)},  # idx 0
        {"RECORD": str(A.RECORD_DESIGNATOR), "OwnerIndex": "0", "Text": "R1"},
        {"RECORD": str(A.RECORD_PARAMETER), "OwnerIndex": "0",
         "Name": "Footprint", "Text": "R0603"},
    ]
    comps, _ = A._build_components(recs)
    assert len(comps) == 1
    assert comps[0].designator == "R1"
    assert comps[0].footprint == "R0603"


def test_footprint_model_link_wins_over_parameter():
    # Real model-link chain: component <- RECORD-44 (impl) <- RECORD-45 (model, ModelName).
    # The footprint must resolve back to the component index across BOTH hops.
    from akcli.readers import altium_records as AR, altium_sch as A

    recs = [
        {"RECORD": str(AR.RECORD_COMPONENT)},                                      # idx0
        {"RECORD": str(AR.RECORD_DESIGNATOR), "OwnerIndex": "0", "Text": "U1"},    # idx1
        {"RECORD": str(AR.RECORD_IMPL_44), "OwnerIndex": "0"},                     # idx2 impl
        {"RECORD": str(AR.RECORD_IMPL_MODEL), "OwnerIndex": "2", "ModelName": "LGA-28"},  # idx3
        {"RECORD": str(AR.RECORD_PARAMETER), "OwnerIndex": "0",
         "Name": "Footprint", "Text": "WRONG"},                                    # idx4
    ]
    comps, _ = A._build_components(recs)
    assert comps[0].footprint == "LGA-28"


# --- #(report) diff --json and component --json carry schema_version ---------
def test_diff_and_component_json_have_schema_version(capsys):
    from pathlib import Path

    from akcli.readers import kicad as kr

    v8 = str(Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch")

    cli.main(["diff", v8, v8, "--json"])
    diff_out = json.loads(capsys.readouterr().out)
    assert diff_out["schema_version"]

    ref = kr.read_sch(v8).components[0].designator
    cli.main(["component", v8, ref, "--json"])
    comp_out = json.loads(capsys.readouterr().out)
    assert comp_out["schema_version"]


# --- #3 export --json wraps the netlist in a structured envelope -------------
def test_export_json_envelope(capsys):
    from pathlib import Path
    fixture = str(Path(__file__).parent / "fixtures" / "shared_name_label.SchDoc")
    rc = cli.main(["export", fixture, "--json"])
    assert rc == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["format"] == "protel"
    assert doc["schema_version"]
    assert doc["content"].startswith("[")  # protel netlist body


# --- #4 report JSON carries schema_version ----------------------------------
def test_report_json_has_schema_version():
    payload = json.loads(report.render([], "json", {}))
    assert payload["schema_version"]
    assert "findings" in payload and "metadata" in payload


# --- #9 global flags accepted before OR after the subcommand -----------------
def test_global_flags_before_or_after_subcommand():
    p = cli.build_parser()
    a = p.parse_args(["-C", "foo.toml", "--json", "read", "x.SchDoc"])
    assert a.config == "foo.toml" and a.json is True
    b = p.parse_args(["read", "-C", "foo.toml", "x.SchDoc"])
    assert b.config == "foo.toml"
    c = p.parse_args(["read", "x.SchDoc"])           # neither given -> attrs suppressed
    assert getattr(c, "config", None) is None and getattr(c, "json", None) is None
