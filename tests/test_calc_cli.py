"""CLI-layer tests for `akcli calc` (list / info / run / errors)."""

from __future__ import annotations

import json

from akcli import cli


def test_calc_list(capsys):
    assert cli.main(["calc", "list"]) == 0
    out = capsys.readouterr().out
    assert "eseries" in out and "trackwidth" in out and "ne555-astable" in out


def test_calc_list_json_carries_references(capsys):
    assert cli.main(["calc", "list", "--json"]) == 0
    table = json.loads(capsys.readouterr().out)
    assert "via" in table
    assert "Johnson & Graham" in table["via"]["reference"]
    assert all("reference" in c for c in table.values())


def test_calc_info(capsys):
    assert cli.main(["calc", "info", "clearance"]) == 0
    out = capsys.readouterr().out
    assert "IPC-2221B" in out and "voltage" in out


def test_calc_run_json_envelope(capsys):
    assert cli.main(["calc", "rc", "r=10k", "c=100n", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["calc"] == "rc"
    assert doc["results"]["fc"]["value"] > 159
    assert "Horowitz" in doc["reference"]


def test_calc_run_human(capsys):
    assert cli.main(["calc", "led", "vs=5", "vf=2", "i=10m"]) == 0
    out = capsys.readouterr().out
    assert "r_standard" in out and "reference:" in out


def test_calc_never_double_prefixes_units(capsys):
    # clearance values are already in mm — "200 mmm" was a real bug
    assert cli.main(["calc", "clearance", "voltage=120"]) == 0
    out = capsys.readouterr().out
    assert "mmm" not in out and "0.2 mm" in out


def test_calc_bare_lists(capsys):
    assert cli.main(["calc"]) == 0
    assert "eseries" in capsys.readouterr().out


def test_calc_unknown_name(capsys):
    assert cli.main(["calc", "warpdrive"]) == 2


def test_calc_bad_param_token(capsys):
    assert cli.main(["calc", "rc", "10k"]) == 2


def test_calc_missing_param(capsys):
    assert cli.main(["calc", "rc", "r=10k"]) == 2
    assert "missing" in capsys.readouterr().err.lower()


def test_calc_info_unknown(capsys):
    assert cli.main(["calc", "info", "nope"]) == 2
