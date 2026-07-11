"""Round-3 CLI integration tests: nets/intent, net-diff rails, relink, jlc CSV.

Covers the newly wired surface end-to-end through ``cli.main``:

* ``akcli nets`` (text + ``--json``) and the ``--intent-snapshot`` ->
  ``check --intent`` round-trip (clean pass, then a mutated intent that fails);
* the plan/draw before/after net connectivity diff — a ``! SPLIT`` warning in
  dry-run ``plan`` output when an op-list deletes a net-defining label,
  ``--no-net-diff`` opt-out, and the ``draw --apply --strict-nets`` refusal
  (exit ``OPLIST``, file untouched);
* the explicit dry-run / APPLIED / REFUSED status line on plan/draw;
* ``akcli relink-symbols`` dry-run listing + ``--apply`` (fixture text reused
  from ``tests.test_relink``);
* ``jlc bom --csv`` golden-file export with an injected (offline) finder;
* difflib did-you-mean for unknown calc/ops names, the enumerated bare-``jlc``
  usage error, and ``--json`` envelopes for ``ops list``/``undo``/``arrange``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from altium_kicad_cli.cli import main
from altium_kicad_cli.errors import EXIT

FIXTURES = Path(__file__).parent / "fixtures"
V8 = FIXTURES / "kicad" / "board_v8.kicad_sch"


def _copy_v8(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    tgt = tmp_path / name
    shutil.copy(V8, tgt)
    return tgt


def _ops_file(tmp_path: Path, ops: list, name: str = "ops.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"protocol_version": 1, "target_format": "kicad",
                             "target_file": "board.kicad_sch", "ops": ops}),
                 encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# nets
# --------------------------------------------------------------------------- #
def test_nets_text(capsys):
    assert main(["nets", str(V8)]) == EXIT["OK"]
    out = capsys.readouterr().out
    # one line per net, members sorted
    assert "+3V3: #PWR01.1, R1.1" in out
    assert "GND: #PWR02.1, C1.2, R2.2" in out
    assert "MID: C1.1, R1.2, R2.1" in out


def test_nets_json(capsys):
    assert main(["nets", str(V8), "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["source"] == str(V8)
    by_name = {n["name"]: n for n in doc["nets"]}
    assert by_name["GND"]["members"] == ["#PWR02.1", "C1.2", "R2.2"]
    assert all("stable_id" in n for n in doc["nets"])


def test_nets_missing_path_is_usage(capsys):
    assert main(["nets"]) == EXIT["USAGE"]


# --------------------------------------------------------------------------- #
# intent snapshot -> check --intent round-trip
# --------------------------------------------------------------------------- #
def test_intent_snapshot_roundtrip(tmp_path, capsys):
    snap = tmp_path / "intent.json"
    assert main(["nets", str(V8), "--intent-snapshot", str(snap)]) == EXIT["OK"]
    capsys.readouterr()
    doc = json.loads(snap.read_text(encoding="utf-8"))
    assert doc["protocol_version"] == 1 and doc["mode"] == "exact"
    assert set(doc["nets"]) == {"+3V3", "GND", "MID"}

    # --intent alone is a pure intent assertion: the round-trip is clean
    assert main(["check", str(V8), "--intent", str(snap)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "INTENT_" not in out

    # mutate the intent: a pin the schematic does not have -> ERROR finding
    doc["nets"]["GND"].append("R1.99")
    snap.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["check", str(V8), "--intent", str(snap)]) == EXIT["FINDINGS"]
    out = capsys.readouterr().out
    assert "INTENT_PIN_UNKNOWN" in out


def test_intent_snapshot_stdout(capsys):
    assert main(["nets", str(V8), "--intent-snapshot", "-"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "exact" and "GND" in doc["nets"]


def test_check_intent_missing_file_exits_not_found(tmp_path):
    assert main(["check", str(V8), "--intent",
                 str(tmp_path / "nope.json")]) == EXIT["NOT_FOUND"]


# --------------------------------------------------------------------------- #
# net-diff rails on plan/draw
# --------------------------------------------------------------------------- #
def _split_board(tmp_path: Path, capsys) -> tuple[Path, Path]:
    """A board where one THR net (4 pins) exists only via two same-name labels.

    Returns ``(target, ops2)`` where ``ops2`` deletes one of the labels —
    splitting THR into a named 2-pin net and an unnamed 2-pin net.
    """
    tgt = _copy_v8(tmp_path)
    ops1 = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R7",
         "x_mil": 7000, "y_mil": 2000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R8",
         "x_mil": 9000, "y_mil": 2000},
        {"op": "add_wire", "vertices": ["R7.1", "R8.1"]},
        {"op": "add_wire", "vertices": ["R7.2", "R8.2"]},
        {"op": "add_net_label", "name": "THR", "at": "R7.1"},
        {"op": "add_net_label", "name": "THR", "at": "R7.2"},
    ], name="ops1.json")
    assert main(["draw", str(tgt), "--ops", str(ops1),
                 "--apply", "--json"]) == EXIT["OK"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    (label2_uuid,) = payload["ops"][5]["created_uuids"]
    ops2 = _ops_file(tmp_path, [{"op": "delete_object", "uuid": label2_uuid}],
                     name="ops2.json")
    return tgt, ops2


def test_plan_reports_split_of_named_net(tmp_path, capsys):
    tgt, ops2 = _split_board(tmp_path, capsys)
    before = tgt.read_bytes()
    assert main(["plan", str(tgt), "--ops", str(ops2)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "Net changes:" in out
    assert "! SPLIT THR" in out                     # the net-defining label died
    assert "status: dry-run — nothing written" in out
    assert tgt.read_bytes() == before               # plan never writes


def test_strict_nets_refuses_apply(tmp_path, capsys):
    tgt, ops2 = _split_board(tmp_path, capsys)
    before = tgt.read_bytes()
    rc = main(["draw", str(tgt), "--ops", str(ops2), "--apply", "--strict-nets"])
    err = capsys.readouterr().err
    assert rc == EXIT["OPLIST"]                     # same gate as op/verify errors
    assert tgt.read_bytes() == before               # nothing written
    assert "strict-nets" in err and "REFUSED" in err
    assert "SPLIT THR" in err                       # the evidence is shown

    # without --strict-nets the same edit applies (diff stays advisory)
    assert main(["draw", str(tgt), "--ops", str(ops2), "--apply"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "! SPLIT THR" in out
    assert "status: APPLIED" in out and "akcli undo" in out
    assert tgt.read_bytes() != before


def test_no_net_diff_opt_out(tmp_path, capsys):
    tgt, ops2 = _split_board(tmp_path, capsys)
    assert main(["plan", str(tgt), "--ops", str(ops2),
                 "--no-net-diff"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "Net changes:" not in out


def test_plan_json_net_diff_envelope(tmp_path, capsys):
    tgt, ops2 = _split_board(tmp_path, capsys)
    assert main(["plan", str(tgt), "--ops", str(ops2), "--json"]) == EXIT["OK"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry-run" and payload["applied"] is False
    nd = payload["net_diff"]
    assert nd["risk"] is True and nd["equivalent"] is False
    assert any("SPLIT THR" in ln for ln in nd["lines"])


def test_equivalent_edit_prints_none(tmp_path, capsys):
    # moving nothing / pure-annotation op-list: net diff shows "(none)"
    tgt = _copy_v8(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "set_component_parameters", "designator": "R1",
         "parameters": {"MPN": "X123"}},
    ])
    assert main(["draw", str(tgt), "--ops", str(ops)]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "Net changes:" in out and "(none)" in out
    assert "status: dry-run — nothing written (re-run with --apply)" in out


def test_refused_status_line_on_failed_apply(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:NOPE", "designator": "Z1",
         "x_mil": 1000, "y_mil": 1000},
    ])
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    out = capsys.readouterr().out
    assert rc == EXIT["OPLIST"]
    assert "status: REFUSED — nothing written" in out


# --------------------------------------------------------------------------- #
# relink-symbols
# --------------------------------------------------------------------------- #
def _relink_proj(tmp_path: Path) -> tuple[Path, Path]:
    """(schematic, libdir) — the stale/fresh/ghost trio from tests.test_relink."""
    from tests.test_relink import _lib_text, _sch_text
    libdir = tmp_path / "libs"
    libdir.mkdir()
    (libdir / "Fake.kicad_sym").write_text(_lib_text(), encoding="utf-8")
    sch = tmp_path / "board.kicad_sch"
    sch.write_text(_sch_text(), encoding="utf-8")
    return sch, libdir


def test_relink_symbols_dry_run_listing(tmp_path, capsys):
    sch, libdir = _relink_proj(tmp_path)
    before = sch.read_bytes()
    rc = main(["relink-symbols", str(sch), "--libs", str(libdir)])
    out = capsys.readouterr().out
    assert rc == EXIT["OPLIST"]                     # Ghost:X has no source lib
    assert sch.read_bytes() == before               # dry-run: untouched
    assert "replace" in out and "Fake:R2" in out
    assert "up-to-date" in out and "Fake:C2" in out
    assert "missing-lib" in out and "Ghost:X" in out
    assert "re-run with --apply" in out


def test_relink_symbols_only_scopes_and_applies(tmp_path, capsys):
    sch, libdir = _relink_proj(tmp_path)
    # scoped to Fake, the missing Ghost nick no longer poisons the exit code
    rc = main(["relink-symbols", str(sch), "--libs", str(libdir),
               "--only", "Fake", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == EXIT["OK"]
    assert doc["applied"] is False
    assert {a["lib_id"]: a["status"] for a in doc["actions"]} == {
        "Fake:R2": "replace", "Fake:C2": "up-to-date"}
    assert all("new_sexpr" not in a for a in doc["actions"])

    rc = main(["relink-symbols", str(sch), "--libs", str(libdir),
               "--only", "Fake", "--apply"])
    out = capsys.readouterr().out
    assert rc == EXIT["OK"]
    assert "status: APPLIED" in out and "1 symbol(s)" in out
    assert (tmp_path / "board.kicad_sch.bak").exists()
    # idempotent: a re-plan finds everything current
    rc = main(["relink-symbols", str(sch), "--libs", str(libdir),
               "--only", "Fake", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert {a["status"] for a in doc["actions"]} == {"up-to-date"}


# --------------------------------------------------------------------------- #
# jlc bom --csv (offline: injected finder)
# --------------------------------------------------------------------------- #
def _catalog_part(lcsc: str):
    from altium_kicad_cli.parts.search import Part
    return Part(lcsc=lcsc, mpn="0402WGF1002TCE", description="10k 1% 0402",
                package="0402", stock=5000, price=0.001, basic=True,
                datasheet=None, category="Resistors", attributes={})


def test_jlc_bom_csv_golden(tmp_path, capsys, monkeypatch):
    from altium_kicad_cli.parts import search as parts_search
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: _catalog_part(lcsc))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])

    tgt = _copy_v8(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "set_component_parameters", "designator": "R1",
         "parameters": {"LCSC": "C25744"}},
    ])
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()

    out_csv = tmp_path / "bom.csv"
    assert main(["jlc", "bom", str(tgt), "--csv", str(out_csv),
                 "--exit-zero"]) == EXIT["OK"]
    err = capsys.readouterr().err
    assert f"wrote JLCPCB BOM CSV: {out_csv}" in err
    # golden: resolved line carries its C-number, no-part-id lines stay blank
    assert out_csv.read_text(encoding="utf-8") == (
        "Comment,Designator,Footprint,LCSC Part #\n"
        "100n,C1,C_0402_1005Metric,\n"
        "10k,R1,R_0402_1005Metric,C25744\n"
        "10k,R2,R_0402_1005Metric,\n"
    )


def test_jlc_bom_csv_stdout_is_pure_csv(tmp_path, capsys, monkeypatch):
    from altium_kicad_cli.parts import search as parts_search
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: _catalog_part(lcsc))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    tgt = _copy_v8(tmp_path)
    assert main(["jlc", "bom", str(tgt), "--csv", "-",
                 "--exit-zero"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert out.startswith("Comment,Designator,Footprint,LCSC Part #")
    assert "REFS" not in out                        # the table stays off stdout


# --------------------------------------------------------------------------- #
# UX: did-you-mean, usage errors, --json envelopes
# --------------------------------------------------------------------------- #
def test_calc_unknown_did_you_mean(capsys):
    assert main(["calc", "eserie"]) == EXIT["USAGE"]
    err = capsys.readouterr().err
    assert "did you mean" in err and "eseries" in err


def test_calc_info_unknown_did_you_mean(capsys):
    assert main(["calc", "info", "trackwith"]) == EXIT["USAGE"]
    err = capsys.readouterr().err
    assert "did you mean" in err and "trackwidth" in err


def test_ops_template_unknown_did_you_mean(capsys):
    assert main(["ops", "template", "add_wir"]) == EXIT["USAGE"]
    err = capsys.readouterr().err
    assert "did you mean" in err and "add_wire" in err


def test_jlc_bare_enumerates_subcommands(capsys):
    assert main(["jlc"]) == EXIT["USAGE"]
    err = capsys.readouterr().err
    for sub in ("jlc search", "jlc show", "jlc bom", "jlc add"):
        assert sub in err


def test_ops_list_json_envelope(capsys):
    assert main(["ops", "list", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    ops_by_name = {o["name"]: o for o in doc["ops"]}
    assert "place_component" in ops_by_name
    assert "lib_id" in ops_by_name["place_component"]["required"]
    assert any(m["name"] == "place_divider" for m in doc["macros"])


def test_undo_json_envelope(tmp_path, capsys):
    tgt = _copy_v8(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "set_component_parameters", "designator": "R1",
         "parameters": {"MPN": "X1"}},
    ])
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["undo", str(tgt), "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is False and doc["backup"].endswith(".bak")
    assert main(["undo", str(tgt), "--apply", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is True and "summary" in doc


def test_arrange_apply_json_envelope(tmp_path, capsys):
    # two overlapping free resistors -> one move, applied, JSON envelope
    tgt = _copy_v8(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "place_component", "lib_id": "Device:R", "designator": "R7",
         "x_mil": 8000, "y_mil": 2000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R8",
         "x_mil": 8000, "y_mil": 2000},
    ])
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    assert main(["arrange", str(tgt), "--apply", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] is True and len(doc["moves"]) >= 1
    assert doc["anchored_overlaps"] == []


def test_reader_warnings_reach_stderr(tmp_path, capsys):
    # duplicate same-unit placement under a shared designator: the reader's
    # warning (Schematic.warnings) must surface on stderr, not stdout
    import re
    src = V8.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"(?m)^\t\(symbol\n", src)]
    ends = starts[1:] + [src.rfind(")")]
    r1 = next(src[a:b] for a, b in zip(starts, ends)
              if '(reference "R1")' in src[a:b])
    c = iter(range(99))
    dup = re.sub(r'\(uuid "[^"]+"\)',
                 lambda m: f'(uuid "aaaaaa{next(c):02d}-0000-4000-8000-'
                           '000000000000")', r1)
    at = re.search(r"\(at ([\d.]+) ([\d.]+) ", dup)
    dup = dup.replace(at.group(0),
                      f"(at {float(at.group(1)) + 25.4} {at.group(2)} ", 1)
    tgt = tmp_path / "dup.kicad_sch"
    tgt.write_text(src[:src.rfind(")")] + dup + ")\n", encoding="utf-8")

    assert main(["nets", str(tgt)]) == EXIT["OK"]
    captured = capsys.readouterr()
    assert "duplicate designator 'R1'" in captured.err
    assert "duplicate designator" not in captured.out   # stdout stays data


def test_top_level_help_has_workflow_epilog(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "typical workflow" in out and "akcli plan" in out


# --------------------------------------------------------------------------- #
# review-round regressions: net-diff on hierarchical roots, fail-closed strict
# --------------------------------------------------------------------------- #
def _hier_root(tmp_path: Path, with_child: bool = True) -> Path:
    """board_v8 with a (sheet) reference spliced in. The net-diff dry-apply
    used to copy the root alone into a TemporaryDirectory, so read-back died
    on the missing child and --strict-nets silently failed OPEN."""
    root = _copy_v8(tmp_path)
    if with_child:
        (tmp_path / "child.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "akcli") '
            '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n',
            encoding="utf-8")
    text = root.read_text(encoding="utf-8")
    sheet_node = (
        '  (sheet (at 200 200) (size 20 20)\n'
        '    (uuid "99999999-8888-7777-6666-555555555555")\n'
        '    (property "Sheetname" "child" (at 0 0 0)'
        ' (effects (font (size 1.27 1.27))))\n'
        '    (property "Sheetfile" "child.kicad_sch" (at 0 0 0)'
        ' (effects (font (size 1.27 1.27)))))\n')
    k = text.rstrip().rfind(")")
    root.write_text(text[:k] + sheet_node + text[k:], encoding="utf-8")
    return root


def test_strict_nets_gate_works_on_hierarchical_root(tmp_path, capsys):
    root = _hier_root(tmp_path)
    ops = _ops_file(tmp_path, [
        {"op": "add_wire", "vertices": ["R1.1", "R1.2"]},   # +3V3 <-> MID short
    ])
    before = root.read_bytes()
    rc = main(["draw", str(root), "--ops", str(ops), "--apply",
               "--strict-nets"])
    err = capsys.readouterr().err
    assert rc == EXIT["OPLIST"]
    assert "REFUSED" in err and "MERGE" in err
    assert root.read_bytes() == before                      # nothing written
    assert not list(tmp_path.glob("*.netdiff.*"))           # temp copy cleaned


def test_strict_nets_fails_closed_when_diff_unavailable(tmp_path, capsys):
    root = _hier_root(tmp_path, with_child=False)           # read_sch raises
    ops = _ops_file(tmp_path, [
        {"op": "add_wire", "vertices": ["R1.1", "R1.2"]},
    ])
    before = root.read_bytes()
    rc = main(["draw", str(root), "--ops", str(ops), "--apply",
               "--strict-nets"])
    err = capsys.readouterr().err
    assert rc == EXIT["OPLIST"]
    assert "REFUSED" in err and "net diff unavailable" in err
    assert root.read_bytes() == before                      # fail CLOSED


def test_missing_diff_warns_but_applies_without_strict(tmp_path, capsys):
    root = _hier_root(tmp_path, with_child=False)
    ops = _ops_file(tmp_path, [
        {"op": "add_wire", "vertices": ["R1.1", "R1.2"]},
    ])
    before = root.read_bytes()
    rc = main(["draw", str(root), "--ops", str(ops), "--apply"])
    err = capsys.readouterr().err
    assert rc == EXIT["OK"]
    assert "WARNING: net diff unavailable" in err           # loud, not silent
    assert root.read_bytes() != before                      # advisory only
