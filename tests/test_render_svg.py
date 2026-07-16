"""`akcli render` — install-free, format-agnostic, deterministic SVG.

Connectivity-true guarantees: every wire segment and every component (with
refdes) present in the model appears in the SVG; same input renders to
byte-identical output; both KiCad and Altium sources work with no EDA install.
"""

from __future__ import annotations

import io
import json
import contextlib
import xml.etree.ElementTree as ET
from pathlib import Path

from akcli import render_svg
from akcli.cli import main
from akcli.errors import EXIT
from akcli.readers import kicad as kreader

ROOT = Path(__file__).resolve().parents[1]
KICAD_FIXTURE = ROOT / "tests" / "fixtures" / "kicad" / "board_v8.kicad_sch"
ALTIUM_FIXTURE = ROOT / "tests" / "fixtures" / "shared_name_label.SchDoc"

_SVG_NS = "{http://www.w3.org/2000/svg}"


def _render_file(path: Path) -> str:
    sch = kreader.read_sch(str(path)) if path.suffix == ".kicad_sch" else None
    if sch is None:
        from akcli.readers import altium_sch
        sch = altium_sch.read(str(path))
        prims = altium_sch.read_primitives(str(path))
    else:
        prims = kreader.read_primitives(str(path))
    return render_svg.render(sch, prims)


def test_kicad_render_is_connectivity_true():
    svg = _render_file(KICAD_FIXTURE)
    root = ET.fromstring(svg)
    prims = kreader.read_primitives(str(KICAD_FIXTURE))
    sch = kreader.read_sch(str(KICAD_FIXTURE))

    wires = [e for e in root.iter(f"{_SVG_NS}line")
             if e.get("class") == "wire"]
    assert len(wires) == len(prims.wires)

    refs = {g.get("data-ref") for g in root.iter(f"{_SVG_NS}g")
            if g.get("data-ref")}
    assert refs == {c.designator for c in sch.components}

    junctions = [e for e in root.iter(f"{_SVG_NS}circle")
                 if e.get("class") == "junction"]
    assert len(junctions) == len(prims.junctions)


def test_altium_render_no_eda_install():
    svg = _render_file(ALTIUM_FIXTURE)
    root = ET.fromstring(svg)
    texts = {t.text for t in root.iter(f"{_SVG_NS}text")}
    assert "U3" in texts and "R12" in texts
    assert "STAT" in texts  # the net label


def test_render_is_deterministic():
    assert _render_file(KICAD_FIXTURE) == _render_file(KICAD_FIXTURE)


def test_cli_render_writes_svg(tmp_path: Path, capsys):
    out = tmp_path / "board.svg"
    assert main(["render", str(KICAD_FIXTURE), "-o", str(out), "--json"]) \
        == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["render_version"] == render_svg.RENDER_VERSION
    assert doc["components"] == 5 and doc["output"] == str(out)
    ET.parse(out)  # valid XML


def test_cli_render_stdout(capsys):
    assert main(["render", str(ALTIUM_FIXTURE), "-o", "-"]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert out.startswith("<svg ")
    ET.fromstring(out)


def test_cli_render_default_output(tmp_path: Path, capsys):
    import shutil
    target = tmp_path / "b.kicad_sch"
    shutil.copy2(KICAD_FIXTURE, target)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        assert main(["render", str(target)]) == EXIT["OK"]
    assert (tmp_path / "b.kicad_sch.svg").exists()
