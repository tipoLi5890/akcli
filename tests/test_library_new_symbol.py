"""`akcli library new-symbol` / `akcli library lint-symbols` — the CLI surface.

Covers the plan/apply contract (dry-run writes nothing, `--apply` writes
atomically), the JSON payload an agent reads, the structured refusals, and the
one write gate that is deliberately NOT advisory: a coincident pin in our own
output refuses the write, because KiCad would merge those pins into a single
node in every placement of the part.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import kicad_lib

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symlib"
OPAMP = FIX / "generic_opamp_8.spec.json"
LOGIC = FIX / "generic_logic_14.spec.json"


def _run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _spec_file(tmp_path: Path, doc: dict, name: str = "spec.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# plan / apply
# --------------------------------------------------------------------------- #
def test_dry_run_previews_and_writes_nothing(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    code, stdout, _ = _run(capsys, ["library", "new-symbol", str(OPAMP),
                                    "-o", str(out)])
    assert code == EXIT["OK"]
    assert not out.exists()
    assert "dry-run: nothing written" in stdout
    assert "GENERIC_OPAMP_8" in stdout
    # the preview is the point: every pin, with the coordinate akcli computed
    for name, at in (("OUT_A", "(400,100)"), ("IN_A-", "(-400,100)"),
                     ("VEE", "(0,-400)"), ("VCC", "(0,300)")):
        assert name in stdout and at in stdout
    assert stdout.count(" at (") == 8


def test_apply_writes_a_readable_library(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    code, stdout, _ = _run(capsys, ["library", "new-symbol", str(LOGIC),
                                    "-o", str(out), "--apply"])
    assert code == EXIT["OK"]
    assert out.exists()
    assert f"wrote {out}" in stdout

    sym = kicad_lib.read(out).symbols[0]
    assert sym.name == "GENERIC_LOGIC_14"
    assert len(sym.pins) == 14
    assert all(p.x_mil % 100 == 0 and p.y_mil % 100 == 0 for p in sym.pins)


def test_apply_twice_is_byte_stable_and_leaves_a_backup(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    argv = ["library", "new-symbol", str(LOGIC), "-o", str(out), "--apply"]
    assert main(argv) == EXIT["OK"]
    first = out.read_bytes()
    capsys.readouterr()
    assert main(argv) == EXIT["OK"]
    capsys.readouterr()
    assert out.read_bytes() == first
    assert (tmp_path / "lib.kicad_sym.bak").exists()


def test_default_output_name_comes_from_the_symbol(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, stdout, _ = _run(capsys, ["library", "new-symbol", str(OPAMP), "--apply"])
    assert code == EXIT["OK"]
    assert (tmp_path / "GENERIC_OPAMP_8.kicad_sym").exists()


def test_name_override_renames_the_symbol_and_the_default_file(capsys, tmp_path,
                                                               monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, _ = _run(capsys, ["library", "new-symbol", str(OPAMP),
                               "--name", "GENERIC_RENAMED", "--apply"])
    assert code == EXIT["OK"]
    target = tmp_path / "GENERIC_RENAMED.kicad_sym"
    assert target.exists()
    assert kicad_lib.read(target).symbols[0].name == "GENERIC_RENAMED"


# --------------------------------------------------------------------------- #
# --json payload
# --------------------------------------------------------------------------- #
def test_json_payload_carries_the_computed_geometry(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    code, stdout, _ = _run(capsys, ["library", "new-symbol", str(OPAMP),
                                    "-o", str(out), "--json"])
    assert code == EXIT["OK"]
    doc = json.loads(stdout)
    assert doc["schema_version"] == "1"
    assert doc["applied"] is False
    assert doc["symbol"] == "GENERIC_OPAMP_8"
    assert doc["reference_prefix"] == "U"
    assert set(doc["body_mil"]) == {"x0", "y0", "x1", "y1"}
    assert len(doc["pins"]) == 8
    one = next(p for p in doc["pins"] if p["number"] == "1")
    assert one == {"number": "1", "name": "OUT_A", "type": "output",
                   "side": "right", "group": "a", "hidden": False,
                   "stack_with": None, "at_mil": [400, 100], "orientation": 180}
    assert doc["findings"] == []


def test_json_applied_flag_is_true_after_a_write(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    code, stdout, _ = _run(capsys, ["library", "new-symbol", str(LOGIC),
                                    "-o", str(out), "--json", "--apply"])
    assert code == EXIT["OK"]
    assert json.loads(stdout)["applied"] is True


# --------------------------------------------------------------------------- #
# the write gate: coincidence is connectivity, not geometry
# --------------------------------------------------------------------------- #
def _colliding_layout(_spec) -> ss.Layout:
    """A layout that puts two DIFFERENT pins on one point (the field incident)."""
    gnd = ss.PinSpec(number="1", name="GND", type="power_in", side="bottom")
    nc = ss.PinSpec(number="2", name="NC", type="no_connect", side="bottom")
    return ss.Layout(
        name="GENERIC_BROKEN", reference_prefix="U",
        pins=[ss.PlacedPin(spec=gnd, x_mil=0, y_mil=-400, orientation=90),
              ss.PlacedPin(spec=nc, x_mil=0, y_mil=-400, orientation=90)],
        body=(-300, -300, 300, 300))


@pytest.mark.parametrize("extra", [[], ["--apply"]])
def test_coincident_output_refuses_the_write(capsys, tmp_path, monkeypatch, extra):
    monkeypatch.setattr(ss, "layout", _colliding_layout)
    out = tmp_path / "lib.kicad_sym"
    code, stdout, stderr = _run(
        capsys, ["library", "new-symbol", str(OPAMP), "-o", str(out), *extra])
    assert code == EXIT["OPLIST"], stdout
    assert not out.exists()
    assert "VERIFY_FAILED" in stderr
    assert "coincident-pin finding" in stderr
    assert "SYMLIB_PIN_COINCIDENT" in stdout


def test_layout_itself_refuses_to_build_a_coincidence():
    """Defence in depth: the gate above is the SECOND line, not the only one."""
    from akcli.errors import AkcliError

    lay = _colliding_layout(None)
    with pytest.raises(AkcliError) as ei:
        ss._assert_no_unsanctioned_coincidence(lay)
    assert ei.value.code == "INTERNAL"
    assert "electrical mis-join" in ei.value.message


# --------------------------------------------------------------------------- #
# structured refusals (one test each)
# --------------------------------------------------------------------------- #
_BASE_PIN = {"number": "1", "name": "A", "type": "input", "side": "left"}


@pytest.mark.parametrize("doc,needle", [
    ({"protocol_version": 1, "name": "X", "units": 4, "pins": [_BASE_PIN]},
     "multi-unit symbols are not supported yet"),
    ({"protocol_version": 1, "name": "X",
      "pins": [_BASE_PIN, dict(_BASE_PIN, side="right")]},
     "duplicate pin number '1'"),
    ({"protocol_version": 1, "name": "X",
      "pins": [dict(_BASE_PIN, type="analog")]},
     "is not a KiCad electrical type"),
    ({"protocol_version": 1, "name": "X", "pins": [
        dict(_BASE_PIN, number="1", name="VCC", type="power_in", side="top"),
        dict(_BASE_PIN, number="2", name="VDD", type="power_in", side="top",
             stack_with="1")]},
     "SAME name and the SAME electrical type"),
    ({"protocol_version": 1, "name": "X", "pins": []},
     "non-empty array"),
])
def test_spec_refusals_exit_usage_with_a_structured_message(
        capsys, tmp_path, doc, needle):
    spec = _spec_file(tmp_path, doc)
    code, _, stderr = _run(capsys, ["library", "new-symbol", str(spec)])
    assert code == EXIT["USAGE"], stderr
    assert "BAD_CONFIG" in stderr
    assert needle in stderr


def test_wrong_protocol_version_is_a_protocol_mismatch(capsys, tmp_path):
    spec = _spec_file(tmp_path, {"protocol_version": 99, "name": "X",
                                 "pins": [_BASE_PIN]})
    code, _, stderr = _run(capsys, ["library", "new-symbol", str(spec)])
    assert code == EXIT["OPLIST"]
    assert "PROTOCOL_MISMATCH" in stderr


def test_missing_spec_file_exits_not_found(capsys, tmp_path):
    code, _, stderr = _run(capsys, ["library", "new-symbol",
                                    str(tmp_path / "nope.json")])
    assert code == EXIT["NOT_FOUND"]
    assert "no such spec" in stderr


def test_missing_spec_argument_exits_usage(capsys):
    code, _, stderr = _run(capsys, ["library", "new-symbol"])
    assert code == EXIT["USAGE"]
    assert "missing input spec JSON" in stderr


def test_refusal_writes_nothing(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    spec = _spec_file(tmp_path, {"protocol_version": 1, "name": "X", "pins": []})
    _run(capsys, ["library", "new-symbol", str(spec), "-o", str(out), "--apply"])
    assert not out.exists()


# --------------------------------------------------------------------------- #
# lint-symbols
# --------------------------------------------------------------------------- #
def test_lint_symbols_reports_the_coincidence(capsys):
    code, stdout, _ = _run(capsys, ["library", "lint-symbols",
                                    str(FIX / "coincident.kicad_sym")])
    assert code == EXIT["FINDINGS"]
    assert "SYMLIB_PIN_COINCIDENT" in stdout
    assert "1 finding(s)" in stdout


def test_lint_symbols_is_quiet_on_a_clean_library(capsys):
    code, stdout, _ = _run(capsys, ["library", "lint-symbols",
                                    str(FIX / "stacked_ok.kicad_sym")])
    assert code == EXIT["OK"]
    assert "no findings" in stdout


def test_lint_symbols_json_shape(capsys):
    code, stdout, _ = _run(capsys, ["library", "lint-symbols",
                                    str(FIX / "unspecified_type.kicad_sym"),
                                    "--json"])
    assert code == EXIT["FINDINGS"]          # the off-grid WARNING trips it
    doc = json.loads(stdout)
    assert doc["schema_version"] == "1"
    codes = {f["code"] for f in doc["findings"]}
    assert codes == {"SYMLIB_PIN_TYPE_UNSPECIFIED", "SYMLIB_PIN_OFF_GRID"}
    assert all(f["anchors"] for f in doc["findings"])


def test_lint_symbols_honours_the_exit_policy(capsys):
    lib = str(FIX / "unspecified_type.kicad_sym")
    assert main(["library", "lint-symbols", lib, "--fail-on", "never"]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["library", "lint-symbols", lib,
                 "--fail-on", "note"]) == EXIT["FINDINGS"]
    capsys.readouterr()
    assert main(["library", "lint-symbols", lib,
                 "--fail-on", "error"]) == EXIT["OK"]
    capsys.readouterr()


def test_lint_symbols_missing_file_exits_not_found(capsys, tmp_path):
    code, _, stderr = _run(capsys, ["library", "lint-symbols",
                                    str(tmp_path / "nope.kicad_sym")])
    assert code == EXIT["NOT_FOUND"]


def test_lint_symbols_missing_argument_exits_usage(capsys):
    code, _, stderr = _run(capsys, ["library", "lint-symbols"])
    assert code == EXIT["USAGE"]


# --------------------------------------------------------------------------- #
# the loop closes: generate -> lint
# --------------------------------------------------------------------------- #
def test_generated_library_lints_clean(capsys, tmp_path):
    out = tmp_path / "lib.kicad_sym"
    assert main(["library", "new-symbol", str(LOGIC), "-o", str(out),
                 "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["library", "lint-symbols", str(out)]) == EXIT["OK"]
    assert "no findings" in capsys.readouterr().out


def test_apply_refuses_a_gui_locked_target(capsys, tmp_path):
    from akcli.writers.kicad import gui_lock_path

    out = tmp_path / "lib.kicad_sym"
    shutil.copyfile(FIX / "stacked_ok.kicad_sym", out)
    lock = gui_lock_path(out)
    lock.write_text("{}", encoding="utf-8")
    code, _, stderr = _run(capsys, ["library", "new-symbol", str(OPAMP),
                                    "-o", str(out), "--apply"])
    assert code == EXIT["OPLIST"]
    assert "TARGET_LOCKED" in stderr
    assert out.read_text(encoding="utf-8").startswith("(kicad_symbol_lib")
    assert "GENERIC_AMP_8_STACKED" in out.read_text(encoding="utf-8")
