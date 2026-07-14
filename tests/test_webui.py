"""Unified dashboard server tests (`akcli view`).

The real server class (``_Server``, a hardened ``ThreadingHTTPServer``) is
bound to an ephemeral localhost port — the same code path ``akcli view``
runs, minus the browser and the filesystem watcher thread (steps are seeded
straight into the state dir; watcher internals get direct unit tests).
"""

from __future__ import annotations

import gzip
import http.client
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from akcli.calc import CALCS
from akcli.calc.opsmap import MAPPABLE
from akcli.webui import page
from akcli.webui.server import (
    Dash, _bind, _is_local_host, _make_handler, _origin_ok, _Server, _Watcher,
)

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210"></svg>'


def _get(port: int, path: str, headers: dict | None = None):
    """(status, body-bytes, response-headers) for a localhost GET."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _post(port: int, path: str, body: bytes = b"",
          headers: dict | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                 headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _serve(dash: Dash):
    srv = _Server(("127.0.0.1", 0), _make_handler(dash))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Watching-mode server: seeded 2-step timeline + a fake watch target."""
    sdir = tmp_path_factory.mktemp("view-state")
    (sdir / "state.json").write_text(json.dumps({
        "version": 2, "file": "t.kicad_sch",
        "steps": [
            {"n": 1, "svg": "step-1.svg", "sheets": ["step-1.svg"],
             "time": "10:00:00", "ts": 1.0, "note": "baseline",
             "erc_err": 1, "erc_warn": 0,
             "erc": [{"severity": "error", "type": "t", "description": "d",
                      "item": "i", "x": 0.5, "y": 0.4}],
             "parts": 2, "nets": 1},
            {"n": 2, "svg": "step-2.svg",
             "sheets": ["step-2.svg", "step-2-2.svg"],
             "sheet_names": ["root", "child"],
             "time": "10:00:05", "ts": 6.0, "note": "",
             "erc_err": 0, "erc_warn": 0, "erc": [],
             "parts": 3, "nets": 2},
        ],
    }))
    for name in ("step-1.svg", "step-2.svg", "step-2-2.svg"):
        (sdir / name).write_text(SVG)
    (sdir / "secret.txt").write_text("nope")
    target = tmp_path_factory.mktemp("proj") / "t.kicad_sch"
    fixture = (Path(__file__).parent / "fixtures" / "kicad"
               / "board_v8.kicad_sch")
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    dash = Dash(sdir, target)
    srv = _serve(dash)
    yield srv.server_address[1], dash, sdir, target
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def calc_only_port():
    """Calc-only server: no state dir, no watch target."""
    srv = _serve(Dash(None, None))
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- pages ----

def test_packaged_pages_exist():
    calc = page("calc.html").decode("utf-8")
    live = page("live.html").decode("utf-8")
    hub = page("hub.html").decode("utf-8")
    assert "akcli calc" in calc and "/api/run" in calc
    assert "/live/state.json" in live and "getBBox" in live
    assert "/live/events" in live                       # SSE wiring shipped
    assert 'id="werr"' in live and "watcher_error" in live   # crash banner
    assert "aria-current" in live                       # a11y timeline steps
    # lint-findings overlay wired to /api/findings (mil -> mm conversion)
    assert 'id="lintmk"' in live and "/api/findings" in live
    assert "MIL_TO_MM" in live and 'id="fovl"' in live
    assert '/calc"' in hub and '/live"' in hub and "/api/list" in hub
    # the shared bench chrome (mark + page tabs) ships on every page
    for page_src in (calc, live, hub):
        assert 'class="tabs"' in page_src and 'id="mark"' in page_src
    for marker in ("parseEng", "fmtEng", "PREFIXABLE", "engInput", "ANNOT"):
        assert marker in calc


def test_inline_scripts_are_valid_js(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    import re
    for name in ("calc.html", "live.html", "hub.html"):
        src = re.search(r"<script>(.*)</script>", page(name).decode("utf-8"),
                        re.S).group(1)
        f = tmp_path / (name + ".js")
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True)
        assert r.returncode == 0, f"{name}: {r.stderr.decode()}"


# --------------------------------------------------------------- routes ----

