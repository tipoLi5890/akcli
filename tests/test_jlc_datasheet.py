"""Tests for ``akcli jlc datasheet`` (parts/datasheet.py + CLI wiring).

All offline: the EasyEDA lookup and the PDF transport are injected. The
suite pins the three behaviors that make the command trustworthy — the
datasheet link is found on EITHER c_para head (symbol vs footprint), an
HTML challenge page is never saved as a ``.pdf``, and existing files act
as a cache (no re-download without ``--force``).
"""

from __future__ import annotations

import io
import json

import pytest

from akcli import model
from akcli.cli import main
from akcli.errors import EXIT
from akcli.parts import datasheet as ds
from akcli.parts import easyeda

PDF_URL = "https://atta.szlcsc.com/upload/public/pdf/source/x.pdf"
PDF_BODY = b"%PDF-1.4\n%fake body\n%%EOF\n"
HTML_BODY = b"<!DOCTYPE HTML><html>bot check</html>"


# ---------------------------------------------------------- fixtures --------
def _result(link_side="pkg", link=PDF_URL):
    """A minimal EasyEDA ``/components`` ``result`` payload."""
    sym = {"Manufacturer": "VISHAY", "Manufacturer Part": "TCRT5000"}
    pkg = {"package": "DIP-4"}
    if link_side == "sym":
        sym["link"] = link
    elif link_side == "pkg":
        pkg["link"] = link
    return {
        "title": "TCRT5000",
        "dataStr": {"head": {"c_para": sym}},
        "packageDetail": {"dataStr": {"head": {"c_para": pkg}}},
    }


def _info(lcsc="C2984661", url=PDF_URL, mpn="TCRT5000"):
    return easyeda.EasyEdaInfo(lcsc=lcsc, title=mpn, manufacturer="VISHAY",
                               mpn=mpn, datasheet=url, package="DIP-4")


class _Resp:
    def __init__(self, body, status=200):
        self._b = io.BytesIO(body)
        self.status = status

    def read(self, n=-1):
        return self._b.read(n)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, body=PDF_BODY, status=200):
        self.body, self.status, self.calls = body, status, 0

    def open(self, req, timeout=None):
        self.calls += 1
        return _Resp(self.body, self.status)


# ------------------------------------------------- extraction (easyeda) -----
def test_build_info_finds_link_on_either_head():
    for side in ("sym", "pkg"):
        info = easyeda._build_info("C1", _result(link_side=side))
        assert info.datasheet == PDF_URL, side
    assert easyeda._build_info("C1", _result(link_side="none")).datasheet is None


def test_build_info_mpn_falls_back_to_pkg_head():
    r = _result(link_side="pkg")
    r["dataStr"]["head"]["c_para"] = {}          # bare symbol head
    r["packageDetail"]["dataStr"]["head"]["c_para"]["Manufacturer Part"] = "X9"
    assert easyeda._build_info("C1", r).mpn == "X9"


# ----------------------------------------------------------- resolve --------
def test_resolve_happy_path_and_canonical_cnum():
    row = ds.resolve("c2984661", lookup=lambda c: _info(lcsc=c))
    assert row.status == "resolved" and row.url == PDF_URL
    assert row.lcsc == "C2984661"                # canonicalized
    assert row.mpn == "TCRT5000" and row.manufacturer == "VISHAY"


def test_resolve_not_found_and_no_link():
    assert ds.resolve("C1", lookup=lambda c: None).status == "not-found"
    row = ds.resolve("C77", lookup=lambda c: _info(url=None))
    assert row.status == "no-link" and row.url is None
    assert "product-detail/C77" in row.note      # browser fallback hint


def test_resolve_rejects_non_http_link():
    row = ds.resolve("C1", lookup=lambda c: _info(url="ftp://x/y.pdf"))
    assert row.status == "no-link"


def test_resolve_without_injection_is_hermetic():
    # conftest's network guard must bite through the default lookup path
    with pytest.raises(RuntimeError, match="network disabled"):
        ds.resolve("C2984661")


