"""--json must yield machine-readable stdout on EVERY exit path.

The agent contract: an agent that passes ``--json`` never has to parse a bare
non-JSON stdout. Failures that previously left stdout empty (an unreadable
op-list, a missing file, a corrupt container, a usage error) now emit a
structured ``{"error": {code, message, exit, remediation}}`` envelope from
``cli.main``; ``plan``/``draw`` structural op-list errors emit the normal
draw-result shape with ``status: "refused"``. The plain ``ERROR:`` lines keep
going to stderr for humans — stdout stays data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._cli_harness import blank_sheet, json_of, run_cli

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def test_missing_file_emits_json_error_envelope():
    rc, out, err = run_cli(["read", "/nonexistent/board.kicad_sch", "--json"])
    assert rc == 4
    doc = json.loads(out)
    assert doc["schema_version"]
    assert doc["error"]["code"] == "FILE_NOT_FOUND"
    assert doc["error"]["exit"] == 4
    assert doc["error"]["remediation"]      # EXIT-name pseudo-codes hint too
    assert "ERROR" in err  # the human line still goes to stderr


def test_corrupt_container_emits_json_error_with_remediation():
    doc = json_of(["read", str(FIXTURES / "malformed" / "fat_cycle.SchDoc"),
                   "--json"], expect=3)
    assert doc["error"]["code"].startswith("ALTIUM_")
    assert doc["error"]["exit"] == 3
    assert doc["error"]["remediation"]  # every registry code has a hint


def test_usage_error_emits_json_error_envelope(tmp_path):
    # `net <pcb>` routes a .kicad_pcb to _load_schematic -> _ExitWith(5).
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    doc = json_of(["net", str(pcb), "--json"], expect=5)
    assert doc["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert doc["error"]["exit"] == 5
    assert doc["error"]["remediation"]


def test_exitwith_wrapped_error_code_is_recovered(tmp_path):
    """An _ExitWith carrying "ERROR: <CODE>: ..." must surface the real code.

    `undo --apply` under a KiCad GUI lock raises
    ``_ExitWith(6, "ERROR: TARGET_LOCKED: ...")`` — the envelope must say
    TARGET_LOCKED (with its remediation), not the generic OPLIST category.
    """
    target = blank_sheet(tmp_path)
    (tmp_path / f"{target.name}.bak").write_text(
        target.read_text(encoding="utf-8"), encoding="utf-8")
    lock = tmp_path / f"~{target.name}.lck"
    lock.write_text("", encoding="utf-8")
    doc = json_of(["undo", str(target), "--apply", "--json"], expect=6)
    assert doc["error"]["code"] == "TARGET_LOCKED"
    assert "KiCad GUI" in doc["error"]["remediation"]


def _bad_opslist(tmp_path, target, ops_body) -> Path:
    ops = tmp_path / "bad.json"
    ops.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "target_file": target.name, "ops": ops_body,
    }), encoding="utf-8")
    return ops


def test_plan_structural_op_errors_emit_draw_result_json(tmp_path):
    target = blank_sheet(tmp_path)
    ops = _bad_opslist(tmp_path, target, [{"op": "add_wire"}])  # missing vertices
    rc, out, err = run_cli(["plan", str(target), "--ops", str(ops), "--json"])
    assert rc == 6
    doc = json.loads(out)
    assert doc["schema_version"] == "1.0"
    assert doc["status"] == "refused"
    assert doc["applied"] is False
    assert doc["ops"] and all(o["status"] == "error" for o in doc["ops"])
    assert "ERROR" in err


@pytest.mark.parametrize("ops_body", [
    [{"op": "add_wire"}],      # per-op error (op_index >= 0)
    "not-a-list",              # document-level error (OpError op_index -1)
])
def test_plan_structural_errors_validate_against_draw_result_schema(
        tmp_path, ops_body):
    jsonschema = pytest.importorskip("jsonschema")
    target = blank_sheet(tmp_path)
    ops = _bad_opslist(tmp_path, target, ops_body)
    doc = json_of(["plan", str(target), "--ops", str(ops), "--json"], expect=6)
    schema = json.loads((ROOT / "schemas" / "draw-result.schema.json").read_text())
    errors = [e.message for e in
              jsonschema.Draft202012Validator(schema).iter_errors(doc)]
    assert errors == []
    assert all(o["op_index"] >= 0 for o in doc["ops"])


def test_plan_unreadable_oplist_emits_json_error(tmp_path):
    target = blank_sheet(tmp_path)
    ops = tmp_path / "broken.json"
    ops.write_text("{not json", encoding="utf-8")
    rc, out, _ = run_cli(["plan", str(target), "--ops", str(ops), "--json"])
    assert rc != 0
    doc = json.loads(out)   # the top-level envelope, never bare text
    assert "error" in doc


def test_no_double_json_after_partial_output(tmp_path):
    # `net <sch> NAME --json` emits {"found": false} then exits 8: the
    # top-level envelope must NOT append a second document.
    sch = FIXTURES / "kicad" / "board_v7.kicad_sch"
    rc, out, _ = run_cli(["net", str(sch), "NO_SUCH_NET", "--json"])
    assert rc == 8
    doc = json.loads(out)   # would raise on two concatenated documents
    assert doc["found"] is False


def test_json_error_stays_off_in_text_mode():
    rc, out, _ = run_cli(["read", "/nonexistent/board.kicad_sch"])
    assert rc == 4
    assert out == ""        # text mode: stdout untouched, stderr has the line


# --------------------------------------------------------------------------- #
# argparse-level failures (wrong flag / wrong choice / unknown command)
# --------------------------------------------------------------------------- #
def _run_argparse(argv):
    """argparse aborts with SystemExit before main() can return."""
    import contextlib
    import io

    from akcli.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc:
            main(argv)
    return exc.value.code, out.getvalue(), err.getvalue()


def test_argparse_unknown_flag_emits_json_envelope():
    rc, out, err = _run_argparse(["--json", "nets", "--frobnicate"])
    assert rc == 2
    doc = json.loads(out)
    assert doc["error"]["code"] == "USAGE" and doc["error"]["exit"] == 2
    assert "--frobnicate" in doc["error"]["message"]
    assert doc["error"]["remediation"]
    assert "usage:" in err          # the human usage text stays on stderr


def test_argparse_invalid_choice_lists_choices_in_json():
    rc, out, _ = _run_argparse(["--json", "check", "x.kicad_sch",
                                "--fail-on", "sometimes"])
    assert rc == 2
    doc = json.loads(out)
    assert "invalid choice" in doc["error"]["message"]
    assert "never" in doc["error"]["message"]   # the full choices list rides along


def test_argparse_unknown_command_emits_json_envelope():
    rc, out, _ = _run_argparse(["--json", "bogus-command"])
    assert rc == 2
    doc = json.loads(out)
    assert doc["error"]["code"] == "USAGE"
    assert "bogus-command" in doc["error"]["message"]


def test_argparse_error_stays_plain_without_json():
    rc, out, err = _run_argparse(["nets", "--frobnicate"])
    assert rc == 2
    assert out == ""                # no envelope unless --json was asked for
    assert "usage:" in err


def test_unexpected_exception_keeps_json_envelope(tmp_path, capsys, monkeypatch):
    """A bug-class exception (TypeError etc.) must still emit the JSON error
    envelope on stdout and exit PARSE — never a bare traceback with empty
    stdout (the audit's reproduced contract break)."""
    from akcli import cli as cli_mod
    from akcli import netbuild
    from akcli.errors import EXIT

    p = tmp_path / "x.kicad_sch"
    p.write_text(
        '(kicad_sch (version 20231120) (generator "t")\n'
        '  (uuid "aaaaaaaa-0000-4000-8000-000000000000"))\n',
        encoding="utf-8")

    def boom(*a, **k):
        raise TypeError("synthetic handler bug")

    monkeypatch.setattr(netbuild, "build_nets", boom)
    rc = cli_mod.main(["nets", str(p), "--json"])
    captured = capsys.readouterr()
    assert rc == EXIT["PARSE"]
    doc = json.loads(captured.out)
    assert doc["error"]["code"] == "INTERNAL"
    assert "TypeError" in captured.err
