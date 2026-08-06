"""`akcli ops from-intent` CLI + the round-trip property (the acceptance test).

The heart of the feature: a design document generates an op-list that `plan`
accepts, `draw --apply` writes, and the drawn sheet's netlist EQUALS the
design's nets table — asserted three ways (normalized `nets
--intent-snapshot`, `check --intent <the same design file>` with zero
findings, and the layout lint clean of overlap classes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli.drivers import kicad_cli

from tests._cli_harness import json_of, run_cli

ROOT = Path(__file__).resolve().parents[1]
SYMS = ROOT / "tests" / "fixtures" / "kicad" / "symbols"
GENERIC = str(SYMS / "Generic.kicad_sym")
DEVICE = str(SYMS / "Device.kicad_sym")

_DESIGN = {
    "protocol_version": 1,
    "nets": {
        "RAIL_A": ["U1.2", "C1.1", "J1.1"],
        "RAIL_IN": ["U1.3", "J1.2"],
        "GND": ["U1.1", "C1.2", "J1.4", "U2.1"],
        "SIG_N": ["U2.2", "J1.3"],
    },
    "components": {
        "U1": {"lib_id": "Generic:REG3", "value": "REG-ADJ"},
        "C1": {"lib_id": "Device:C", "value": "10u"},
        "J1": {"lib_id": "Generic:CONN_1x4"},
        "U2": {"lib_id": "Generic:DUAL_BUF"},
    },
}


def _write_design(tmp_path: Path, doc: dict | None = None) -> Path:
    p = tmp_path / "design.json"
    p.write_text(json.dumps(doc or _DESIGN), encoding="utf-8", newline="\n")
    return p


def _generate(tmp_path: Path, design: Path) -> Path:
    out = tmp_path / "ops.json"
    rc, _, err = run_cli(["ops", "from-intent", str(design), "-o", str(out),
                          "--symbols", GENERIC, "--symbols", DEVICE])
    assert rc == 0, err
    assert "from-intent:" in err          # stderr summary, stdout untouched
    return out


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_stdout_is_the_oplist(tmp_path):
    design = _write_design(tmp_path)
    rc, out, err = run_cli(["ops", "from-intent", str(design),
                            "--symbols", GENERIC, "--symbols", DEVICE])
    assert rc == 0, err
    doc = json.loads(out)                 # stdout is pure op-list JSON
    assert doc["protocol_version"] == 1 and doc["ops"]


def test_output_file_matches_stdout(tmp_path):
    design = _write_design(tmp_path)
    rc, out, _ = run_cli(["ops", "from-intent", str(design),
                          "--symbols", GENERIC, "--symbols", DEVICE])
    assert rc == 0
    op_file = _generate(tmp_path, design)
    assert json.loads(op_file.read_text(encoding="utf-8")) == json.loads(out)


def test_missing_design_arg_is_usage(tmp_path):
    rc, _, err = run_cli(["ops", "from-intent"])
    assert rc == 2
    assert "design" in err


def test_missing_design_file_exits_4(tmp_path):
    rc, _, _ = run_cli(["ops", "from-intent", str(tmp_path / "nope.json")])
    assert rc == 4


def test_refusal_surfaces_design_unsupported(tmp_path):
    design = _write_design(tmp_path, {
        "protocol_version": 1,
        "nets": {"A": ["U1.IN"]},                 # pin by NAME
        "components": {"U1": {"lib_id": "Generic:REG3"}},
    })
    rc, _, err = run_cli(["ops", "from-intent", str(design),
                          "--symbols", GENERIC])
    assert rc == 6
    assert "DESIGN_UNSUPPORTED" in err and "U1.3" in err


def test_unknown_lib_id_exits_6_symbol_not_found(tmp_path):
    design = _write_design(tmp_path, {
        "protocol_version": 1,
        "nets": {"A": ["U1.1"]},
        "components": {"U1": {"lib_id": "No:Such"}},
    })
    rc, _, err = run_cli(["ops", "from-intent", str(design)])
    assert rc == 6
    assert "SYMBOL_NOT_FOUND" in err


# --------------------------------------------------------------------------- #
# the round-trip property
# --------------------------------------------------------------------------- #
def _round_trip(tmp_path: Path, design_doc: dict) -> tuple[Path, Path]:
    """design -> ops -> plan (accepted) -> fresh sheet -> draw --apply."""
    design = _write_design(tmp_path, design_doc)
    op_file = _generate(tmp_path, design)
    target = tmp_path / "board.kicad_sch"
    rc, _, err = run_cli(["new", str(target)])
    assert rc == 0, err
    plan = json_of(["plan", str(target), "--ops", str(op_file),
                    "--symbols", GENERIC, "--symbols", DEVICE, "--json"],
                   expect=0)
    assert plan["status"] == "dry-run"
    assert all(op["status"] == "ok" for op in plan["ops"])
    drew = json_of(["draw", str(target), "--ops", str(op_file),
                    "--symbols", GENERIC, "--symbols", DEVICE,
                    "--apply", "--json"], expect=0)
    assert drew["status"] == "applied"
    return design, target


def test_round_trip_nets_equal_and_intent_clean(tmp_path):
    design, target = _round_trip(tmp_path, _DESIGN)

    # (1) the drawn netlist ≡ the design's nets table (normalized: named
    #     nets only, sorted members)
    rc, out, err = run_cli(["nets", str(target), "--intent-snapshot", "-"])
    assert rc == 0, err
    snapshot = json.loads(out)
    assert ({k: sorted(v) for k, v in snapshot["nets"].items()}
            == {k: sorted(v) for k, v in _DESIGN["nets"].items()})

    # (2) the SAME design file asserts clean over the drawn sheet
    verdict = json_of(["check", str(target), "--intent", str(design),
                       "--json"], expect=0)
    assert verdict["findings"] == []

    # (3) layout: no ERROR-class findings, and none of the overlap classes
    #     the generator's cell sizing exists to prevent (even as warnings)
    layout = json_of(["check", str(target), "--layout", "--json"],
                     expect=(0, 1))
    assert not [f for f in layout["findings"]
                if f["severity"] in ("error", "critical")]
    assert not [f for f in layout["findings"]
                if f["code"] in ("LAYOUT_SYMBOL_OVERLAP",
                                 "LAYOUT_LABEL_OVER_SYMBOL",
                                 "LAYOUT_LABEL_OVERLAP")]


def test_round_trip_reapply_is_byte_identical(tmp_path):
    """The generated op-list obeys the writer's idempotency invariant."""
    _, target = _round_trip(tmp_path, _DESIGN)
    before = target.read_bytes()
    rc, _, err = run_cli(["draw", str(target), "--ops",
                          str(tmp_path / "ops.json"),
                          "--symbols", GENERIC, "--symbols", DEVICE,
                          "--apply"])
    assert rc == 0, err
    assert target.read_bytes() == before


def test_round_trip_grouped(tmp_path):
    doc = dict(_DESIGN)
    doc["layout"] = {"group": "FRONTEND", "origin": [2000, 2000]}
    design, target = _round_trip(tmp_path, doc)
    verdict = json_of(["check", str(target), "--intent", str(design),
                       "--json"], expect=0)
    assert verdict["findings"] == []
    # group membership persisted on the sheet (hidden Group property)
    rc, out, _ = run_cli(["groups", str(target)])
    assert rc == 0 and "FRONTEND" in out


def test_round_trip_drawn_file_accepted_by_kicad_cli(tmp_path):
    """A real ``kicad-cli`` (CI KiCad job) must accept the generated sheet."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    _, target = _round_trip(tmp_path, _DESIGN)
    report = kicad_cli.erc(str(target))
    assert report is not None