def test_root_serves_hub(env, calc_only_port):
    for p in (env[0], calc_only_port):
        status, body, _ = _get(p, "/")
        assert status == 200
        assert b'href="/calc"' in body and b'href="/live"' in body
        assert b"akcli" in body


def test_both_pages_served_from_one_server(env):
    port, *_ = env
    status, body, _ = _get(port, "/calc")
    assert status == 200 and b"CALC BENCH" in body.upper() or b"calc" in body
    status, body, _ = _get(port, "/live")
    assert status == 200 and b"Timeline" in body


# ------------------------------------------------------------ calc API ----

def test_calc_list_meta_and_mappable(env, calc_only_port):
    port, *_ = env
    status, body, _ = _get(port, "/api/list")
    doc = json.loads(body)
    assert status == 200
    assert doc["meta"]["count"] == len(CALCS)
    assert doc["meta"]["watching"] == "t.kicad_sch"
    calcs = [c for grp in doc["groups"].values() for c in grp]
    by_name = {c["name"]: c for c in calcs}
    assert by_name["led"]["mappable"] is True
    assert by_name["ohm"]["mappable"] is False
    assert all(c["name"] in MAPPABLE for c in calcs if c["mappable"])
    doc2 = json.loads(_get(calc_only_port, "/api/list")[1])
    assert doc2["meta"]["watching"] is None


def test_calc_run_ok_and_errors(env):
    port, *_ = env
    status, body, _ = _get(port, "/api/run?name=ohm&v=5&r=1k")
    doc = json.loads(body)
    assert status == 200 and doc["results"]["i"]["value"] == pytest.approx(0.005)

    status, body, _ = _get(port, "/api/run?name=nope")
    assert status == 400 and "unknown calculator" in json.loads(body)["error"]

    status, body, _ = _get(port, "/api/run?name=ohm&v=5")  # underdetermined
    assert status == 400 and "error" in json.loads(body)

    status, _, _ = _get(port, "/definitely-not-here")
    assert status == 404


def test_calc_ops_export(env):
    port, *_ = env
    status, body, _ = _get(port, "/api/ops?name=led&vs=5&vf=2&i=10m")
    doc = json.loads(body)
    assert status == 200 and doc["protocol_version"] == 1
    assert [o["op"] for o in doc["ops"]] == ["place_led_indicator"]
    from akcli.ops import expand_macros
    assert any(o.get("lib_id") == "Device:LED"
               for o in expand_macros(doc)["ops"])

    status, body, _ = _get(port, "/api/ops?name=ohm&v=5&r=1k")
    assert status == 400 and "--ops not supported" in json.loads(body)["error"]


def test_gzip_when_accepted(env):
    port, *_ = env
    status, body, hdrs = _get(port, "/api/list",
                              headers={"Accept-Encoding": "gzip"})
    assert status == 200 and hdrs.get("Content-Encoding") == "gzip"
    doc = json.loads(gzip.decompress(body))
    assert doc["meta"]["count"] == len(CALCS)
    # without the header: identity
    _, body, hdrs = _get(port, "/api/list")
    assert hdrs.get("Content-Encoding") is None
    json.loads(body)


# ------------------------------------------------------------- live API ----

def test_live_state_and_sheets(env):
    port, *_ = env
    status, body, _ = _get(port, "/live/state.json?ts=1")
    doc = json.loads(body)
    assert status == 200 and doc["watching"] is True
    assert doc["steps"][1]["sheets"] == ["step-2.svg", "step-2-2.svg"]

    for name in ("step-1.svg", "step-2-2.svg"):
        status, body, hdrs = _get(port, f"/live/{name}?v=1")
        assert status == 200 and b"<svg" in body
        assert "immutable" in hdrs.get("Cache-Control", "")

    # without the ?v= fingerprint the bytes may change: never cache
    status, body, hdrs = _get(port, "/live/step-1.svg")
    assert status == 200 and b"<svg" in body
    assert hdrs.get("Cache-Control") == "no-store"


