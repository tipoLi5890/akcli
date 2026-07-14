"""`bom_jlc.to_jlc_csv` — JLCPCB "Upload BOM" CSV export (fully offline).

The upload format is four columns — ``Comment,Designator,Footprint,LCSC Part #``
— one row per BOM line, every ref of a line comma-joined inside a single
(quoted) Designator cell, and the footprint reduced to its bare name. A line
that did not resolve to a live catalog part must leave ``LCSC Part #`` blank:
a known-dead C-number in an order file would be worse than none.
"""

from __future__ import annotations

import csv
import io

from akcli import model
from akcli.parts import bom_jlc
from akcli.parts.search import Part


def _line(**kw) -> bom_jlc.BomLine:
    base = dict(refs=["R1"], value="10k", footprint=None)
    base.update(kw)
    return bom_jlc.BomLine(**base)


def _part(lcsc="C1", stock=1000):
    return Part(lcsc=lcsc, mpn="X", description="", package="0402",
                stock=stock, price=0.01, basic=True, datasheet=None,
                category="R", attributes={})


def _rows(out: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(out)))


def test_header_is_exact():
    out = bom_jlc.to_jlc_csv([])
    assert out.splitlines() == ["Comment,Designator,Footprint,LCSC Part #"]


def test_row_shape_multi_ref_quoting_and_short_footprint():
    lines = [
        _line(refs=["C1", "C2", "C3"], value="100n",
              footprint="Capacitor_SMD:C_0402_1005Metric",
              lcsc="C1525", status="ok", part=_part("C1525")),
        _line(refs=["R1"], value="10k",
              footprint="Resistor_SMD:R_0402_1005Metric",
              lcsc="C25744", status="ok", part=_part("C25744")),
    ]
    out = bom_jlc.to_jlc_csv(lines)
    rows = _rows(out)
    assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]
    assert rows[1] == ["100n", "C1,C2,C3", "C_0402_1005Metric", "C1525"]
    assert rows[2] == ["10k", "R1", "R_0402_1005Metric", "C25744"]
    # the multi-ref designators are ONE quoted cell on the raw CSV line
    assert '"C1,C2,C3"' in out.splitlines()[1]


def test_rows_sorted_by_first_designator():
    lines = [_line(refs=["R9"], lcsc="C2", status="ok"),
             _line(refs=["C1"], lcsc="C3", status="ok"),
             _line(refs=["D4"], lcsc="C4", status="ok")]
    rows = _rows(bom_jlc.to_jlc_csv(lines))
    assert [r[1] for r in rows[1:]] == ["C1", "D4", "R9"]


def test_unresolved_lines_leave_lcsc_blank():
    lines = [
        _line(refs=["R9"], value="1k", footprint="R_0603",   # no colon: kept as-is
              status="no-part-id"),
        _line(refs=["C9"], value="1u", footprint="X:C_0603",
              lcsc="C42", status="not-found"),               # dead id stays out
    ]
    rows = _rows(bom_jlc.to_jlc_csv(lines))
    assert rows[1] == ["1u", "C9", "C_0603", ""]
    assert rows[2] == ["1k", "R9", "R_0603", ""]


def test_values_with_commas_and_quotes_are_csv_escaped():
    lines = [_line(refs=["U1"], value='1,5" special', footprint=None,
                   lcsc="C7", status="ok")]
    rows = _rows(bom_jlc.to_jlc_csv(lines))
    assert rows[1] == ['1,5" special', "U1", "", "C7"]


def test_missing_value_and_footprint_render_empty():
    rows = _rows(bom_jlc.to_jlc_csv([_line(value=None, footprint=None)]))
    assert rows[1] == ["", "R1", "", ""]


# ------------------------------------------------- end-to-end via check() ----

def _comp(ref, params=None, value="10k"):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint="Resistor_SMD:R_0402_1005Metric",
        parameters=params or {})


def test_csv_from_checked_schematic():
    sch = model.Schematic(source_path="<t>", source_format="kicad", nets=[],
                          components=[_comp("R1", {"LCSC": "C25744"}),
                                      _comp("R2", {"LCSC": "C25744"}),
                                      _comp("R3")])
    lines = bom_jlc.check(sch, get=lambda lcsc: _part(lcsc),
                          find=lambda *a, **k: [])
    rows = _rows(bom_jlc.to_jlc_csv(lines))
    assert rows[1] == ["10k", "R1,R2", "R_0402_1005Metric", "C25744"]
    assert rows[2] == ["10k", "R3", "R_0402_1005Metric", ""]   # no-part-id


def test_collect_lines_public_with_underscore_alias():
    assert bom_jlc._collect_lines is bom_jlc.collect_lines
    sch = model.Schematic(source_path="<t>", source_format="kicad", nets=[],
                          components=[_comp("R1", {"LCSC": "C5"}),
                                      _comp("R2", {"LCSC": "C5"})])
    lines = bom_jlc.collect_lines(sch)
    assert len(lines) == 1 and lines[0].refs == ["R1", "R2"]
