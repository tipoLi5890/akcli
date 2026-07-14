"""Tests for the vendored JLC2KiCadLib conversion path (offline, fixture-driven).

The ``_http`` shim's ``opener`` is injected so no test touches the network; the
fixtures are real EasyEDA API payloads for C25804 (an 0603 resistor), captured
once. Covers the driver orchestration, the clean-room ``_kmt`` footprint
writer's output (parsed back with akcli's own s-expression reader), and the
error paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli._vendor.jlc2kicadlib import _http, _kmt
from akcli.drivers import jlc2kicad
from akcli.readers import kicad_lib, sexpr

FIX = Path(__file__).parent / "fixtures" / "jlc"
SVGS = FIX / "C25804_svgs.json"


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, n=-1):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FixtureOpener:
    """Serves the captured EasyEDA payloads by URL suffix."""

    def __init__(self):
        d = json.loads(SVGS.read_text())
        self.by_uuid = {}
        uuids = [i["component_uuid"] for i in d["result"]]
        for p in FIX.glob("C25804_*_*.json"):
            uuid = p.stem.rsplit("_", 1)[-1]
            self.by_uuid[uuid] = p.read_bytes()
        assert set(uuids) <= set(self.by_uuid), "fixture uuids out of sync"

    def open(self, request, *, timeout=None):
        url = request.full_url
        if url.endswith("/svgs"):
            return _FakeResp(SVGS.read_bytes())
        for uuid, body in self.by_uuid.items():
            if url.endswith(uuid):
                return _FakeResp(body)
        return _FakeResp(b"", status=404)


@pytest.fixture()
def offline_http(monkeypatch):
    monkeypatch.setattr(_http, "opener", _FixtureOpener())


# --------------------------------------------------------------------------- #
# driver: full conversion from fixtures
# --------------------------------------------------------------------------- #
def test_convert_produces_symbol_and_footprint(tmp_path, offline_http):
    res = jlc2kicad.convert("C25804", str(tmp_path / "out"))
    assert res.error_code is None, res.message
    kinds = {Path(a).suffix for a in res.artifacts}
    assert ".kicad_sym" in kinds and ".kicad_mod" in kinds

    # the produced symbol parses with akcli's own reader and has 2 pins
    sym = next(a for a in res.artifacts if a.endswith(".kicad_sym"))
    lib = kicad_lib.read(sym)
    assert len(lib.symbols) == 1
    assert len(lib.symbols[0].pins) == 2

    # the produced footprint is modern (footprint ...) s-expr with 2 pads
    mod = next(a for a in res.artifacts if a.endswith(".kicad_mod"))
    doc = sexpr.parse(Path(mod).read_text())
    assert doc.tag == "footprint"
    assert len(doc.find_all("pad")) == 2
    # every pad is SMT on the F-side stack (an 0603 resistor)
    for pad in doc.find_all("pad"):
        assert pad.children[2].value == "smd"


def test_convert_part_not_found(tmp_path, monkeypatch):
    class _NotFound:
        def open(self, request, *, timeout=None):
            return _FakeResp(b'{"success": false, "result": []}')

    monkeypatch.setattr(_http, "opener", _NotFound())
    res = jlc2kicad.convert("C999999999", str(tmp_path / "out"))
    assert res.error_code == "CONVERT_PART_NOT_FOUND"


def test_convert_network_error(tmp_path, monkeypatch):
    class _Down:
        def open(self, request, *, timeout=None):
            raise OSError("no route to host")

    monkeypatch.setattr(_http, "opener", _Down())
    res = jlc2kicad.convert("C25804", str(tmp_path / "out"))
    assert res.error_code == "NETWORK"


# --------------------------------------------------------------------------- #
# _kmt: clean-room footprint writer
# --------------------------------------------------------------------------- #
def _roundtrip(fp: _kmt.Footprint):
    return sexpr.parse(_kmt.KicadFileHandler(fp).serialize())


def test_kmt_translation_offsets_children_but_not_later_appends():
    fp = _kmt.Footprint('"T"')
    fp.append(_kmt.Line(start=(1, 1), end=(2, 1), width=0.12, layer="F.SilkS"))
    fp.insert(_kmt.Translation(-1, -1))       # wraps the existing line
    fp.append(_kmt.Line(start=(5, 5), end=(6, 5), width=0.12, layer="F.SilkS"))
    doc = _roundtrip(fp)
    lines = doc.find_all("fp_line")
    starts = sorted(
        (float(line.find("start").children[1].value),
         float(line.find("start").children[2].value))
        for line in lines
    )
    assert starts == [(0.0, 0.0), (5.0, 5.0)]  # first translated, second not


def test_kmt_tht_pad_carries_drill_and_smt_pad_does_not():
    fp = _kmt.Footprint("P")
    fp.append(_kmt.Pad(number="1", type=_kmt.Pad.TYPE_THT, shape=_kmt.Pad.SHAPE_CIRCLE,
                       at=(0, 0), size=(1.6, 1.6), drill=0.8, layers=_kmt.Pad.LAYERS_THT))
    fp.append(_kmt.Pad(number="2", type=_kmt.Pad.TYPE_SMT, shape=_kmt.Pad.SHAPE_RECT,
                       at=(2, 0), size=(1, 1), drill=0, layers=_kmt.Pad.LAYERS_SMT))
    doc = _roundtrip(fp)
    pads = doc.find_all("pad")
    assert pads[0].find("drill") is not None
    assert pads[1].find("drill") is None
    assert doc.find("attr") is not None


def test_kmt_arc_mid_lies_on_the_circle():
    fp = _kmt.Footprint("A")
    fp.append(_kmt.Arc(start=(1, 0), end=(0, 1), center=(0, 0), width=0.1,
                       layer="F.SilkS"))
    doc = _roundtrip(fp)
    arc = doc.find("fp_arc")
    mid = arc.find("mid")
    mx, my = float(mid.children[1].value), float(mid.children[2].value)
    assert abs((mx * mx + my * my) ** 0.5 - 1.0) < 1e-6  # radius preserved


def test_kmt_model_offset_converted_to_mm():
    fp = _kmt.Footprint("M")
    fp.append(_kmt.Model(filename='"$(V)/x.step"', at=[1.0, 0, 0], rotate=[0, 0, 0]))
    doc = _roundtrip(fp)
    model = doc.find("model")
    assert model.children[1].value == "$(V)/x.step"   # pre-quoting stripped
    off = model.find("offset").find("xyz")
    assert float(off.children[1].value) == 25.4       # inches -> mm
