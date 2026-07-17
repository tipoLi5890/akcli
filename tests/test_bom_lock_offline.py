"""BOM P2-1 lockfile + P2-6 offline mode."""

from __future__ import annotations

import json

from akcli import cli, model
from akcli.parts import bom_jlc
from akcli.parts.search import JlcOfflineMiss, Part


def _comp(ref, value="10k", params=None):
    return model.Component(
        designator=ref, library_ref="Device:R", x_mil=0, y_mil=0,
        value=value, footprint="R_0402_1005Metric", unique_id=ref,
        parameters=params or {})


def _sch(comps):
    return model.Schematic(source_path="<t>", source_format="kicad",
                           components=comps, nets=[])


def _part(lcsc, stock=5000, price=0.01, basic=True):
    return Part(lcsc=lcsc, mpn="X" + lcsc,
                description="10kΩ ±1% 0402 Chip Resistor", package="0402",
                stock=stock, price=price, basic=basic, datasheet=None,
                category="", attributes={})


def _board():
    return _sch([
        _comp("R1", params={"LCSC": "C11111"}),
        _comp("R2", params={"LCSC": "C22222"}),
    ])


# --------------------------------------------------------------------------- #
# lockfile
# --------------------------------------------------------------------------- #
def test_lock_roundtrip_no_drift():
    lines = bom_jlc.check(_board(), get=lambda c: _part(c),
                          find=lambda q, **k: [])
    lock = bom_jlc.make_lock(lines, qty=1)
    assert lock["schema_version"] == "1"
    assert lock["generated_at"]
    assert len(lock["lines"]) == 2
    # identical re-check drifts nothing
    fresh = bom_jlc.check(_board(), get=lambda c: _part(c),
                          find=lambda q, **k: [])
    assert bom_jlc.diff_lock(fresh, lock) == []


def test_lock_detects_every_drift_kind():
    lines = bom_jlc.check(_board(), get=lambda c: _part(c),
                          find=lambda q, **k: [])
    lock = bom_jlc.make_lock(lines, qty=1)

    # price change + part gone
    def drifted_get(c):
        if c == "C11111":
            return _part(c, price=0.05)          # price up
        return None                              # C22222 vanished (EOL)

    fresh = bom_jlc.check(_board(), get=drifted_get, find=lambda q, **k: [])
    kinds = {d["kind"] for d in bom_jlc.diff_lock(fresh, lock)}
    assert "price_changed" in kinds
    assert "gone" in kinds

    # id edit in the schematic -> id_changed
    edited = _sch([
        _comp("R1", params={"LCSC": "C99999"}),  # someone re-pointed R1
        _comp("R2", params={"LCSC": "C22222"}),
    ])
    fresh = bom_jlc.check(edited, get=lambda c: _part(c),
                          find=lambda q, **k: [])
    kinds = {d["kind"] for d in bom_jlc.diff_lock(fresh, lock)}
    assert "id_changed" in kinds

    # stock collapse below need
    fresh = bom_jlc.check(_board(), qty=100,
                          get=lambda c: _part(c, stock=5),
                          find=lambda q, **k: [])
    kinds = {d["kind"] for d in bom_jlc.diff_lock(fresh, lock)}
    assert "stock_below_need" in kinds

    # board edits -> added/removed lines
    grown = _sch([
        _comp("R1", params={"LCSC": "C11111"}),
        _comp("R2", params={"LCSC": "C22222"}),
        _comp("R3", params={"LCSC": "C33333"}),
    ])
    fresh = bom_jlc.check(grown, get=lambda c: _part(c), find=lambda q, **k: [])
    kinds = {d["kind"] for d in bom_jlc.diff_lock(fresh, lock)}
    assert "line_added" in kinds
    shrunk = _sch([_comp("R1", params={"LCSC": "C11111"})])
    fresh = bom_jlc.check(shrunk, get=lambda c: _part(c), find=lambda q, **k: [])
    kinds = {d["kind"] for d in bom_jlc.diff_lock(fresh, lock)}
    assert "line_removed" in kinds


def test_cli_lock_write_and_against(tmp_path, capsys, monkeypatch):
    from akcli.parts import search as parts_search
    monkeypatch.setattr(parts_search, "get",
                        lambda lcsc, **k: _part(lcsc, stock=777, price=0.01))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    # a real .kicad_sch with one id-carrying resistor
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    work = tmp_path / "t.kicad_sch"
    shutil.copy(fixture, work)
    ops = [{"op": "set_component_parameters", "designator": "R1",
            "parameters": {"LCSC Part": "C25804"}}]
    opsfile = tmp_path / "ops.json"
    opsfile.write_text(json.dumps({"protocol_version": 1,
                                   "target_format": "kicad",
                                   "target_file": "x", "ops": ops}),
                       encoding="utf-8", newline="\n")
    assert cli.main(["draw", str(work), "--ops", str(opsfile), "--apply"]) == 0
    capsys.readouterr()

    lock = tmp_path / "bom.lock.json"
    assert cli.main(["jlc", "bom", str(work), "--lock", str(lock),
                     "--exit-zero"]) == 0
    assert json.loads(lock.read_text(encoding="utf-8"))["lines"]
    capsys.readouterr()

    # no drift -> exit 0 even without --exit-zero (only R1 resolves; the
    # no-part-id advisory lines are not problems)
    assert cli.main(["jlc", "bom", str(work), "--against-lock", str(lock)]) == 0
    assert "lock drift: none" in capsys.readouterr().out

    # price drift -> reported + exit 1
    monkeypatch.setattr(parts_search, "get",
                        lambda lcsc, **k: _part(lcsc, stock=777, price=0.02))
    assert cli.main(["jlc", "bom", str(work), "--against-lock", str(lock)]) == 1
    out = capsys.readouterr().out
    assert "DRIFT price_changed" in out and "$0.0100 -> $0.0200" in out


# --------------------------------------------------------------------------- #
# offline mode
# --------------------------------------------------------------------------- #
def test_offline_miss_degrades_to_unverified():
    def get(c):
        if c == "C11111":
            return _part(c)                      # warm cache
        raise JlcOfflineMiss("offline: not in cache")

    lines = bom_jlc.check(_board(), get=get, find=lambda q, **k: [],
                          offline=True)
    by_ref = {ln.refs[0]: ln for ln in lines}
    assert by_ref["R1"].status == "ok"           # cache hit still verifies
    assert by_ref["R2"].status == "unverified"
    assert "offline" in by_ref["R2"].note
    agg = bom_jlc.totals(lines)
    assert agg["unverified"] == 1
    assert agg["problems"] == 0                  # degraded, not an error


def test_offline_search_uses_cache_only(tmp_path, monkeypatch):
    from akcli.parts import search as parts_search
    # cache-only: no cache dir -> guaranteed miss, and the network is
    # NEVER touched (the conftest network guard would explode if it were)
    import pytest
    with pytest.raises(JlcOfflineMiss):
        parts_search.search("C7593", cache_dir=None, offline=True)


def test_cli_offline_banner(tmp_path, capsys, monkeypatch):
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "kicad" / "board_v8.kicad_sch"
    work = tmp_path / "t.kicad_sch"
    shutil.copy(fixture, work)
    capsys.readouterr()
    assert cli.main(["jlc", "bom", str(work), "--offline", "--exit-zero"]) == 0
    err = capsys.readouterr().err
    assert "OFFLINE (degraded)" in err
    # --suggest needs the live catalog
    assert cli.main(["jlc", "bom", str(work), "--offline", "--suggest"]) == 2