# ---------------------------------------------------------- fetch_pdf -------
def test_fetch_pdf_writes_validates_and_caches(tmp_path):
    dest = tmp_path / "C1_X.pdf"
    op = _Opener()
    path, downloaded = ds.fetch_pdf(PDF_URL, dest, opener=op)
    assert downloaded and path == dest and dest.read_bytes() == PDF_BODY
    # existing file short-circuits: no second network call
    path, downloaded = ds.fetch_pdf(PDF_URL, dest, opener=op)
    assert not downloaded and op.calls == 1
    # --force refetches
    path, downloaded = ds.fetch_pdf(PDF_URL, dest, opener=op, force=True)
    assert downloaded and op.calls == 2


def test_fetch_pdf_rejects_html_and_leaves_nothing(tmp_path):
    dest = tmp_path / "x.pdf"
    with pytest.raises(easyeda.EasyEdaError) as ei:
        ds.fetch_pdf(PDF_URL, dest, opener=_Opener(body=HTML_BODY))
    assert ei.value.kind == "decode" and "browser" in ei.value.message
    assert list(tmp_path.iterdir()) == []        # no dest, no .part leftover


def test_fetch_pdf_http_error_and_size_cap(tmp_path):
    with pytest.raises(easyeda.EasyEdaError) as ei:
        ds.fetch_pdf(PDF_URL, tmp_path / "a.pdf", opener=_Opener(status=503))
    assert ei.value.kind == "http" and ei.value.retryable
    big = b"%PDF" + b"x" * 4096
    with pytest.raises(easyeda.EasyEdaError) as ei:
        ds.fetch_pdf(PDF_URL, tmp_path / "b.pdf",
                     opener=_Opener(body=big), max_bytes=1024)
    assert ei.value.kind == "size"
    assert not (tmp_path / "b.pdf").exists()


# ------------------------------------------------------ BOM row seeding -----
def _comp(ref, params=None, value="10k"):
    return model.Component(designator=ref, library_ref="Device:R",
                           x_mil=0, y_mil=0, value=value,
                           footprint="R_0603", parameters=params or {})


def test_rows_for_schematic_split_by_lcsc_presence():
    sch = model.Schematic(source_path="<t>", source_format="kicad",
                          components=[
                              _comp("R1", {"LCSC": "C11702"}),
                              _comp("R2", {"LCSC": "C11702"}),
                              _comp("U1", {"MPN": "LM339"}),
                              _comp("J1"),
                          ], nets=[])
    rows = ds.rows_for_schematic(sch)
    by = {tuple(r.refs or []): r for r in rows}
    assert by[("R1", "R2")].lcsc == "C11702"     # grouped like the BOM
    assert by[("U1",)].status == "no-lcsc" and by[("U1",)].mpn == "LM339"
    assert by[("J1",)].status == "no-lcsc"


# ------------------------------------------------------------- CLI ----------
def test_cli_single_cnum_json(monkeypatch, capsys):
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, mpn="TCRT5000", url=PDF_URL,
                            status="resolved"))
    assert main(["jlc", "datasheet", "C2984661", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] == 1 and doc["problems"] == 0
    assert doc["rows"][0]["url"] == PDF_URL


def test_cli_problem_exit_and_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, status="no-link", note="n/a"))
    assert main(["jlc", "datasheet", "C11"]) == EXIT["FINDINGS"]
    capsys.readouterr()
    assert main(["jlc", "datasheet", "C11", "--exit-zero"]) == EXIT["OK"]


def test_cli_fetch_writes_into_out_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, mpn="TCRT5000", url=PDF_URL,
                            status="resolved"))
    monkeypatch.setattr(ds, "fetch_pdf",
                        lambda url, dest, **kw: (
                            dest.parent.mkdir(parents=True, exist_ok=True),
                            dest.write_bytes(PDF_BODY), (dest, True))[-1])
    rc = main(["jlc", "datasheet", "C2984661", "--fetch",
               "--out", str(tmp_path / "dsx"), "--json"])
    assert rc == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    row = doc["rows"][0]
    assert row["status"] == "fetched"
    assert row["path"].endswith("C2984661_TCRT5000.pdf")
    assert doc["out_dir"].endswith("dsx")


