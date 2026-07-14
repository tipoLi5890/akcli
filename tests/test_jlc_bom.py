"""`akcli jlc bom` — BOM → JLCPCB purchasability bridge (offline, injected).

Network is faked through the ``get``/``find`` injection points of
:func:`parts.bom_jlc.check`; the CLI-level tests monkeypatch the same
functions on the search module.
"""

from __future__ import annotations

import json

import pytest

from akcli import cli, model
from akcli.parts import bom_jlc
from akcli.parts.search import JlcNetworkError, Part


def _part(lcsc="C1", mpn="X", stock=1000, basic=False, preferred=False, price=0.01):
    return Part(lcsc=lcsc, mpn=mpn, description="", package="0603",
                stock=stock, price=price, basic=basic, datasheet=None,
                category="R", attributes={"is_preferred": preferred})


def _comp(ref, params=None, value="10k", uid=None):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint="R_0603", unique_id=uid,
        parameters=params or {})


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


# ------------------------------------------------------------ resolution ----

def test_lcsc_param_direct_lookup_and_grouping():
    calls = []
    def get(lcsc):
        calls.append(lcsc)
        return _part(lcsc=lcsc, stock=500)
    sch = _sch([
        _comp("C1", {"LCSC Part": "C25804"}),
        _comp("C2", {"LCSC": "25804"}),          # bare digits normalize
        _comp("C3", {"JLCPCB#": "C25804"}),
    ])
    lines = bom_jlc.check(sch, get=get, find=lambda *a, **k: [])
    assert len(lines) == 1 and calls == ["C25804"]   # one line, ONE lookup
    ln = lines[0]
    assert ln.refs == ["C1", "C2", "C3"] and ln.qty == 3
    assert ln.status == "ok" and ln.part.stock == 500


def test_mpn_search_prefers_basic_and_stock():
    def find(q, limit=10):
        return [_part("C10", "NE555DR", stock=0),
                _part("C11", "NE555DR", stock=900, basic=True),
                _part("C12", "ne555dr", stock=50_000),          # case-insensitive
                _part("C13", "NE555DR-OTHER", stock=9)]         # not exact
    lines = bom_jlc.check(_sch([_comp("U1", {"MPN": "NE555DR"})]),
                          get=lambda *a: None, find=find)
    ln = lines[0]
    assert ln.status == "ok"
    assert ln.lcsc == "C11"                    # in-stock Basic beats deeper stock
    assert "candidates" in ln.note


def test_mpn_no_exact_match_reports_nearest():
    lines = bom_jlc.check(
        _sch([_comp("U1", {"Manufacturer Part": "XYZ999"})]),
        get=lambda *a: None,
        find=lambda q, limit=10: [_part("C9", "XYZ998")])
    assert lines[0].status == "not-found"
    assert "XYZ998" in lines[0].note


def test_missing_ids_and_bogus_lcsc():
    lines = bom_jlc.check(
        _sch([_comp("R1"), _comp("R2", {"LCSC": "C42"})]),
        get=lambda lcsc: None, find=lambda *a, **k: [])
    by_ref = {ln.refs[0]: ln for ln in lines}
    assert by_ref["R1"].status == "no-part-id"
    assert by_ref["R2"].status == "not-found"


def test_stock_thresholds():
    def get(lcsc):
        return _part(lcsc=lcsc, stock={"C10": 0, "C22": 5, "C33": 50}[lcsc])
    sch = _sch([_comp("R1", {"LCSC": "C10"}), _comp("R2", {"LCSC": "C22"}),
                _comp("R3", {"LCSC": "C33"})])
    st = {ln.lcsc: ln.status
          for ln in bom_jlc.check(sch, min_stock=10, get=get,
                                  find=lambda *a, **k: [])}
    assert st == {"C10": "out-of-stock", "C22": "low-stock", "C33": "ok"}


