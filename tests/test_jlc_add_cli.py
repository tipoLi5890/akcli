"""CLI-layer tests for `akcli jlc add` (offline; captured EasyEDA fixtures).

Complements ``test_jlc2kicad.py`` (driver layer): these run the real argparse
handler through ``cli.main`` — flag validation, exit-code mapping, and the
``--place`` op-list emission.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from akcli import cli
from akcli.commands import jlc as jlc_cmd
from akcli._vendor.jlc2kicadlib import _http

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
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)


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
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C999999999", "--out", str(tmp_path)]) == 4


def test_network_error_exits_7(tmp_path, monkeypatch):
    class _Down:
        def open(self, request, *, timeout=None):
            raise OSError("no route")

    monkeypatch.setattr(_http, "opener", _Down())
    monkeypatch.setattr(jlc_cmd, "_easyeda_enrich", lambda lcsc: None)
    assert cli.main(["jlc", "add", "C25804", "--out", str(tmp_path)]) == 7


def test_place_oplist_value_is_the_part_value_not_the_mpn(tmp_path, offline, capsys):
    """The op-list Value must be the converted symbol's Value ("10k"), which the
    library and the placed instance then agree on."""
    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out), "--place",
                     "--designator", "R1", "--at", "0", "0"]) == 0
    (op,) = json.loads((out / "place.json").read_text())["ops"]
    assert op["value"] == "10k"


def test_place_hint_prints_a_draw_command_that_actually_works(tmp_path, offline,
                                                             capsys):
    """The printed hint used to omit --symbols, so following it was a guaranteed
    exit 6 (SYMBOL_NOT_FOUND): the symbol only exists in the fresh library."""
    import shlex

    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out), "--place",
                     "--designator", "R1", "--at", "2000", "1000"]) == 0
    hint = next(ln for ln in capsys.readouterr().out.splitlines()
                if "apply with:" in ln)
    assert "--symbols" in hint
    sym = str(out / "symbol" / "akcli.kicad_sym")
    assert sym in hint

    target = tmp_path / "sheet.kicad_sch"
    assert cli.main(["new", str(target)]) == 0
    argv = shlex.split(hint.split("apply with:", 1)[1].strip(),
                       posix=(os.name != "nt"))[1:]      # drop the "akcli" word
    argv = [str(target) if a == "<target.kicad_sch>" else a for a in argv]
    assert cli.main(argv) == 0
    text = target.read_text(encoding="utf-8")
    assert '"R1"' in text
    assert '(property "Value" "10k"' in text


def test_converted_passive_pins_keep_erc_clean(tmp_path, offline, capsys):
    """End of the chain the pin-type fix exists for: two converted resistors
    wired together must NOT raise ERC_FLOATING_INPUT (they did as `input` pins)."""
    out = tmp_path / "lib"
    assert cli.main(["jlc", "add", "C25804", "--out", str(out)]) == 0
    sym = out / "symbol" / "akcli.kicad_sym"
    lib_id = "akcli:0603WAF1002T5E"

    target = tmp_path / "sheet.kicad_sch"
    assert cli.main(["new", str(target)]) == 0
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"protocol_version": 2, "target_format": "kicad", "ops": [
        {"op": "place_component", "lib_id": lib_id, "designator": "R1",
         "x_mil": 2000, "y_mil": 1000},
        {"op": "place_component", "lib_id": lib_id, "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "add_wire", "vertices": ["R1.2", "R2.1"]},
    ]}), encoding="utf-8", newline="\n")
    assert cli.main(["draw", str(target), "--ops", str(ops),
                     "--symbols", str(sym), "--apply"]) == 0
    capsys.readouterr()

    cli.main(["check", str(target), "--erc", "--json", "--exit-zero"])
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert "ERC_FLOATING_INPUT" not in codes

    # ... and the rule really is pin-type gated: forcing the cached pins back to
    # `input` (the pre-fix conversion output) reproduces the finding.
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("(pin passive line", "(pin input line"),
                      encoding="utf-8", newline="\n")
    cli.main(["check", str(target), "--erc", "--json", "--exit-zero"])
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert "ERC_FLOATING_INPUT" in codes
