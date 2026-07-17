"""BOM P1 — the four-population semantic model, end to end.

The acceptance scenario mirrors the kind of real board that exposed the
gaps: a healthy fitted majority with explicit LCSC
ids, two genuinely missing caps (C16/C17), one externally-sourced module
(U1, bought outside JLCPCB), a wall of test points, and two bare solder
pads excluded via ``in_bom=no``. The board must stop reporting "0 findings /
26 rows of noise" and start saying exactly: C16/C17 are the holes, the rest
is structural or external, coverage is computed over FITTED parts only.
"""

from __future__ import annotations

import csv
import io

from akcli import bom_policy, model
from akcli.checks import bom as bom_check
from akcli.parts import bom_jlc
from akcli.parts.search import Part


def _comp(ref, value="10k", footprint="R_0402_1005Metric", params=None,
          dnp=False, in_bom=True, on_board=True, uid=None):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint=footprint, unique_id=uid or ref,
        parameters=params or {}, dnp=dnp, in_bom=in_bom, on_board=on_board)


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


def _part(lcsc, description="10kΩ ±1% 0402 Chip Resistor", package="0402"):
    return Part(lcsc=lcsc, mpn="X" + lcsc, description=description,
                package=package, stock=5000, price=0.01, basic=True,
                datasheet=None, category="", attributes={})


def _pod():
    """A mixed-population board: fitted / holes / external / dnp / structural."""
    comps = []
    # ten healthy fitted parts with explicit ids
    for i in range(1, 11):
        comps.append(_comp(f"R{i}", params={"LCSC": f"C100{i:02d}"}))
    # the two real holes: 8pF caps nobody filled in
    comps.append(_comp("C16", value="8pF", footprint="C_0402_1005Metric"))
    comps.append(_comp("C17", value="8pF", footprint="C_0402_1005Metric"))
    # externally-bought radio module
    comps.append(_comp("U1", value="RF-MOD-01", footprint="RF_Module_SMD",
                       params={"Sourcing": "external:vendor official store"}))
    # a DNP'd debug resistor that still carries an id
    comps.append(_comp("R99", dnp=True, params={"LCSC": "C25804"}))
    # structural: test points + bare pads excluded from the BOM
    for i in range(1, 23):
        comps.append(_comp(f"TP{i}", value="TestPoint",
                           footprint="TestPoint_Pad_D1.0mm"))
    comps.append(_comp("J3", value="SolderPad", footprint="Pad", in_bom=False))
    comps.append(_comp("J4", value="SolderPad", footprint="Pad", in_bom=False))
    return _sch(comps)


# --------------------------------------------------------------------------- #
# classification (P1-1/P1-2/P1-4)
# --------------------------------------------------------------------------- #
def test_classify_all_four_populations():
    assert bom_policy.classify(_comp("R1"))[0] == "fitted"
    assert bom_policy.classify(_comp("R9", dnp=True))[0] == "dnp"
    assert bom_policy.classify(_comp("TP3"))[0] == "no-part"
    assert bom_policy.classify(_comp("J3", in_bom=False))[0] == "no-part"
    klass, note = bom_policy.classify(
        _comp("U1", params={"Sourcing": "external:vendor official store"}))
    assert klass == "external" and note == "vendor official store"
    assert bom_policy.classify(
        _comp("X1", params={"BOM_Sourcing": "consigned"}))[0] == "external"
    assert bom_policy.classify(
        _comp("X2", params={"sourcing": "no-part"}))[0] == "no-part"
    # a DNP'd test point is still a test point
    assert bom_policy.classify(_comp("TP9", dnp=True))[0] == "no-part"
    # unknown channel never silently drops a part
    assert bom_policy.classify(
        _comp("R2", params={"Sourcing": "lcsc-typo??"}))[0] == "fitted"


# --------------------------------------------------------------------------- #
# check --bom (P1-2/P1-5): exactly C16/C17, everything else quiet
# --------------------------------------------------------------------------- #
def test_pod_reports_exactly_the_two_real_holes():
    findings = bom_check.run(_pod())
    missing = [f for f in findings if f.code == "BOM_MISSING_PART_ID"]
    assert sorted(r for f in missing for r in f.refs) == ["C16", "C17"]
    # TP/J/U1/R99 are all silent on missing-id; the class summary is INFO
    summary = [f for f in findings if f.code == "BOM_CLASS_SUMMARY"]
    assert len(summary) == 1
    assert "24 no-part" in summary[0].message      # 22 TP + J3 + J4
    assert "1 dnp" in summary[0].message
    assert "1 external" in summary[0].message
    # the DNP'd id carrier gets its confirmation note
    dnp_notes = [f for f in findings if f.code == "BOM_DNP_HAS_ORDER_ID"]
    assert [f.refs for f in dnp_notes] == [["R99"]]
    # nothing above WARNING; and no coverage warning — fitted coverage is
    # 10/12 (83%), above the 50% floor, where the OLD all-parts denominator
    # (10/38 = 26%) would have cried wolf
    assert not [f for f in findings if f.code == "BOM_MPN_COVERAGE"]