def test_live_blocks_everything_else(env):
    port, *_ = env
    for path in ("/live/secret.txt", "/live/../pyproject.toml",
                 "/live/step-1.svg/../secret.txt", "/live/state.json.tmp",
                 "/live/step-9.svg", "/state.json", "/step-1.svg"):
        status, _, _ = _get(port, path)
        assert status == 404, path


def test_sse_pushes_versions(env):
    port, dash, *_ = env
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live/events")
    resp = conn.getresponse()
    assert resp.getheader("Content-Type") == "text/event-stream"
    hello = resp.fp.readline()
    assert hello.startswith(b"data: ")
    v0 = json.loads(hello[6:])["version"]
    resp.fp.readline()                    # blank line after the event
    dash.publish()
    push = resp.fp.readline()
    assert json.loads(push[6:])["version"] == v0 + 1
    conn.close()


def test_live_bom_offline_and_checked(env, calc_only_port, monkeypatch):
    port, *_ = env
    # offline: BOM lines, no network
    status, body, _ = _get(port, "/live/bom")
    doc = json.loads(body)
    assert status == 200 and doc["checked"] is False
    refs = {tuple(ln["refs"]) for ln in doc["lines"]}
    assert ("R1", "R2") in refs                     # same value+fp -> one line
    assert ("C1",) in refs
    assert not any(r.startswith("#") for t in refs for r in t)   # no #PWR

    # checked: catalog data via (monkeypatched) search layer
    from akcli.parts import search as parts_search
    monkeypatch.setattr(
        parts_search, "get",
        lambda lcsc, **k: parts_search.Part(
            lcsc=lcsc, mpn="X", description="", package="0603", stock=9,
            price=0.5, basic=True, datasheet=None, category="R",
            attributes={}))
    monkeypatch.setattr(parts_search, "search", lambda q, **k: [])
    status, body, _ = _get(port, "/live/bom?check=1")
    doc = json.loads(body)
    assert status == 200 and doc["checked"] is True and "totals" in doc

    # not watching -> 409
    status, body, _ = _get(calc_only_port, "/live/bom")
    assert status == 409


# ---------------------------------------------------------- findings API ----

DEVICE_SYM = (Path(__file__).parent / "fixtures" / "kicad" / "symbols"
              / "Device.kicad_sym")


def _overlap_sch(tmp_path: Path) -> Path:
    """A .kicad_sch with two overlapping resistors — yields a positioned
    LAYOUT_SYMBOL_OVERLAP finding (pos in mils, root frame)."""
    from akcli.writers import kicad as kw
    tgt = tmp_path / "overlap.kicad_sch"
    tgt.write_text(
        '(kicad_sch (version 20231120) (generator "akcli") '
        '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n')
    kw.apply(
        {"protocol_version": 1, "target_format": "kicad", "ops": [
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 2000},
            {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
             "x_mil": 2050, "y_mil": 2000}]},
        str(tgt), apply=True, sources=[str(DEVICE_SYM)])
    return tgt


