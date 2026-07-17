"""BOM P2-3 diff --bom, P3-1 waiver transparency, P3-2 markdown report."""

from __future__ import annotations

from akcli import cli, model
from akcli.parts import bom_jlc
from akcli.parts.search import Part
from akcli.report import Finding, Severity, apply_waivers


def _comp(ref, value="10k", params=None, dnp=False):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint="R_0402_1005Metric", unique_id=ref,
        parameters=params or {}, dnp=dnp)


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


# --------------------------------------------------------------------------- #
# P2-3 BOM line-level diff
# --------------------------------------------------------------------------- #
def test_bom_diff_kinds():
    a = _sch([
        _comp("R1", params={"LCSC": "C11111"}),
        _comp("R2", value="1k"),
        _comp("C1", value="100n"),
        _comp("R7"),
    ])
    b = _sch([
        _comp("R1", params={"LCSC": "C99999"}),        # id edit
        _comp("R2", value="4.7k"),                     # value edit
        _comp("C1", value="100n", dnp=True),           # fitted -> dnp
        _comp("R8", value="220"),                      # new line (R7 gone)
    ])
    kinds = {(d["kind"], d["refs"]) for d in bom_jlc.bom_diff(a, b)}
    assert ("id_changed", "R1") in kinds
    assert ("value_changed", "R2") in kinds
    assert ("class_changed", "C1") in kinds
    assert ("line_added", "R8") in kinds
    assert ("line_removed", "R7") in kinds
    # identical revisions diff to nothing
    assert bom_jlc.bom_diff(a, a) == []


def test_cli_diff_bom_flag(tmp_path, capsys):
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    a = tmp_path / "a.kicad_sch"
    b = tmp_path / "b.kicad_sch"
    shutil.copy(fixture, a)
    shutil.copy(fixture, b)
    import json
    ops = [{"op": "set_component_parameters", "designator": "R1",
            "value": "22k"}]
    opsfile = tmp_path / "ops.json"
    opsfile.write_text(json.dumps({"protocol_version": 1,
                                   "target_format": "kicad",
                                   "target_file": "x", "ops": ops}),
                       encoding="utf-8", newline="\n")
    assert cli.main(["draw", str(b), "--ops", str(opsfile), "--apply"]) == 0
    capsys.readouterr()
    assert cli.main(["diff", str(a), str(b), "--bom", "--exit-zero"]) == 0
    out = capsys.readouterr().out
    assert "BOM changes" in out
    assert "value_changed: R1 — 10k -> 22k" in out


# --------------------------------------------------------------------------- #
# P3-1 waiver transparency
# --------------------------------------------------------------------------- #
def test_apply_waivers_details():
    findings = [
        Finding(code="BOM_MISSING_VALUE", severity=Severity.WARNING,
                message="x", refs=["R1"]),
        Finding(code="BOM_REFDES_GAP", severity=Severity.NOTE,
                message="y", refs=["R3"]),
    ]
    waivers = [{"code": "BOM_MISSING_VALUE", "severity": "off",
                "reason": "prototype only"}]
    detail: list[dict] = []
    kept, waived, demoted = apply_waivers(findings, waivers, detail_out=detail)
    assert waived == 1 and demoted == 0 and len(kept) == 1
    (d,) = detail
    assert d["code"] == "BOM_MISSING_VALUE"
    assert d["refs"] == ["R1"]
    assert d["action"] == "dropped"
    assert d["reason"] == "prototype only"


def test_check_names_waived_findings(tmp_path, capsys):
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    work = tmp_path / "t.kicad_sch"
    shutil.copy(fixture, work)
    (tmp_path / "akcli.toml").write_text(
        '[[waiver]]\ncode = "BOM_MISSING_PART_ID"\nseverity = "off"\n'
        'reason = "ids arrive at layout time"\n',
        encoding="utf-8", newline="\n")
    capsys.readouterr()
    assert cli.main(["check", str(work), "--bom", "-C",
                     str(tmp_path / "akcli.toml"), "--exit-zero"]) == 0
    captured = capsys.readouterr()
    # every silenced finding is NAMED with the waiver's reason
    assert "waived: BOM_MISSING_PART_ID" in captured.err
    assert "ids arrive at layout time" in captured.err
    assert "config-waived: 3" in captured.out


# --------------------------------------------------------------------------- #
# P3-2 markdown report
# --------------------------------------------------------------------------- #
def test_markdown_report_shape():
    def _part(lcsc):
        return Part(lcsc=lcsc, mpn="X" + lcsc,
                    description="10kΩ ±1% 0402 Chip Resistor",
                    package="0402", stock=5000, price=0.01, basic=False,
                    datasheet=None, category="", attributes={})

    sch = _sch([_comp("R1", params={"LCSC": "C11111"}),
                _comp("TP1", value="TestPoint")])
    lines = bom_jlc.check(sch, get=lambda c: _part(c), find=lambda q, **k: [])
    agg = bom_jlc.totals(lines)
    md = bom_jlc.to_markdown(lines, agg, qty=2)
    assert md.startswith("# BOM report")
    assert "1 fitted" in md and "1 no-part" in md
    assert "fitted coverage: 100%" in md
    assert "| R1 |" in md and "| TP1 |" in md
    assert "Extended lines" in md            # economics surfaced
