"""BOM P2-2 multi-board merge, P2-4 assembly economics, P2-5 alternates."""

from __future__ import annotations

from akcli import model
from akcli.parts import bom_jlc
from akcli.parts.search import Part


def _comp(ref, value="10k", footprint="R_0402_1005Metric", params=None):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint=footprint, unique_id=ref,
        parameters=params or {})


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


def _part(lcsc, stock=5000, price=0.01, basic=True, package="0402",
          description="10kΩ ±1% 0402 Chip Resistor", tiers=None):
    return Part(lcsc=lcsc, mpn="X" + lcsc, description=description,
                package=package, stock=stock, price=price, basic=basic,
                datasheet=None, category="",
                attributes={"price_tiers": tiers or []})


# --------------------------------------------------------------------------- #
# P2-5 alternates (LCSC_ALT)
# --------------------------------------------------------------------------- #
def test_alternate_takes_over_when_primary_out_of_stock():
    sch = _sch([_comp("R1", params={"LCSC": "C11111",
                                    "LCSC_ALT": "C22222;C33333"})])
    catalog = {"C11111": _part("C11111", stock=0),
               "C22222": _part("C22222", stock=0),        # also dry
               "C33333": _part("C33333", stock=9000)}
    lines = bom_jlc.check(sch, get=lambda c: catalog.get(c),
                          find=lambda q, **k: [])
    (ln,) = lines
    assert ln.lcsc == "C33333" and ln.status == "ok"
    assert "fallback from C11111 (out-of-stock)" in ln.note
    assert ln.verified == "ok"            # the fallback is reverse-verified too


def test_alternate_eol_primary_and_no_healthy_alt():
    sch = _sch([_comp("R1", params={"LCSC": "C11111", "LCSC_ALT": "C22222"})])
    catalog = {"C22222": _part("C22222", stock=0)}
    lines = bom_jlc.check(sch, get=lambda c: catalog.get(c),
                          find=lambda q, **k: [])
    (ln,) = lines
    assert ln.status == "not-found"       # primary gone, alt unhealthy
    assert "none healthy" in ln.note


def test_alternates_flag_suggests_for_risk_lines():
    sch = _sch([_comp("R1", params={"LCSC": "C11111"})])
    cand = _part("C55555", stock=8000)
    lines = bom_jlc.check(sch, get=lambda c: _part(c, stock=0),
                          find=lambda q, **k: [cand])
    # default suggest skips ok/at-risk resolved lines...
    assert bom_jlc.suggest_parts(lines, find=lambda q, **k: [cand]) == 0
    # ...but --alternates opts risk lines in
    n = bom_jlc.suggest_parts(lines, find=lambda q, **k: [cand],
                              include_risk=True)
    assert n == 1 and lines[0].suggestion.lcsc == "C55555"
    assert lines[0].suggestion_confidence == "high"


# --------------------------------------------------------------------------- #
# P2-4 economics
# --------------------------------------------------------------------------- #
def test_totals_economics_and_basic_alternative_note():
    sch = _sch([
        _comp("R1", params={"LCSC": "C10001"}),
        _comp("R2", params={"LCSC": "C20002"}),
    ])
    catalog = {"C10001": _part("C10001", basic=True),
               "C20002": _part("C20002", basic=False)}
    lines = bom_jlc.check(sch, get=lambda c: catalog.get(c),
                          find=lambda q, **k: [])
    agg = bom_jlc.totals(lines, extended_fee=3.0)
    assert agg["basic_lines"] == 1
    assert agg["extended_lines"] == 1
    assert agg["extended_fee_est"] == 3.0

    # the Extended 10k has a Basic twin (same value+package, strict equality)
    basic_twin = _part("C90009", basic=True)
    n = bom_jlc.basic_alternatives(lines, find=lambda q, **k: [basic_twin])
    assert n == 1
    ext_line = next(ln for ln in lines if ln.lcsc == "C20002")
    assert "Basic alternative: C90009" in ext_line.note
    # the Basic line itself is never nagged
    basic_line = next(ln for ln in lines if ln.lcsc == "C10001")
    assert "Basic alternative" not in basic_line.note


def test_basic_alternative_requires_strict_value_match():
    sch = _sch([_comp("R1", value="5.1k", params={"LCSC": "C20002"})])
    lines = bom_jlc.check(sch, get=lambda c: _part(c, basic=False,
                                                   description="5.1kΩ 0402"),
                          find=lambda q, **k: [])
    wrong = _part("C90009", basic=True,
                  description="1kΩ ±1% 0402 Chip Resistor")
    assert bom_jlc.basic_alternatives(lines, find=lambda q, **k: [wrong]) == 0


# --------------------------------------------------------------------------- #
# P2-2 multi-board merge
# --------------------------------------------------------------------------- #
def test_multi_board_merges_shared_cnumbers():
    mainb = _sch([_comp("R1", params={"LCSC": "C11111"}),
                _comp("R2", params={"LCSC": "C11111"}),
                _comp("U1", value="MAIN-ONLY", params={"LCSC": "C77777"})])
    auxb = _sch([_comp("R1", params={"LCSC": "C11111"})])
    # tier pricing: merged need (3) crosses the 3+ tier, single boards don't
    tiers = [{"qFrom": 1, "price": 0.10}, {"qFrom": 3, "price": 0.05}]
    catalog = {"C11111": _part("C11111", tiers=tiers),
               "C77777": _part("C77777")}
    lines = bom_jlc.check_multi([("mainb", mainb), ("auxb", auxb)],
                                get=lambda c: catalog.get(c),
                                find=lambda q, **k: [])
    shared = next(ln for ln in lines if ln.lcsc == "C11111")
    assert sorted(shared.refs) == ["auxb:R1", "mainb:R1", "mainb:R2"]
    assert shared.boards == {"mainb": ["R1", "R2"], "auxb": ["R1"]}
    assert shared.need == 3
    assert shared.unit_price == 0.05      # merged quantity reaches the tier
    only = next(ln for ln in lines if ln.lcsc == "C77777")
    assert only.boards == {"mainb": ["U1"]}
    # merged refs keep the unit-class hint working (mainb:R1 -> R)
    assert shared.verified == "ok"


def test_multi_board_qty_multiplies_merged_need():
    mainb = _sch([_comp("R1", params={"LCSC": "C11111"})])
    auxb = _sch([_comp("R9", params={"LCSC": "C11111"})])
    lines = bom_jlc.check_multi([("mainb", mainb), ("auxb", auxb)], qty=10,
                                get=lambda c: _part(c),
                                find=lambda q, **k: [])
    (ln,) = lines
    assert ln.need == 20                  # (1+1 refs) x 10 boards