def test_cli_fetch_failed_is_per_row_not_fatal(monkeypatch, capsys):
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, url=PDF_URL, status="resolved"))

    def _boom(url, dest, **kw):
        raise easyeda.EasyEdaError("not a PDF ...", kind="decode")

    monkeypatch.setattr(ds, "fetch_pdf", _boom)
    assert main(["jlc", "datasheet", "C11", "--fetch"]) == EXIT["FINDINGS"]
    out = capsys.readouterr().out
    assert "fetch-failed" in out


def test_cli_network_error_maps_to_exit_7(monkeypatch, capsys):
    def _net(lcsc, **kw):
        raise easyeda.EasyEdaError("boom", kind="network", retryable=True)

    monkeypatch.setattr(ds, "resolve", _net)
    assert main(["jlc", "datasheet", "C11"]) == EXIT["TOOL_MISSING"]
    assert "NETWORK" in capsys.readouterr().err


def test_cli_mpn_path_uses_catalog_exact_match(monkeypatch, capsys):
    from akcli.parts import search as parts_search

    class _P:
        def __init__(self, lcsc, mpn, stock=10, basic=True):
            self.lcsc, self.mpn, self.stock, self.basic = lcsc, mpn, stock, basic

    monkeypatch.setattr(parts_search, "search",
                        lambda q, limit=10, cache_dir=None: [
                            _P("C90760", "LMC555CMX"), _P("C1", "LMC555CN")])
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, url=PDF_URL, status="resolved"))
    assert main(["jlc", "datasheet", "LMC555CMX", "--json"]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["rows"][0]["lcsc"] == "C90760"
    capsys.readouterr()
    assert main(["jlc", "datasheet", "NOPE-999"]) == EXIT["FINDINGS"]


def test_cli_schematic_batch(monkeypatch, tmp_path, capsys):
    import shutil
    from pathlib import Path as _P
    v8 = _P(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    tgt = tmp_path / "board.kicad_sch"
    shutil.copy(v8, tgt)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 1, "target_format": "kicad",
        "ops": [{"op": "set_component_parameters", "designator": "R1",
                 "parameters": {"LCSC": "C11702"}}]}))
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OK"]
    capsys.readouterr()
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, url=PDF_URL, status="resolved"))
    rc = main(["jlc", "datasheet", str(tgt), "--json"])
    doc = json.loads(capsys.readouterr().out)
    resolved = [r for r in doc["rows"] if r["status"] == "resolved"]
    assert len(resolved) == 1 and resolved[0]["lcsc"] == "C11702"
    assert resolved[0]["refs"] == ["R1"]
    assert doc["no_lcsc"] >= 1                   # the other parts lack ids
    assert rc == EXIT["OK"] if doc["problems"] == 0 else EXIT["FINDINGS"]


def test_resolve_mpn_only_rows_hit_miss_and_dedupe(monkeypatch):
    """--resolve-mpn helper: one search per distinct MPN, hit + miss rewrite."""
    from akcli.commands.jlc import _resolve_mpn_only_rows
    from akcli.parts import search as parts_search

    class _P:
        def __init__(self, lcsc, mpn, stock=10, basic=True):
            self.lcsc, self.mpn, self.stock, self.basic = lcsc, mpn, stock, basic

    calls = []

    def _search(q, limit=10, cache_dir=None):
        calls.append(q)
        if q.casefold() == "lm339":
            return [_P("C1", "LM339"), _P("C2", "LM339N")]
        return [_P("C9", "NEAR-999")]

    monkeypatch.setattr(parts_search, "search", _search)

    rows = [
        ds.DatasheetRow(refs=["U1"], mpn="LM339", status="no-lcsc"),
        ds.DatasheetRow(refs=["U2"], mpn="LM339", status="no-lcsc"),  # dedupe
        ds.DatasheetRow(refs=["U3"], mpn="NOPE-999", status="no-lcsc"),  # miss
        ds.DatasheetRow(refs=["J1"], status="no-lcsc"),               # no mpn
        ds.DatasheetRow(refs=["R1"], lcsc="C11702"),                  # untouched
    ]
    _resolve_mpn_only_rows(rows, cache=None)

    assert calls.count("LM339") == 1                 # deduped across U1/U2
    assert "NOPE-999" in calls
    by_refs = {tuple(r.refs or []): r for r in rows}
    assert by_refs[("U1",)].lcsc == "C1" and by_refs[("U1",)].status == ""
    assert by_refs[("U2",)].lcsc == "C1" and by_refs[("U2",)].status == ""
    assert by_refs[("U3",)].status == "not-found"
    assert "nearest" in by_refs[("U3",)].note
    assert by_refs[("J1",)].status == "no-lcsc"       # no mpn: left alone
    assert by_refs[("R1",)].lcsc == "C11702"          # already had lcsc


