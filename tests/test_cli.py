"""End-to-end tests for the ``akcli`` CLI dispatch (``cli.py``).

Drives :func:`akcli.cli.main` with explicit ``argv`` and asserts both
the exit code (per SPEC §8) and the stdout/stderr split (data vs logs).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT

FIXTURES = Path(__file__).parent / "fixtures"


def F(name: str) -> str:
    return str(FIXTURES / name)


SHARED = "shared_name_label.SchDoc"
TWO_GND = "two_gnd_ports.SchDoc"
JUNC = "junction_cross.SchDoc"


# --------------------------------------------------------------------------- #
# global
# --------------------------------------------------------------------------- #
def test_version(capsys):
    assert main(["--version"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "akcli" in out
    assert "protocol" in out


def test_no_command_is_usage_error(capsys):
    assert main([]) == EXIT["USAGE"]


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_text(capsys):
    assert main(["read", F(SHARED)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "components: 4" in out
    assert "STAT" in out


def test_read_json(capsys):
    assert main(["read", "--json", F(SHARED)]) == EXIT["OK"]
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["schema_version"] == "1.2"
    assert doc["source_format"] == "altium"
    assert len(doc["components"]) == 4
    # net membership present
    assert doc["nets"][0]["name"] == "STAT"


def test_read_md(capsys):
    assert main(["read", "--md", F(SHARED)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert out.lstrip().startswith("# Schematic")
    assert "| Designator |" in out


def test_read_kicad_works(capsys):
    # KiCad reader is wired into `read`; a .kicad_sch parses
    assert main(["read", F("kicad/board_v8.kicad_sch"), "--json"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert '"source_format"' in out and "kicad" in out


def test_read_missing_altium_file(capsys):
    assert main(["read", F("does_not_exist.SchDoc")]) == EXIT["NOT_FOUND"]


# --------------------------------------------------------------------------- #
# net / component
# --------------------------------------------------------------------------- #
def test_net_query_named(capsys):
    assert main(["net", F(SHARED), "STAT"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert out.startswith("STAT:")
    for tok in ("U2.1", "U3.2", "R7.1", "R12.1"):
        assert tok in out


def test_net_list_all_json(capsys):
    assert main(["net", "--json", F(TWO_GND)]) == EXIT["OK"]
    nets = json.loads(capsys.readouterr().out)
    assert len(nets) == 1
    assert nets[0]["name"] == "GND"


def test_net_missing_name(capsys):
    assert main(["net", F(SHARED), "NOPE"]) == EXIT["OK"]
    assert "no net" in capsys.readouterr().err


def test_component_pin_to_net(capsys):
    assert main(["component", F(SHARED), "U3"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "component: U3" in out
    assert "-> STAT" in out


def test_component_json(capsys):
    assert main(["component", "--json", F(SHARED), "U3"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["designator"] == "U3"
    assert doc["pin_nets"]["2"] == "STAT"


def test_component_not_found(capsys):
    assert main(["component", F(SHARED), "Q99"]) == EXIT["OK"]
    assert "no component" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def test_check_findings_exit_one(capsys):
    # bom check finds missing values/footprints -> WARNING findings -> exit 1
    assert main(["check", "--bom", F(SHARED)]) == EXIT["FINDINGS"]
    out = capsys.readouterr().out
    assert "# metadata" in out
    assert "# findings" in out


def test_check_exit_zero_forces_zero(capsys):
    assert main(["check", "--bom", "--exit-zero", F(SHARED)]) == EXIT["OK"]


def test_check_json(capsys):
    assert main(["check", "--bom", "--json", F(SHARED)]) in (EXIT["OK"], EXIT["FINDINGS"])
    doc = json.loads(capsys.readouterr().out)
    assert "metadata" in doc and "findings" in doc


def test_check_power_clean_is_zero(capsys):
    # two_gnd_ports: power check emits only NOTE (no rails) -> not actionable
    rc = main(["check", "--power", F(TWO_GND)])
    assert rc == EXIT["OK"]


def test_check_erc_now_runs(capsys):
    # erc.py is implemented now: --erc must RUN (no longer "gracefully skipped").
    rc = main(["check", "--erc", F(TWO_GND)])
    assert rc in (EXIT["OK"], EXIT["FINDINGS"])
    err = capsys.readouterr().err
    assert "ERC check unavailable" not in err  # the skip path is gone


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def test_diff_identical_is_clean(capsys):
    # identical schematic -> only NOTE (low confidence) -> not actionable -> 0
    assert main(["diff", F(SHARED), F(SHARED)]) == EXIT["OK"]


def test_diff_different_exit_one(capsys):
    assert main(["diff", F(SHARED), F(TWO_GND)]) == EXIT["FINDINGS"]
    out = capsys.readouterr().out
    assert "DIFF_" in out


def test_diff_json(capsys):
    main(["diff", "--json", F(SHARED), F(TWO_GND)])
    doc = json.loads(capsys.readouterr().out)
    assert "summary" in doc
    assert "component_changes" in doc


# --------------------------------------------------------------------------- #
# pinmap
# --------------------------------------------------------------------------- #
def test_pinmap_mcu_flag(capsys):
    # U3 pin 2 sits on STAT -> a clean PINMAP info finding, exit 0
    assert main(["pinmap", "--mcu", "U3", F(SHARED)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "STAT" in out


def test_pinmap_no_mcu_warns(capsys):
    # no config + no --mcu -> PINMAP_NO_MCU WARNING -> exit 1
    assert main(["pinmap", F(SHARED)]) == EXIT["FINDINGS"]


def test_pinmap_expected_csv_mismatch(tmp_path, capsys):
    csvfile = tmp_path / "expected.csv"
    csvfile.write_text("pin,signal\n2,WRONG_NET\n", encoding="utf-8")
    rc = main(["pinmap", "--mcu", "U3", "--expected", str(csvfile), F(SHARED)])
    assert rc == EXIT["FINDINGS"]
    out = capsys.readouterr().out
    assert "WRONG_NET" in out


def test_pinmap_expected_json_match(tmp_path, capsys):
    jf = tmp_path / "expected.json"
    jf.write_text(json.dumps({"2": "STAT"}), encoding="utf-8")
    rc = main(["pinmap", "--mcu", "U3", "--expected", str(jf), F(SHARED)])
    assert rc == EXIT["OK"]
    assert "matches" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def test_export_protel_stdout(capsys):
    assert main(["export", "--format", "protel", F(TWO_GND)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "(\nGND\n" in out
    assert "U1-1" in out


def test_export_csv_stdout(capsys):
    assert main(["export", "--format", "csv", F(TWO_GND)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "net,ref,pin"


def test_export_to_file(tmp_path, capsys):
    dest = tmp_path / "out.net"
    assert main(["export", "--format", "kicad", "-o", str(dest), F(TWO_GND)]) == EXIT["OK"]
    text = dest.read_text(encoding="utf-8")
    assert text.startswith('(export (version "E")')
    # data went to the file, not stdout
    assert capsys.readouterr().out == ""


def test_export_default_format(capsys):
    # default is protel
    assert main(["export", F(JUNC)]) == EXIT["OK"]
    assert "[" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# plan / draw (KiCad op-list executor; full coverage in test_draw_cli.py)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ["plan", "draw"])
def test_plan_draw_need_a_target(cmd, capsys):
    # No target / no --ops -> usage error (exit 2), not a stub notice.
    assert main([cmd]) == EXIT["USAGE"]


# --------------------------------------------------------------------------- #
# format handling
# --------------------------------------------------------------------------- #
def test_check_on_kicad_runs(capsys):
    # KiCad is supported now; check runs on a .kicad_sch and returns OK or FINDINGS
    rc = main(["check", F("kicad/board_v8.kicad_sch")])
    assert rc in (EXIT["OK"], EXIT["FINDINGS"])