def test_virtual_parts_excluded():
    lines = bom_jlc.check(
        _sch([_comp("#PWR01", value="+3V3"), _comp("R1", {"LCSC": "C5"})]),
        get=lambda lcsc: _part(lcsc), find=lambda *a, **k: [])
    assert [ln.refs for ln in lines] == [["R1"]]


def test_multi_unit_counts_once():
    sch = _sch([_comp("U1", {"LCSC": "C5"}, uid="u-1"),
                _comp("U1", {"LCSC": "C5"}, uid="u-1")])   # two units, one part
    lines = bom_jlc.check(sch, get=lambda lcsc: _part(lcsc),
                          find=lambda *a, **k: [])
    assert lines[0].qty == 1


# ------------------------------------------------------------------ CLI ----

def _write_sch(tmp_path, params_by_ref):
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    work = tmp_path / "t.kicad_sch"
    shutil.copy(fixture, work)
    ops = [{"op": "set_component_parameters", "designator": ref,
            "parameters": p} for ref, p in params_by_ref.items()]
    opsfile = tmp_path / "ops.json"
    opsfile.write_text(json.dumps({"protocol_version": 1,
                                   "target_format": "kicad",
                                   "target_file": "x", "ops": ops}))
    assert cli.main(["draw", str(work), "--ops", str(opsfile), "--apply"]) == 0
    return work


def test_cli_exit_semantics(tmp_path, capsys, monkeypatch):
    from akcli.parts import search as parts_search
    monkeypatch.setattr(parts_search, "get",
                        lambda lcsc, **k: _part(lcsc, stock=777))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    work = _write_sch(tmp_path, {"R1": {"LCSC Part": "C25804"}})
    capsys.readouterr()
    # R1 resolves, R2/C1 are advisory no-part-id -> exit 0
    assert cli.main(["jlc", "bom", str(work)]) == 0
    out = capsys.readouterr().out
    assert "C25804" in out and "no-part-id" in out

    # a dead C-number -> problem -> exit 1; --exit-zero forces 0
    monkeypatch.setattr(parts_search, "get", lambda lcsc, **k: None)
    assert cli.main(["jlc", "bom", str(work)]) == 1
    capsys.readouterr()
    assert cli.main(["jlc", "bom", str(work), "--exit-zero"]) == 0
    capsys.readouterr()