def test_resolve_mpn_only_rows_finds_exact_beyond_first_ten(monkeypatch):
    """A short generic MPN whose exact row ranks 11th must still be found:
    the exact-resolution search widens past the old limit=10 cap."""
    from akcli.commands.jlc import _resolve_mpn_only_rows
    from akcli.parts import search as parts_search

    class _P:
        def __init__(self, lcsc, mpn, stock=10, basic=True):
            self.lcsc, self.mpn, self.stock, self.basic = lcsc, mpn, stock, basic

    # Ten longer/marketing variants rank ahead of the exact casefold match.
    pool = [_P(f"C{i}", f"BSS138LT{i}G") for i in range(10)]
    pool.append(_P("C999", "BSS138"))          # the exact match, 11th row

    seen: dict = {}

    def _search(q, limit=10, cache_dir=None):
        seen["limit"] = limit
        return pool[:limit]

    monkeypatch.setattr(parts_search, "search", _search)
    rows = [ds.DatasheetRow(refs=["Q1"], mpn="BSS138", status="no-lcsc")]
    _resolve_mpn_only_rows(rows, cache=None)

    assert seen["limit"] > 10                   # searches wider than the old cap
    assert rows[0].lcsc == "C999"               # exact match found …
    assert rows[0].status == ""                 # … not flipped to not-found


def test_cli_schematic_resolve_mpn_hit_and_miss(monkeypatch, tmp_path, capsys):
    from akcli.parts import search as parts_search

    class _P:
        def __init__(self, lcsc, mpn, stock=10, basic=True):
            self.lcsc, self.mpn, self.stock, self.basic = lcsc, mpn, stock, basic

    sch = model.Schematic(
        source_path="<t>", source_format="kicad",
        components=[
            _comp("U1", {"MPN": "LM339"}),      # hit
            _comp("U3", {"MPN": "NOPE-999"}),   # miss
            _comp("J1"),                        # no MPN at all: stays no-lcsc
        ], nets=[])
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text("(kicad_sch)")
    monkeypatch.setattr("akcli.commands.jlc._load_schematic",
                        lambda p: sch)

    def _search(q, limit=10, cache_dir=None):
        if q.casefold() == "lm339":
            return [_P("C1", "LM339"), _P("C2", "LM339N")]
        return [_P("C9", "NEAR-999")]

    monkeypatch.setattr(parts_search, "search", _search)
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, mpn="LM339", url=PDF_URL,
                            status="resolved"))

    rc = main(["jlc", "datasheet", str(tgt), "--resolve-mpn", "--json"])
    doc = json.loads(capsys.readouterr().out)
    rows = doc["rows"]
    by_refs = {tuple(r["refs"] or []): r for r in rows}
    assert by_refs[("U1",)]["status"] == "resolved"
    assert by_refs[("U3",)]["status"] == "not-found"
    assert "nearest" in by_refs[("U3",)]["note"]
    assert by_refs[("J1",)]["status"] == "no-lcsc"
    assert rc == EXIT["FINDINGS"]                     # U3 not-found is a problem


