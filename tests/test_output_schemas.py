"""diff/pinmap/plan/draw --json must validate against their published schemas.

Closes the ROADMAP v0.9 "agent contract completeness" gap: every lint-style
and write-path JSON payload now has a canonical schema in schemas/ (mirrored
into the wheel), and this test keeps the payloads honest against them.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from akcli.cli import main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _validator(name: str) -> "jsonschema.Draft202012Validator":
    doc = json.loads((ROOT / "schemas" / name).read_text())
    return jsonschema.Draft202012Validator(doc)


def _json_of(argv: list[str], expect: int | tuple[int, ...] = 0) -> dict:
    codes = (expect,) if isinstance(expect, int) else expect
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(argv)
    assert rc in codes, f"{argv} -> exit {rc}"
    return json.loads(buf.getvalue())


def _assert_valid(doc: dict, schema: str) -> None:
    errors = ["/".join(map(str, e.path)) + ": " + e.message
              for e in _validator(schema).iter_errors(doc)]
    assert errors == [], f"payload drifted from {schema}: {errors}"


def test_diff_json_validates():
    doc = _json_of(["diff", str(FIXTURES / "shared_name_label.SchDoc"),
                    str(FIXTURES / "two_gnd_ports.SchDoc"),
                    "--json", "--exit-zero"])
    _assert_valid(doc, "diff.schema.json")


def test_diff_self_json_validates():
    doc = _json_of(["diff", str(FIXTURES / "shared_name_label.SchDoc"),
                    str(FIXTURES / "shared_name_label.SchDoc"),
                    "--json", "--exit-zero"])
    _assert_valid(doc, "diff.schema.json")


def test_pinmap_json_validates():
    doc = _json_of(["pinmap", str(FIXTURES / "shared_name_label.SchDoc"),
                    "--mcu", "U3", "--json", "--exit-zero"])
    _assert_valid(doc, "pinmap.schema.json")


def test_pinmap_no_mcu_json_validates():
    doc = _json_of(["pinmap", str(FIXTURES / "shared_name_label.SchDoc"),
                    "--json", "--exit-zero"])
    _assert_valid(doc, "pinmap.schema.json")


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    target = tmp_path / "board.kicad_sch"
    assert main(["new", str(target)]) == 0
    ops = {"protocol_version": 1, "target_format": "kicad",
           "target_file": "board.kicad_sch",
           "ops": [{"op": "add_text", "at": [1000, 1000], "text": "hi"}]}
    (tmp_path / "ops.json").write_text(json.dumps(ops), encoding="utf-8")
    return target


def test_plan_json_validates(board: Path, capsys):
    doc = _json_of(["plan", str(board), "--ops",
                    str(board.parent / "ops.json"), "--json"])
    _assert_valid(doc, "draw-result.schema.json")
    assert doc["status"] == "dry-run" and doc["applied"] is False


def test_draw_apply_json_validates(board: Path, capsys):
    doc = _json_of(["draw", str(board), "--ops",
                    str(board.parent / "ops.json"), "--apply", "--json"])
    _assert_valid(doc, "draw-result.schema.json")
    assert doc["status"] == "applied" and doc["applied"] is True
    assert doc["net_diff"] == {"equivalent": True, "risk": False, "lines": []}


def test_op_error_carries_remediation(board: Path, capsys):
    missing = {"protocol_version": 1, "target_format": "kicad",
               "target_file": "board.kicad_sch",
               "ops": [{"op": "place_component", "lib_id": "No:Such",
                        "designator": "U9", "x_mil": 1000, "y_mil": 1000}]}
    (board.parent / "missing.json").write_text(json.dumps(missing),
                                               encoding="utf-8")
    doc = _json_of(["plan", str(board), "--ops",
                    str(board.parent / "missing.json"), "--json"], expect=6)
    _assert_valid(doc, "draw-result.schema.json")
    op = doc["ops"][0]
    assert op["error_code"] == "SYMBOL_NOT_FOUND"
    assert "--symbols" in op["remediation"]


def test_draw_refused_json_validates(board: Path, capsys):
    bad = {"protocol_version": 1, "target_format": "kicad",
           "target_file": "board.kicad_sch",
           "ops": [{"op": "add_wire",
                    "vertices": [[1000, 1000], [1500, 1000]]}]}
    (board.parent / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    doc = _json_of(["draw", str(board), "--ops",
                    str(board.parent / "bad.json"), "--apply", "--json"],
                   expect=6)
    _assert_valid(doc, "draw-result.schema.json")
    assert doc["status"] == "refused" and doc["applied"] is False