def test_dnp_skips_sourcing_checks_but_keeps_structural():
    sch = _sch([
        _comp("R1", value=None, footprint=None, dnp=True),   # dnp: no missing-* noise
        _comp("R2", value=None, footprint=None),             # fitted: flagged
    ])
    codes = [(f.code, f.refs) for f in bom_check.run(sch)]
    assert ("BOM_MISSING_VALUE", ["R2"]) in codes
    assert ("BOM_MISSING_VALUE", ["R1"]) not in codes
    # duplicate designators stay flagged even when one twin is dnp
    sch = _sch([_comp("R1", uid="uid-a"), _comp("R1", dnp=True, uid="uid-b")])
    dup = [f for f in bom_check.run(sch) if f.code == "BOM_DUPLICATE_DESIGNATOR"]
    assert len(dup) == 1


def test_cpl_inconsistency_note():
    sch = _sch([_comp("R5", in_bom=False)])       # real-looking part, not in BOM
    codes = {f.code for f in bom_check.run(sch)}
    assert "BOM_CPL_INCONSISTENT" in codes
    # structural refs and dnp parts are NOT inconsistent
    sch = _sch([_comp("TP1", in_bom=False), _comp("R6", in_bom=False, dnp=True)])
    assert "BOM_CPL_INCONSISTENT" not in {f.code for f in bom_check.run(sch)}


def test_config_floor_and_classes(tmp_path):
    from akcli.config import load_config
    cfg_file = tmp_path / "akcli.toml"
    cfg_file.write_text(
        '[bom]\ncoverage_floor = 0.9\ncoverage_min_parts = 3\n'
        'min_stock = 5\n[bom.classes]\nno_part = ["TP", "PAD"]\n',
        encoding="utf-8", newline="\n")
    cfg = load_config(cfg_file)
    assert cfg.bom["coverage_floor"] == 0.9
    # custom class list: PAD1 is structural now, H1 (default class) is not
    sch = _sch([_comp("PAD1"), _comp("H1"),
                _comp("R1", params={"LCSC": "C25804"}), _comp("R2"), _comp("R3")])
    findings = bom_check.run(sch, cfg)
    missing_refs = sorted(r for f in findings
                          if f.code == "BOM_MISSING_PART_ID" for r in f.refs)
    assert missing_refs == ["H1", "R2", "R3"]
    # 1/4 fitted covered < 0.9 floor with min_parts 3 -> coverage fires
    assert any(f.code == "BOM_MPN_COVERAGE" for f in findings)


# --------------------------------------------------------------------------- #
# jlc bom (P1-2): per-class statuses, no catalog calls for non-fitted
# --------------------------------------------------------------------------- #
def test_jlc_bom_classes_statuses_and_lookup_isolation():
    looked_up: list[str] = []

    def get(c):
        looked_up.append(c)
        return _part(c)

    lines = bom_jlc.check(_pod(), get=get, find=lambda q, **k: [])
    by_class = {}
    for ln in lines:
        by_class.setdefault(ln.bom_class, []).append(ln)
    assert {ln.status for ln in by_class["no-part"]} == {"no-part"}
    assert {ln.status for ln in by_class["dnp"]} == {"dnp"}
    assert {ln.status for ln in by_class["external"]} == {"external"}
    # catalog lookups happen ONLY for fitted lines with ids
    assert sorted(looked_up) == sorted(f"C100{i:02d}" for i in range(1, 11))
    # dnp with an id carries the confirmation note; external names its source
    (dnp_line,) = by_class["dnp"]
    assert "confirm intent" in dnp_line.note and dnp_line.need == 0
    (ext_line,) = by_class["external"]
    assert "vendor official store" in ext_line.note

    agg = bom_jlc.totals(lines)
    assert agg["classes"] == {"fitted": 12, "dnp": 1, "external": 1,
                              "no-part": 24}
    assert agg["fitted_coverage"] == round(10 / 12, 4)


# --------------------------------------------------------------------------- #
# order CSV: every row present, annotated (user decision — no silent filtering)
# --------------------------------------------------------------------------- #
def test_csv_keeps_all_rows_with_notes():
    lines = bom_jlc.check(_pod(), get=lambda c: _part(c),
                          find=lambda q, **k: [])
    rows = list(csv.reader(io.StringIO(bom_jlc.to_jlc_csv(lines))))
    header, body = rows[0], rows[1:]
    assert header == ["No.", "Quantity", "Comment", "Designator", "Footprint",
                      "Value", "Manufacturer Part", "Manufacturer",
                      "Supplier Part", "Supplier", "Note"]
    DES, SUP, NOTE = 3, 8, 10
    by_first_ref = {r[DES].split(",")[0]: r for r in body}
    # dnp: keeps its C-number, annotated
    assert by_first_ref["R99"][SUP] == "C25804"
    assert by_first_ref["R99"][NOTE] == "DNP — do not populate"
    # external: no LCSC id (nothing ordered from JLC), note names the source
    assert by_first_ref["U1"][SUP] == "" and by_first_ref["U1"][9] == ""
    assert "vendor official store" in by_first_ref["U1"][NOTE]
    # structural rows stay visible with their note
    assert by_first_ref["TP1"][NOTE] == "No part expected (structural)"
    assert by_first_ref["J3"][NOTE] == "No part expected (structural)"
    # fitted rows: plain, no note, LCSC as supplier
    assert by_first_ref["R1"][SUP] == "C10001"
    assert by_first_ref["R1"][9] == "LCSC" and by_first_ref["R1"][NOTE] == ""
    # every component of the board is somewhere in the file
    all_refs = {ref for r in body for ref in r[DES].split(",")}
    assert {"R1", "C16", "C17", "U1", "R99", "TP22", "J4"} <= all_refs
