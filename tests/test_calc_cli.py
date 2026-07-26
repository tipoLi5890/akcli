"""CLI-layer tests for `akcli calc` (list / info / run / errors)."""

from __future__ import annotations

import json

import pytest

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
    # argparse-native choices: unknown names fail at parse time with the
    # full `choose from` list (and a USAGE envelope under --json)
    with pytest.raises(SystemExit) as exc:
        cli.main(["calc", "warpdrive"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    # argparse quotes the choices list on some Python versions and not on
    # others (3.12 dropped the quotes) — assert format-agnostically.
    assert "invalid choice: 'warpdrive'" in err
    assert "eseries" in err                 # the list rides along


def test_calc_bad_param_token(capsys):
    assert cli.main(["calc", "rc", "10k"]) == 2


def test_calc_missing_param(capsys):
    assert cli.main(["calc", "rc", "r=10k"]) == 2
    assert "missing" in capsys.readouterr().err.lower()


def test_calc_info_unknown(capsys):
    assert cli.main(["calc", "info", "nope"]) == 2


# --------------------------------------------------------------------------- #
# RFC 8259 conformance + degenerate-input classification
#
# `_dumps` (commands/_shared.py) is the single JSON exit for the whole CLI and
# used to allow Python's non-standard NaN/Infinity literals through. The fix is
# two-sided: `allow_nan=False` on the way out, and refusals at the two places a
# non-finite number can enter — the parameter parser and the formula itself.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("params", [
    ["r=nan", "c=100n"],
    ["r=inf", "c=100n"],
    ["r=10k", "c=-inf"],
    ["r=NaN", "c=100n"],
    ["r=1e400", "c=100n"],      # overflows float on parse
])
def test_calc_rejects_non_finite_inputs(capsys, params):
    assert cli.main(["calc", "rc", *params, "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["code"] == "USAGE"
    assert doc["error"]["exit"] == 2


def test_calc_json_output_is_rfc8259_parseable(capsys):
    """No bare NaN/Infinity token may ever reach stdout."""
    assert cli.main(["calc", "rc", "r=10k", "c=100n", "--json"]) == 0
    out = capsys.readouterr().out
    json.loads(out)                  # Python's parser ACCEPTS NaN — so also:
    assert "NaN" not in out and "Infinity" not in out


def test_calc_division_by_zero_is_a_usage_error_not_internal(capsys):
    assert cli.main(["calc", "rc", "r=0", "c=0", "--json"]) == 2
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["error"]["code"] == "USAGE"
    assert "zero" in doc["error"]["message"]
    assert "Traceback" not in captured.err


def test_no_calculator_crashes_on_zeroed_inputs(capsys):
    """Registry-wide sweep: every calculator must REFUSE, never explode.

    A zero for every numeric parameter is the classic degenerate input; the
    shared guard in ``calc.registry._run`` turns the resulting
    ZeroDivisionError / domain error into a ``CalcError``, so the exit is 0
    (it coped) or 2 (it refused) — never 3 (INTERNAL).
    """
    from akcli.calc.registry import CALCS

    for name, calc in sorted(CALCS.items()):
        params = [f"{p.name}={p.choices[0] if p.choices else 0}"
                  for p in calc.params]
        rc = cli.main(["calc", name, *params, "--json"])
        captured = capsys.readouterr()
        assert rc in (0, 2), f"{name} -> exit {rc}: {captured.out}{captured.err}"
        assert "Traceback" not in captured.err, name
        if rc == 0:
            assert "NaN" not in captured.out, name
            assert "Infinity" not in captured.out, name


def test_calc_info_json(capsys):
    assert cli.main(["calc", "info", "rc", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)   # was plain text under --json
    assert doc["name"] == "rc"
    assert doc["schema_version"]
    assert {p["name"] for p in doc["params"]} == {"r", "c"}
    assert all(p["required"] for p in doc["params"])
    assert "Horowitz" in doc["reference"]


def test_calc_info_json_matches_calc_list_entry(capsys):
    """``calc info X --json`` and ``calc list --json``'s X entry agree."""
    assert cli.main(["calc", "info", "clearance", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert cli.main(["calc", "list", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["clearance"]
    for key in ("title", "group", "reference"):
        assert info[key] == entry[key]
    assert ([p["name"] for p in info["params"]]
            == [p["name"] for p in entry["params"]])