def test_cli_json_shape(tmp_path, capsys, monkeypatch):
    from akcli.parts import search as parts_search
    monkeypatch.setattr(parts_search, "get",
                        lambda lcsc, **k: _part(lcsc, stock=42))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    work = _write_sch(tmp_path, {"R1": {"LCSC": "C7593"}})
    capsys.readouterr()
    assert cli.main(["jlc", "bom", str(work), "--json", "--exit-zero",
                     "--qty", "10"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["qty"] == 10 and doc["totals"]["lines"] >= 1
    row = next(ln for ln in doc["lines"] if ln["refs"] == ["R1"])
    assert row["status"] == "ok" and row["part"]["stock"] == 42
    assert row["qty"] == 1 and row["need"] == 10 and row["lcsc"] == "C7593"
    assert row["unit_price"] == 0.01 and row["ext_price"] == 0.1


def test_cli_network_error_exits_7(tmp_path, capsys, monkeypatch):
    from akcli.parts import search as parts_search
    def boom(*a, **k):
        raise JlcNetworkError("could not reach jlcsearch: refused")
    monkeypatch.setattr(parts_search, "get", boom)
    monkeypatch.setattr(parts_search, "search", boom)
    work = _write_sch(tmp_path, {"R1": {"LCSC": "C7593"}})
    capsys.readouterr()
    assert cli.main(["jlc", "bom", str(work)]) == 7
    assert "NETWORK" in capsys.readouterr().err


def test_qty_drives_need_stock_and_tier_pricing():
    tiers = [{"qFrom": 20, "qTo": 199, "price": 0.10},
             {"qFrom": 200, "qTo": None, "price": 0.05}]
    def get(lcsc):
        return Part(lcsc=lcsc, mpn="X", description="", package="0603",
                    stock=150, price=0.10, basic=True, datasheet=None,
                    category="R", attributes={"price_tiers": tiers})
    sch = _sch([_comp("R1", {"LCSC": "C77"}), _comp("R2", {"LCSC": "C77"})])

    # 10 boards x 2 refs = 20 pieces -> first tier, stock ok
    ln = bom_jlc.check(sch, qty=10, get=get, find=lambda *a, **k: [])[0]
    assert (ln.need, ln.unit_price, ln.ext_price) == (20, 0.10, 2.0)
    assert ln.status == "ok"

    # 150 boards -> 300 pieces: second tier price, stock 150 insufficient
    ln = bom_jlc.check(sch, qty=150, get=get, find=lambda *a, **k: [])[0]
    assert (ln.need, ln.unit_price, ln.ext_price) == (300, 0.05, 15.0)
    assert ln.status == "low-stock" and "required 300" in ln.note

    # below the minimum tier quantity, the lowest tier still prices it
    ln = bom_jlc.check(sch, qty=1, get=get, find=lambda *a, **k: [])[0]
    assert (ln.need, ln.unit_price) == (2, 0.10)


def test_totals_aggregation():
    def get(lcsc):
        return _part(lcsc, stock=10, price=0.5) if lcsc == "C10" else None
    sch = _sch([_comp("R1", {"LCSC": "C10"}), _comp("R2", {"LCSC": "C99"}),
                _comp("R3")])
    agg = bom_jlc.totals(bom_jlc.check(sch, get=get, find=lambda *a, **k: []))
    assert agg["lines"] == 3 and agg["ok"] == 1
    assert agg["problems"] == 1 and agg["no_part_id"] == 1
    assert agg["priced_lines"] == 1 and agg["est_cost"] == 0.5


def test_default_cache_dir_env(monkeypatch, tmp_path):
    from akcli.parts.search import default_cache_dir
    monkeypatch.setenv("AKCLI_JLC_CACHE", str(tmp_path / "jc"))
    assert default_cache_dir() == tmp_path / "jc"
    monkeypatch.setenv("AKCLI_JLC_CACHE", "off")
    assert default_cache_dir() is None
    monkeypatch.delenv("AKCLI_JLC_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x"))
    assert default_cache_dir() == tmp_path / "x" / "akcli" / "jlc"


# ------------------------------------------------------- suggest / fix ----

def test_suggest_matches_value_and_package():
    calls = []
    def find(q, limit=20):
        calls.append(q)
        return [_part("C9999", "WRONGPKG", stock=99),           # 0603 default
                Part(lcsc="C1525", mpn="CL05B104KO5NNNC", description="",
                     package="0402", stock=50_000_000, price=0.001,
                     basic=True, datasheet=None, category="C", attributes={})]
    line = bom_jlc.BomLine(refs=["C1"], value="100n",
                           footprint="Capacitor_SMD:C_0402_1005Metric",
                           lcsc="C42", lcsc_key="LCSC", status="not-found")
    assert bom_jlc.suggest_parts([line], find=find) == 1
    assert line.suggestion.lcsc == "C1525"
    assert calls[0] == "100nF 0402"            # cap value normalized + package


def test_suggest_skips_resolved_lines_and_respects_package():
    def find(q, limit=20):
        return [_part("C1", "X", stock=10)]    # package 0603
    ok = bom_jlc.BomLine(refs=["R1"], value="10k", footprint="R_0402_X",
                         status="ok")
    nopkg = bom_jlc.BomLine(refs=["R2"], value="10k", footprint="R_0402_X",
                            status="no-part-id")
    assert bom_jlc.suggest_parts([ok, nopkg], find=find) == 0
    assert ok.suggestion is None and nopkg.suggestion is None   # 0603 != 0402


def test_fix_ops_write_back_to_original_key():
    line = bom_jlc.BomLine(refs=["C1", "C2"], value="100n", footprint=None,
                           lcsc="C42", lcsc_key="LCSC Part",
                           status="not-found",
                           suggestion=_part("C1525", "CL05B"),
                           suggestion_confidence="high")
    ops = bom_jlc.fix_ops([line])
    assert ops == [
        {"op": "set_component_parameters", "designator": "C1",
         "parameters": {"LCSC Part": "C1525"}},
        {"op": "set_component_parameters", "designator": "C2",
         "parameters": {"LCSC Part": "C1525"}},
    ]


def test_fix_ops_confidence_gate():
    low = bom_jlc.BomLine(refs=["C1"], value="100n", footprint=None,
                          status="not-found", suggestion=_part("C9", "X"),
                          suggestion_confidence="low")
    ungraded = bom_jlc.BomLine(refs=["C2"], value="100n", footprint=None,
                               status="not-found", suggestion=_part("C8", "Y"))
    # default gate ("high") skips low and ungraded suggestions
    assert bom_jlc.fix_ops([low, ungraded]) == []
    # min_confidence="low" writes everything (the CLI's --fix-all)
    ops = bom_jlc.fix_ops([low, ungraded], min_confidence="low")
    assert [op["designator"] for op in ops] == ["C1", "C2"]


def test_suggest_grades_confidence():
    high = Part(lcsc="C1525", mpn="CL05B104KO5NNNC",
                description="50V 100nF X7R 0402 MLCC", package="0402",
                stock=1000, price=0.001, basic=True, datasheet=None,
                category="C", attributes={})
    line = bom_jlc.BomLine(refs=["C1"], value="100n",
                           footprint="Capacitor_SMD:C_0402_1005Metric",
                           status="no-part-id")
    assert bom_jlc.suggest_parts([line], find=lambda q, limit=20: [high]) == 1
    assert line.suggestion_confidence == "high"
    assert line.to_dict()["suggestion_confidence"] == "high"

    # same package but the value is nowhere in the description/MPN -> low
    vague = Part(lcsc="C7", mpn="ZZZ", description="capacitor",
                 package="0402", stock=10, price=0.01, basic=False,
                 datasheet=None, category="C", attributes={})
    line2 = bom_jlc.BomLine(refs=["C2"], value="100n",
                            footprint="Capacitor_SMD:C_0402_1005Metric",
                            status="no-part-id")
    bom_jlc.suggest_parts([line2], find=lambda q, limit=20: [vague])
    assert line2.suggestion_confidence == "low"


def test_cli_fix_end_to_end(tmp_path, capsys, monkeypatch):
    from akcli.parts import search as parts_search
    # value "100n" appears in the description and the package matches the
    # footprint -> a HIGH-confidence suggestion, which the default --fix writes
    good = Part(lcsc="C1525", mpn="CL05B104KO5NNNC",
                description="50V 100nF X7R 0402 MLCC",
                package="0402", stock=999, price=0.001, basic=True,
                datasheet=None, category="C", attributes={})
    monkeypatch.setattr(parts_search, "get",
                        lambda lcsc, **k: good if lcsc == "C1525" else None)
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [good])
    work = _write_sch(tmp_path, {"C1": {"LCSC": "C42"}})   # bogus id
    capsys.readouterr()
    assert cli.main(["jlc", "bom", str(work), "--fix", "--exit-zero"]) == 0
    out = capsys.readouterr()
    assert "fixed C1" in out.err and "C1525" in out.out
    # written back into the SAME parameter key
    text = work.read_text(encoding="utf-8")
    assert '(property "LCSC" "C1525"' in text
    # the fix went through the draw pipeline -> undo can revert it
    assert (work.parent / (work.name + ".bak")).exists()