def test_api_findings_offline_positions(tmp_path):
    from akcli.checks import layout
    tgt = _overlap_sch(tmp_path)
    sdir = tmp_path / "state"
    sdir.mkdir()
    srv = _serve(Dash(sdir, tgt))
    port = srv.server_address[1]
    try:
        status, body, _ = _get(port, "/api/findings?file=overlap.kicad_sch")
        doc = json.loads(body)
        assert status == 200
        codes = {f["code"] for f in doc["findings"]}
        assert layout.LAYOUT_SYMBOL_OVERLAP in codes
        overlap = next(f for f in doc["findings"]
                       if f["code"] == layout.LAYOUT_SYMBOL_OVERLAP)
        # positions ride along as [x_mil, y_mil]; anchors name the entities
        assert overlap["pos"][0] == pytest.approx(2000, abs=1)
        assert overlap["pos"][1] == pytest.approx(2000, abs=1)
        assert overlap["anchors"] and overlap["anchors"][0]["kind"] == "component"
        assert doc["count"] == len(doc["findings"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_findings_cached_per_mtime(tmp_path):
    import os
    tgt = _overlap_sch(tmp_path)
    sdir = tmp_path / "state"
    sdir.mkdir()
    dash = Dash(sdir, tgt)
    a = dash.findings()
    assert dash.findings() is a                 # same mtime -> memoized object
    st = tgt.stat()
    os.utime(tgt, (st.st_atime, st.st_mtime + 5))
    c = dash.findings()
    assert c is not a and c["count"] == a["count"]


def test_api_findings_not_watching(calc_only_port):
    status, body, _ = _get(calc_only_port, "/api/findings")
    assert status == 409 and "not watching" in json.loads(body)["error"]


def test_api_findings_error_is_json_not_dropped(tmp_path):
    tgt = tmp_path / "broken.kicad_sch"
    tgt.write_text("(kicad_sch")                # unterminated -> parse error
    sdir = tmp_path / "s"
    sdir.mkdir()
    srv = _serve(Dash(sdir, tgt))
    port = srv.server_address[1]
    try:
        status, body, _ = _get(port, "/api/findings")
        doc = json.loads(body)
        assert status == 500 and "error" in doc   # answered, never dropped
    finally:
        srv.shutdown()
        srv.server_close()


def test_live_bom_datasheet_links(env, monkeypatch):
    """?check=1 attaches a per-line datasheet link (pdf/page), tolerating
    per-line resolver failures; the offline path never resolves."""
    port, *_ = env
    from akcli.parts import bom_jlc
    from akcli.parts import datasheet as ds_mod

    def fake_check(sch, **k):
        return [
            bom_jlc.BomLine(refs=["U1"], value="MCU", footprint="QFN",
                            lcsc="C1234", status="ok"),
            bom_jlc.BomLine(refs=["J1"], value="HDR", footprint="1x4",
                            lcsc="C9", status="ok"),
            bom_jlc.BomLine(refs=["D1"], value="LED", footprint="0603",
                            lcsc="CBAD", status="ok"),   # resolver blows up
            bom_jlc.BomLine(refs=["R1"], value="10k", footprint="0603"),  # no id
        ]
    monkeypatch.setattr(bom_jlc, "check", fake_check)
    monkeypatch.setattr(bom_jlc, "totals", lambda lines: {"lines": len(lines)})

    def fake_resolve(lcsc, **k):
        if lcsc == "C1234":
            return ds_mod.DatasheetRow(lcsc=lcsc, url="https://a/x.pdf",
                                       status="resolved")
        if lcsc == "C9":
            return ds_mod.DatasheetRow(lcsc=lcsc, status="page-link",
                                       url="https://item.szlcsc.com/9.html")
        raise RuntimeError("resolver exploded")
    monkeypatch.setattr(ds_mod, "resolve", fake_resolve)

    status, body, _ = _get(port, "/live/bom?check=1")
    doc = json.loads(body)
    assert status == 200 and doc["checked"] is True
    by_ref = {tuple(ln["refs"]): ln for ln in doc["lines"]}
    assert by_ref[("U1",)]["datasheet"] == {"url": "https://a/x.pdf", "kind": "pdf"}
    assert by_ref[("J1",)]["datasheet"] == {
        "url": "https://item.szlcsc.com/9.html", "kind": "page"}
    assert "datasheet" not in by_ref[("D1",)]   # resolver error tolerated
    assert "datasheet" not in by_ref[("R1",)]   # no lcsc -> never resolved

    # the OFFLINE path must never carry datasheet links (no network there)
    monkeypatch.setattr(bom_jlc, "collect_lines", lambda sch: fake_check(sch))
    status, body, _ = _get(port, "/live/bom")
    doc = json.loads(body)
    assert status == 200 and doc["checked"] is False
    assert all("datasheet" not in ln for ln in doc["lines"])


# ------------------------------------------------------------- security ----

def test_local_host_and_origin_helpers():
    for host in (None, "127.0.0.1", "127.0.0.1:8765", "localhost",
                 "LocalHost:80", "[::1]", "[::1]:8765", "::1"):
        assert _is_local_host(host), host
    for host in ("evil.example.com", "evil.example.com:8765",
                 "127.0.0.1.evil.com", "192.168.1.7:8765", ""):
        assert not _is_local_host(host), host
    assert _origin_ok(None, "127.0.0.1:8765")
    assert _origin_ok("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert _origin_ok("http://localhost:5173", "127.0.0.1:8765")
    assert _origin_ok("http://[::1]:8765", "127.0.0.1:8765")
    for origin in ("http://evil.example.com", "https://evil.example.com:8765",
                   "null", "garbage"):
        assert not _origin_ok(origin, "127.0.0.1:8765"), origin


def test_dns_rebinding_host_rejected(env, calc_only_port):
    """evil.com rebound to 127.0.0.1 still sends `Host: evil.com` -> 403."""
    for p in (env[0], calc_only_port):
        for path in ("/", "/api/list", "/live/state.json", "/live/events"):
            status, body, _ = _get(p, path, headers={"Host": "evil.example.com"})
            assert status == 403, (p, path)
            assert b"Host" in body
    # localhost-family Hosts (any port) keep working
    port = env[0]
    for host in (f"127.0.0.1:{port}", "localhost", "localhost:80", "[::1]:9"):
        status, _, _ = _get(port, "/", headers={"Host": host})
        assert status == 200, host


def test_cross_origin_post_rejected(env):
    port, _, _, target = env
    note = target.parent / "note.txt"
    note.unlink(missing_ok=True)
    for origin in ("http://evil.example.com", "https://evil.example.com:443",
                   "null"):
        status, body = _post(port, "/live/note", b"pwned",
                             headers={"Origin": origin})
        assert status == 403, origin
        status, _ = _post(port, "/live/clear", headers={"Origin": origin})
        assert status == 403, origin
    assert not note.exists()                    # nothing leaked through
    assert json.loads(_get(port, "/live/state.json")[1])["steps"]  # intact
    # same-origin fetch (browsers send Origin on POST) and bare curl pass
    status, _ = _post(port, "/live/note", b"ok",
                      headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200 and note.exists()
    note.unlink()
    # an evil Host on POST is refused even with no Origin
    status, _ = _post(port, "/live/note", b"x",
                      headers={"Host": "evil.example.com"})
    assert status == 403


# ----------------------------------------------------- watcher hardening ----

def _script_wrapper(tmp_path: Path, name: str, py: Path) -> Path:
    """A launcher the OS can actually exec: Windows CreateProcess cannot run
    a shebang script (WinError 193) but handles ``.cmd``; POSIX gets the
    usual ``#!/bin/sh`` wrapper. The logic itself stays in one ``.py``."""
    if sys.platform == "win32":
        stub = tmp_path / f"{name}.cmd"
        stub.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n')
        return stub
    stub = tmp_path / name
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n')
    stub.chmod(0o755)
    return stub


def _stub_kicad(tmp_path: Path) -> Path:
    """A fake kicad-cli: exports two SVGs / an empty JSON ERC report."""
    py = tmp_path / "kicad-cli-stub.py"
    py.write_text(
        "import os, sys\n"
        f"SVG = {SVG!r}\n"
        "args = sys.argv[1:]\n"
        "out = None\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    if args[i] == '-o':\n"
        "        out = args[i + 1]; i += 2; continue\n"
        "    if args[i] == '--output':\n"
        "        open(args[i + 1], 'w').write('{\"sheets\": []}')\n"
        "        raise SystemExit(0)\n"
        "    i += 1\n"
        "open(os.path.join(out, 'board.svg'), 'w').write(SVG)\n"
        "open(os.path.join(out, 'board_child.svg'), 'w').write(SVG)\n")
    return _script_wrapper(tmp_path, "kicad-cli-stub", py)


def _watch_env(tmp_path: Path) -> tuple[Dash, Path]:
    sdir = tmp_path / "state"
    sdir.mkdir()
    target = tmp_path / "board.kicad_sch"
    target.write_text("(kicad_sch)")
    return Dash(sdir, target), sdir


def test_export_svgs_via_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CLI", str(_stub_kicad(tmp_path)))
    dash, sdir = _watch_env(tmp_path)
    files, names = _Watcher(dash)._export_svgs(7)
    assert files == ["step-7.svg", "step-7-2.svg"]
    assert names == ["root", "child"]           # stem stripped from labels
    assert (sdir / "step-7.svg").exists() and (sdir / "step-7-2.svg").exists()


def test_export_svgs_failure_and_timeout(tmp_path, monkeypatch):
    # non-zero exit (intermediate broken save): no step, no error state
    bad_py = tmp_path / "kicad-cli-bad.py"
    bad_py.write_text("raise SystemExit(1)\n")
    bad = _script_wrapper(tmp_path, "kicad-cli-bad", bad_py)
    monkeypatch.setenv("KICAD_CLI", str(bad))
    dash, _ = _watch_env(tmp_path)
    w = _Watcher(dash)
    assert w._export_svgs(1) == ([], [])
    assert "watcher_error" not in dash.state

    # a hung kicad-cli: TimeoutExpired -> ([], []) + published banner state
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="kicad-cli", timeout=60)
    import akcli.webui.server as server_mod
    monkeypatch.setattr(server_mod.subprocess, "run", hang)
    v0 = dash.state["version"]
    assert w._export_svgs(2) == ([], [])
    assert "timed out" in dash.state["watcher_error"]
    assert dash.state["version"] == v0 + 1


def test_wait_stable_tolerates_vanishing_file(tmp_path):
    dash, _ = _watch_env(tmp_path)
    w = _Watcher(dash)
    dash.target.unlink()                        # editor mid-rename
    assert w._stat_mtime() is None
    assert w._wait_stable(quiet=0.01, max_wait=0.05) == 0.0
    dash.target.write_text("(kicad_sch)")
    m = w._wait_stable(quiet=0.01, max_wait=0.5)
    assert m == dash.target.stat().st_mtime


def test_poll_once_builds_step_and_clears_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KICAD_CLI", str(_stub_kicad(tmp_path)))
    dash, sdir = _watch_env(tmp_path)
    dash.state["watcher_error"] = "previous crash"
    dash.state["next_n"] = 5                    # as left by /live/clear
    w = _Watcher(dash)
    w._wait_stable = lambda **k: dash.target.stat().st_mtime  # skip the wait
    w._poll_once()
    steps = dash.state["steps"]
    assert [s["n"] for s in steps] == [5]       # numbering resumed, not reset
    assert steps[0]["sheets"] == ["step-5.svg", "step-5-2.svg"]
    assert steps[0]["sheet_names"] == ["root", "child"]
    assert steps[0]["note"] == "baseline"
    assert "watcher_error" not in dash.state    # a good step clears the banner
    assert (sdir / "step-5.svg").exists()
    w._poll_once()                              # unchanged mtime: no new step
    assert len(dash.state["steps"]) == 1


def test_watcher_crash_surfaces_not_dies(tmp_path):
    dash, _ = _watch_env(tmp_path)
    w = _Watcher(dash)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")

    w._poll_once = boom
    w.start()                                   # daemon; killed with pytest
    deadline = time.time() + 5
    while time.time() < deadline and not dash.state.get("watcher_error"):
        time.sleep(0.02)
    assert dash.state["watcher_error"] == "RuntimeError: boom"
    assert calls and w.is_alive()               # the loop survived the crash


def test_watcher_trim_deletes_dropped_svgs(tmp_path):
    dash, sdir = _watch_env(tmp_path)
    dash.max_steps = 2
    for n in (1, 2, 3):
        name = f"step-{n}.svg"
        (sdir / name).write_text(SVG)
        dash.state["steps"].append({"n": n, "svg": name, "sheets": [name]})
    w = _Watcher(dash)
    with dash.lock:
        w._trim()
    assert [s["n"] for s in dash.state["steps"]] == [2, 3]
    assert not (sdir / "step-1.svg").exists()
    assert (sdir / "step-2.svg").exists() and (sdir / "step-3.svg").exists()


# --------------------------------------------------- state.json loading ----

def test_load_normalizes_malformed_state(tmp_path):
    cases = ["[]", "{}", "null", "not json", '{"version": 3, "steps": null}',
             '{"steps": {"a": 1}}',
             '{"version": "x", "steps": [null, 42, {"n": 1}]}']
    for i, text in enumerate(cases):
        d = tmp_path / f"s{i}"
        d.mkdir()
        (d / "state.json").write_text(text)
        dash = Dash(d, None)
        assert isinstance(dash.state["version"], int), text
        assert all(isinstance(s, dict) for s in dash.state["steps"]), text
        dash.publish()                          # must not crash post-load
    # malformed steps are dropped, dict steps survive
    d = tmp_path / "keep"
    d.mkdir()
    (d / "state.json").write_text('{"version": 9, "steps": [null, {"n": 4}]}')
    dash = Dash(d, None)
    assert dash.state["version"] == 9
    assert dash.state["steps"] == [{"n": 4}]


# ------------------------------------------------------- server plumbing ----

def test_bind_auto_increment_and_exhaustion():
    handler = _make_handler(Dash(None, None))
    s1, _ = _bind(0, handler, tries=1)          # port 0: kernel-assigned
    assert s1 is not None
    base = s1.server_address[1]
    try:
        s2, p2 = _bind(base, handler, tries=3)  # base taken -> increments
        assert s2 is not None and p2 == s2.server_address[1] > base
        s2.server_close()
        s3, p3 = _bind(base, handler, tries=1)  # window exhausted
        assert s3 is None and p3 == base
    finally:
        s1.server_close()


def test_sse_unsubscribes_dropped_client():
    dash = Dash(None, None)                     # hermetic: fresh bus
    srv = _serve(dash)
    port = srv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/live/events")
        resp = conn.getresponse()
        resp.fp.readline(), resp.fp.readline()  # hello event + blank line
        assert len(dash.bus._subs) == 1
        resp.close()                            # client walks away (resp
        conn.close()                            # holds the socket fd alive)
        # the handler notices on its next write and cleans up its queue
        deadline = time.time() + 5
        while time.time() < deadline and dash.bus._subs:
            dash.publish()
            time.sleep(0.05)
        assert not dash.bus._subs
    finally:
        srv.shutdown()
        srv.server_close()


def test_live_bom_error_paths(env, monkeypatch):
    port, *_ = env
    from akcli.parts import bom_jlc
    from akcli.parts import search as parts_search

    # network down -> 502 with a clean message (no traceback leak)
    def net_down(*a, **k):
        raise parts_search.JlcNetworkError("host unreachable", kind="network")
    monkeypatch.setattr(bom_jlc, "check", net_down)
    status, body, _ = _get(port, "/live/bom?check=1")
    doc = json.loads(body)
    assert status == 502 and "network: host unreachable" in doc["error"]

    # a bug in either branch -> JSON 500, the page stays alive
    def bug(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(bom_jlc, "check", bug)
    status, body, _ = _get(port, "/live/bom?check=1")
    assert status == 500 and "RuntimeError" in json.loads(body)["error"]
    monkeypatch.setattr(bom_jlc, "collect_lines", bug)
    status, body, _ = _get(port, "/live/bom")
    assert status == 500 and "RuntimeError" in json.loads(body)["error"]

    # older bom_jlc without the public name: falls back to _collect_lines
    monkeypatch.delattr(bom_jlc, "collect_lines")
    status, body, _ = _get(port, "/live/bom")
    assert status == 200 and json.loads(body)["checked"] is False


def test_note_post(env, calc_only_port):
    port, _, _, target = env
    status, body = _post(port, "/live/note", "接上上拉電阻".encode("utf-8"))
    assert status == 200
    assert (target.parent / "note.txt").read_text(
        encoding="utf-8").strip() == "接上上拉電阻"
    status, _ = _post(calc_only_port, "/live/note", b"x")
    assert status == 409                   # nothing is being watched


def test_zz_clear_wipes_timeline(env):
    """Runs last: clearing destroys the seeded steps."""
    port, dash, sdir, _ = env
    v_before = dash.state["version"]
    status, _ = _post(port, "/live/clear")
    assert status == 200
    doc = json.loads(_get(port, "/live/state.json")[1])
    assert doc["steps"] == [] and doc["version"] == v_before + 1
    assert not list(sdir.glob("step-*.svg"))   # exported SVGs deleted
    # step numbering survives the wipe: the next step is #3, not #1,
    # so a cached step-1.svg URL can never alias fresh bytes
    assert doc["next_n"] == 3
