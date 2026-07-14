"""Central config ``[[waiver]]`` application, header accounting, and --fail-on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import cli
from akcli.config import load_config
from akcli.errors import AkcliError
from akcli.report import Finding, Severity, apply_waivers

FIX = Path(__file__).parent / "fixtures"
SCH = FIX / "t_junction.SchDoc"


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_load_generic_waivers(tmp_path):
    cfg_file = tmp_path / "akcli.toml"
    cfg_file.write_text(
        '[[waiver]]\ncode = "BOM_MISSING_FOOTPRINT"\nrefs = "U*"\n'
        'severity = "off"\nreason = "no PCB yet"\n\n'
        '[[waiver]]\ncode = "BOM_MISSING_VALUE"\nseverity = "note"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert len(cfg.waivers) == 2
    assert cfg.waivers[0]["code"] == "BOM_MISSING_FOOTPRINT"
    assert cfg.waivers[0]["severity"] == "off"
    # generic waivers do not disturb the legacy ERC-only mechanism
    assert cfg.erc_waivers == []


def test_waiver_requires_code(tmp_path):
    cfg_file = tmp_path / "akcli.toml"
    cfg_file.write_text('[[waiver]]\nseverity = "off"\n', encoding="utf-8")
    with pytest.raises(AkcliError):
        load_config(cfg_file)


def test_waiver_bad_severity_rejected(tmp_path):
    cfg_file = tmp_path / "akcli.toml"
    cfg_file.write_text('[[waiver]]\ncode = "X"\nseverity = "bogus"\n', encoding="utf-8")
    with pytest.raises(AkcliError):
        load_config(cfg_file)


# --------------------------------------------------------------------------- #
# apply_waivers
# --------------------------------------------------------------------------- #
def _findings():
    return [
        Finding("BOM_MISSING_VALUE", Severity.WARNING, "U1 no value", refs=["U1"]),
        Finding("BOM_MISSING_FOOTPRINT", Severity.WARNING, "U1 no fp", refs=["U1"]),
        Finding("ERC_DANGLING_NET", Severity.WARNING, "dangle", refs=["R9.2"]),
    ]


def test_apply_waivers_off_drops_demote_downgrades():
    waivers = [
        {"code": "BOM_MISSING_FOOTPRINT", "severity": "off"},
        {"code": "BOM_MISSING_VALUE", "severity": "note"},
    ]
    kept, waived, demoted = apply_waivers(_findings(), waivers)
    assert waived == 2 and demoted == 1
    codes = {f.code: f.severity for f in kept}
    assert "BOM_MISSING_FOOTPRINT" not in codes           # dropped
    assert codes["BOM_MISSING_VALUE"] is Severity.NOTE     # demoted
    assert codes["ERC_DANGLING_NET"] is Severity.WARNING   # untouched


def test_apply_waivers_refs_fnmatch_scopes_the_match():
    waivers = [{"code": "BOM_*", "refs": "R*", "severity": "off"}]
    kept, waived, demoted = apply_waivers(_findings(), waivers)
    # refs "R*" matches none of the U1 BOM findings, so nothing is waived
    assert waived == 0 and len(kept) == 3


def test_apply_waivers_no_waivers_is_identity():
    fs = _findings()
    kept, waived, demoted = apply_waivers(fs, [])
    assert kept == fs and waived == 0 and demoted == 0


# --------------------------------------------------------------------------- #
# CLI: header accounting + exit semantics
# --------------------------------------------------------------------------- #
def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "akcli.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_cli_header_reports_waivers_and_clean_exit(tmp_path, capsys):
    cfg = _write_cfg(
        tmp_path,
        '[[waiver]]\ncode = "BOM_MISSING_FOOTPRINT"\nseverity = "off"\n\n'
        '[[waiver]]\ncode = "BOM_MISSING_VALUE"\nseverity = "note"\n',
    )
    rc = cli.main(["-C", str(cfg), "check", str(SCH), "--json"])
    doc = json.loads(capsys.readouterr().out)
    # 3 footprint findings dropped + 3 value findings demoted = 6 waived, 3 demoted
    assert doc["metadata"]["config_waived"] == "6 (3 demoted)"
    # every remaining finding is <= NOTE, so the default warning gate exits 0 —
    # but the header still shows the run was only cleaned by waivers
    assert rc == 0


def test_cli_fail_on_note_trips_on_demoted(tmp_path, capsys):
    cfg = _write_cfg(
        tmp_path,
        '[[waiver]]\ncode = "BOM_MISSING_FOOTPRINT"\nseverity = "off"\n\n'
        '[[waiver]]\ncode = "BOM_MISSING_VALUE"\nseverity = "note"\n',
    )
    rc = cli.main(["-C", str(cfg), "check", str(SCH), "--json", "--fail-on", "note"])
    capsys.readouterr()
    assert rc == 1


def test_cli_fail_on_error_ignores_warnings(capsys):
    rc = cli.main(["check", str(SCH), "--json", "--fail-on", "error"])
    capsys.readouterr()
    assert rc == 0  # only warnings/notes present, none reach ERROR


def test_cli_default_fail_on_warning_matches_history(capsys):
    rc = cli.main(["check", str(SCH), "--json"])
    capsys.readouterr()
    assert rc == 1  # BOM warnings present -> exit 1, exactly as before


def test_cli_exit_zero_alias_still_forces_zero(capsys):
    rc = cli.main(["check", str(SCH), "--json", "--exit-zero"])
    capsys.readouterr()
    assert rc == 0


def test_cli_fail_on_never_forces_zero(capsys):
    rc = cli.main(["check", str(SCH), "--json", "--fail-on", "never"])
    capsys.readouterr()
    assert rc == 0