def test_cli_schematic_resolve_mpn_flag_off_keeps_old_behavior(
        monkeypatch, tmp_path, capsys):
    from akcli.parts import search as parts_search

    sch = model.Schematic(
        source_path="<t>", source_format="kicad",
        components=[_comp("U1", {"MPN": "LM339"})], nets=[])
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text("(kicad_sch)")
    monkeypatch.setattr("akcli.commands.jlc._load_schematic",
                        lambda p: sch)

    def _boom(*a, **kw):
        raise AssertionError("catalog must not be searched without --resolve-mpn")

    monkeypatch.setattr(parts_search, "search", _boom)

    rc = main(["jlc", "datasheet", str(tgt), "--json"])
    doc = json.loads(capsys.readouterr().out)
    row = doc["rows"][0]
    assert row["status"] == "no-lcsc"
    assert "--resolve-mpn" in row["note"]
    assert rc == EXIT["OK"]                   # no-lcsc alone isn't a "problem"


def test_resolve_mpn_only_rows_network_error_maps_to_exit_7(
        monkeypatch, tmp_path, capsys):
    from akcli.parts import search as parts_search

    sch = model.Schematic(
        source_path="<t>", source_format="kicad",
        components=[_comp("U1", {"MPN": "LM339"})], nets=[])
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text("(kicad_sch)")
    monkeypatch.setattr("akcli.commands.jlc._load_schematic",
                        lambda p: sch)

    def _net(q, limit=10, cache_dir=None):
        raise parts_search.JlcNetworkError("boom")

    monkeypatch.setattr(parts_search, "search", _net)
    rc = main(["jlc", "datasheet", str(tgt), "--resolve-mpn"])
    assert rc == EXIT["TOOL_MISSING"]
    assert "NETWORK" in capsys.readouterr().err


def test_cli_bare_target_is_usage_error(capsys):
    assert main(["jlc", "datasheet"]) == EXIT["USAGE"]


# ------------------------------------------------------------ misc ----------
def test_pdf_filename_sanitizes():
    assert ds.pdf_filename("C1", "TCRT5000") == "C1_TCRT5000.pdf"
    assert ds.pdf_filename("C1", "a b/c:d") == "C1_a-b-c-d.pdf"
    assert ds.pdf_filename(None, None) == "datasheet.pdf"


def test_default_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AKCLI_DATASHEET_DIR", str(tmp_path / "z"))
    assert ds.default_dir() == tmp_path / "z"
    monkeypatch.delenv("AKCLI_DATASHEET_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ds.default_dir() == tmp_path / "xdg" / "akcli" / "datasheets"


def test_resolve_classifies_page_and_search_links():
    # product/viewer page: URL is surfaced for a browser-grade fetcher
    row = ds.resolve("C7948",
                     lookup=lambda c: _info(url="https://item.szlcsc.com/7253.html"))
    assert row.status == "page-link"
    assert row.url == "https://item.szlcsc.com/7253.html"
    assert "browser" in row.note
    # search-engine junk (real-world EasyEDA data): worthless, hint instead
    for junk in ("https://so.szlcsc.com/global.html?c=&k=C9843",
                 "https://cn.bing.com/search?q=datasheetAD8067ARTZ-REEL7"):
        row = ds.resolve("C90760", lookup=lambda c: _info(url=junk))
        assert row.status == "no-link" and row.url is None
        assert "product-detail/C90760" in row.note
    # query strings after .pdf still count as direct documents
    row = ds.resolve("C11", lookup=lambda c: _info(url=PDF_URL + "?v=2"))
    assert row.status == "resolved"


def test_cli_page_link_counts_and_exit(monkeypatch, capsys):
    monkeypatch.setattr(ds, "resolve",
                        lambda lcsc, **kw: ds.DatasheetRow(
                            lcsc=lcsc, url="https://item.szlcsc.com/1.html",
                            status="page-link", note="not a direct PDF"))
    assert main(["jlc", "datasheet", "C11", "--json"]) == EXIT["FINDINGS"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["page_link"] == 1 and doc["problems"] == 0
    capsys.readouterr()
    assert main(["jlc", "datasheet", "C11", "--exit-zero"]) == EXIT["OK"]
