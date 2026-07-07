"""CLI-layer tests for `akcli jlc add` (offline; captured EasyEDA fixtures).

Complements ``test_jlc2kicad.py`` (driver layer): these run the real argparse
handler through ``cli.main`` — flag validation, exit-code mapping, and the
``--place`` op-list emission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from altium_kicad_cli import cli
from altium_kicad_cli._vendor.jlc2kicadlib import _http

FIX = Path(__file__).parent / "fixtures" / "jlc"


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def read(self, n=-1):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FixtureOpener:
    def __init__(self):
        self.by_uuid = {
            p.stem.rsplit("_", 1)[-1]: p.read_bytes()
            for p in FIX.glob("C25804_*_*.json")
        }

    def open(self, request, *, timeout=None):
        url = request.full_url
        if url.endswith("/svgs"):
            return _FakeResp((FIX / "C25804_svgs.json").read_bytes())
        for uuid, body in self.by_uuid.items():
            if url.endswith(uuid):
                return _FakeResp(body)
        return _FakeResp(b"", status=404)


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setattr(_http, "opener", _FixtureOpener())
    # jlcsearch enrichment is a separate network path — keep the test offline.
    monkeypatch.setattr(cli, "_easyeda_enrich", lambda lcsc: None)


def test_add_with_place_emits_oplist(tmp_path, offline, capsys):
    out = tmp_path / "lib"
    rc = cli.main(["jlc", "add", "C25804", "--out", str(out),
                   "--place", "--designator", "R1", "--at", "2000", "1000"])
    assert rc == 0
    place = json.loads((out / "place.json").read_text())
    (op,) = place["ops"]
    assert op["op"] == "place_component"
    assert op["designator"] == "R1"
    assert op["lib_id"].startswith("akcli:")
    assert op["footprint"].startswith("footprint:")
    assert (op["x_mil"], op["y_mil"]) == (2000.0, 1000.0)
    assert (out / "symbol" / "akcli.kicad_sym").is_file()


def test_place_requires_designator_and_at(tmp_path, offline):
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path / "a"),
                     "--place", "--at", "1", "1"]) == 2
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path / "b"),
                     "--place", "--designator", "R1"]) == 2


def test_missing_cnumber_is_usage_error(offline):
    assert cli.main(["jlc", "add"]) == 2


def test_part_not_found_exits_4(tmp_path, monkeypatch):
    class _NotFound:
        def open(self, request, *, timeout=None):
            return _FakeResp(b'{"success": false, "result": []}')

    monkeypatch.setattr(_http, "opener", _NotFound())
    monkeypatch.setattr(cli, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C999999999", "--out", str(tmp_path)]) == 4


def test_network_error_exits_7(tmp_path, monkeypatch):
    class _Down:
        def open(self, request, *, timeout=None):
            raise OSError("no route")

    monkeypatch.setattr(_http, "opener", _Down())
    monkeypatch.setattr(cli, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path)]) == 7
