"""Full-browser UI regression for `akcli view` (optional, auto-skipped).

Needs the system Chrome plus a one-time ``npm install`` in ``tools/ui-test``
(puppeteer-core drives the installed browser; nothing is downloaded). The
fixture timeline below is the contract ``browser_test.mjs`` asserts against.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parents[1] / "tools" / "ui-test"
CHROME = os.environ.get(
    "CHROME_PATH",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210">'
       '<rect x="40" y="30" width="120" height="90" fill="none"'
       ' stroke="#333" stroke-width="0.5"/></svg>')

ERC_A = {"severity": "error", "type": "power_pin_not_driven",
         "description": "Input Power pin not driven",
         "item": "Symbol #PWR01 Pin 1", "x": 0.5, "y": 0.4}
ERC_B = {"severity": "warning", "type": "lib_symbol_mismatch",
         "description": "Symbol library mismatch",
         "item": "Symbol R1", "x": 0.9, "y": 0.7}


def _missing() -> str | None:
    if shutil.which("node") is None:
        return "node not installed"
    if not (UI_DIR / "node_modules" / "puppeteer-core").exists():
        return "puppeteer-core not installed (run `npm install` in tools/ui-test)"
    if not Path(CHROME).exists():
        return "Chrome not found (set CHROME_PATH)"
    return None


DEVICE_SYM = (Path(__file__).parent / "fixtures" / "kicad" / "symbols"
              / "Device.kicad_sym")


def _overlap_target(tmp_path: Path) -> Path:
    """A real .kicad_sch with two overlapping resistors so /api/findings
    returns a positioned LAYOUT_SYMBOL_OVERLAP the lint overlay can mark."""
    from akcli.writers import kicad as kw
    tgt = tmp_path / "t.kicad_sch"
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


def _stub_bom_network(monkeypatch) -> None:
    """Make ?check=1 offline: canned lines + a canned datasheet resolver so
    the BOM datasheet-link overlay renders without touching the network."""
    from akcli.parts import bom_jlc
    from akcli.parts import datasheet as ds_mod
    monkeypatch.setattr(bom_jlc, "check", lambda sch, **k: [
        bom_jlc.BomLine(refs=["R1", "R2"], value="10k", footprint="0603",
                        lcsc="C25804", status="ok")])
    monkeypatch.setattr(bom_jlc, "totals", lambda lines: {
        "lines": len(lines), "ok": len(lines), "problems": 0,
        "no_part_id": 0, "est_cost": 0.0})
    monkeypatch.setattr(ds_mod, "resolve", lambda lcsc, **k: ds_mod.DatasheetRow(
        lcsc=lcsc, url="https://atta.szlcsc.com/C25804.pdf", status="resolved"))


@pytest.mark.skipif(_missing() is not None, reason=str(_missing()))
def test_browser_ui(tmp_path, monkeypatch):
    from akcli.webui.server import Dash, _make_handler

    _stub_bom_network(monkeypatch)
    sdir = tmp_path / "state"
    sdir.mkdir()
    (sdir / "state.json").write_text(json.dumps({
        "version": 2, "file": "t.kicad_sch",
        "watcher_error": "kicad-cli exploded (fixture)",
        "steps": [
            {"n": 1, "svg": "step-1.svg", "sheets": ["step-1.svg"],
             "time": "10:00:00", "ts": 1.0, "note": "baseline",
             "erc_err": 1, "erc_warn": 0, "erc": [ERC_A],
             "parts": 2, "nets": 1},
            {"n": 2, "svg": "step-2.svg",
             "sheets": ["step-2.svg", "step-2-2.svg"],
             "sheet_names": ["root", "child"],
             "time": "10:00:05", "ts": 6.0, "note": "wired the pull-up",
             "erc_err": 1, "erc_warn": 1, "erc": [ERC_A, ERC_B],
             "parts": 3, "nets": 2},
        ],
    }))
    for name in ("step-1.svg", "step-2.svg", "step-2-2.svg"):
        (sdir / name).write_text(SVG)
    target = _overlap_target(tmp_path)

    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              _make_handler(Dash(sdir, target)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(os.environ,
                   AKCLI_VIEW_URL=f"http://127.0.0.1:{srv.server_address[1]}",
                   CHROME_PATH=CHROME)
        r = subprocess.run(["node", "browser_test.mjs"], cwd=UI_DIR, env=env,
                           capture_output=True, text=True, timeout=240)
        assert r.returncode == 0, f"\n{r.stdout}\n{r.stderr}"
        assert (target.parent / "note.txt").exists()   # the UI posted a note
    finally:
        srv.shutdown()
        srv.server_close()
