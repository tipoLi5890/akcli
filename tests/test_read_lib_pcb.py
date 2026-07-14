"""CLI ``read`` coverage for the library + PCB readers.

Four readers are wired into the ``read`` command:

* KiCad ``.kicad_sym`` symbol library (:mod:`readers.kicad_lib`)
* KiCad ``.kicad_pcb`` board            (:mod:`readers.kicad` ``read_pcb``)
* Altium ``.SchLib`` symbol library     (:mod:`readers.altium_schlib`)
* Altium ``.PcbDoc`` board              (:mod:`readers.altium_pcb`)

These tests drive :func:`akcli.cli.main` end-to-end and assert both
the exit code and the stdout payload (text + JSON). The Altium fixtures are
built at test time from the committed pure-stdlib generators (the same ones the
reader unit tests use), so the already-wired ``.SchLib``/``.PcbDoc`` paths are
exercised through the CLI here too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from akcli.cli import main
from akcli.errors import EXIT

FIXTURES = Path(__file__).parent / "fixtures"

GEN = FIXTURES / "_gen"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))
import altium_fixture  # noqa: E402  (committed stdlib-only fixture generator)
import ole_writer  # noqa: E402


def F(name: str) -> str:
    return str(FIXTURES / name)


def _frame_records(records: list[dict]) -> bytes:
    return b"".join(bytes(altium_fixture._frame(r)) for r in records)


# --------------------------------------------------------------------------- #
# KiCad .kicad_sym (symbol library)
# --------------------------------------------------------------------------- #
def test_read_kicad_sym_text(capsys):
    assert main(["read", F("kicad/symbols/Device.kicad_sym")]) == EXIT["OK"]
    out = capsys.readouterr().out
    # Device.kicad_sym holds R, C, C_Polarized (extends C), L.
    assert "symbols: 4" in out
    for name in ("R ", "C ", "C_Polarized ", "L "):
        assert name in out


def test_read_kicad_sym_json(capsys):
    assert main(["read", "--json", F("kicad/symbols/power.kicad_sym")]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1.2"
    assert doc["source_format"] == "kicad"
    names = [s["name"] for s in doc["symbols"]]
    assert names == ["GND", "+3V3"]
    # power symbols carry a single power_in pin
    gnd = doc["symbols"][0]
    assert gnd["pins"][0]["electrical_type"] == "power_in"
    # the writer-only raw SNode handle must export as null (not crash json.dumps)
    assert gnd["body_sexpr"] is None


def test_read_kicad_sym_detected_without_extension(tmp_path, capsys):
    # magic-byte sniff: a "(kicad_symbol_lib ..." file with no .kicad_sym suffix.
    p = tmp_path / "lib_noext"
    p.write_text(Path(F("kicad/symbols/Device.kicad_sym")).read_text(), encoding="utf-8")
    assert main(["read", str(p)]) == EXIT["OK"]
    assert "symbols: 4" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# KiCad .kicad_pcb (board)
# --------------------------------------------------------------------------- #
def test_read_kicad_pcb_text(capsys):
    assert main(["read", F("kicad/board.kicad_pcb")]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "footprints: 2" in out
    assert "nets: 2" in out
    assert "R1" in out and "C1" in out


def test_read_kicad_pcb_json(capsys):
    assert main(["read", "--json", F("kicad/board.kicad_pcb")]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1.2"
    assert doc["source_format"] == "kicad"
    # net 0 ("") is dropped; only the two named nets survive.
    assert doc["nets"] == ["GND", "+3V3"]
    by_des = {f["designator"]: f for f in doc["footprints"]}
    assert set(by_des) == {"R1", "C1"}
    # R1 uses (property "Reference"/"Value" ...); C1 uses legacy (fp_text ...).
    assert by_des["R1"]["footprint_name"] == "Resistor_SMD:R_0402_1005Metric"
    assert by_des["R1"]["value"] == "10k"
    assert by_des["C1"]["value"] == "100nF"
    assert by_des["C1"]["rotation"] == 90.0


# --------------------------------------------------------------------------- #
# Altium .SchLib / .PcbDoc (already wired; exercised here through the CLI)
# --------------------------------------------------------------------------- #
def _write_schlib(tmp_path: Path) -> Path:
    res = [
        {"RECORD": "1", "LibReference": "RES", "PartCount": "1"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "1", "Name": "A",
         "Location.X": "0", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "0", "Electrical": "4", "OwnerPartId": "1"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "2", "Name": "B",
         "Location.X": "100", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "2", "Electrical": "4", "OwnerPartId": "1"},
    ]
    cap = [
        {"RECORD": "1", "LibReference": "CAP", "PartCount": "1"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "1", "Name": "+",
         "Location.X": "0", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "1", "Electrical": "7", "OwnerPartId": "1"},
    ]
    streams = {
        "FileHeader": _frame_records(
            [{"HEADER": "Protel for Windows - Schematic Library"}]
        ),
        "RES/Data": _frame_records(res),
        "CAP/Data": _frame_records(cap),
    }
    p = tmp_path / "parts.SchLib"
    ole_writer.write_ole(str(p), streams)
    return p


def _write_pcbdoc(tmp_path: Path) -> Path:
    streams = {
        "FileHeader": _frame_records([{"HEADER": "PCB 6.0 Binary File"}]),
        "Nets6/Data": _frame_records([{"NAME": "GND"}, {"NAME": "V3V3"}]),
        "Components6/Data": _frame_records([
            {"SOURCEDESIGNATOR": "U1", "PATTERN": "QFN48", "LAYER": "TOP",
             "ROTATION": "90", "COMMENT": "nRF52833"},
            {"SOURCEDESIGNATOR": "R1", "PATTERN": "0402", "LAYER": "TOP",
             "ROTATION": "0"},
        ]),
        # no binary sections: this fixture exercises the ASCII-side CLI path
    }
    p = tmp_path / "board.PcbDoc"
    ole_writer.write_ole(str(p), streams)
    return p


def test_read_altium_schlib_cli_text(tmp_path, capsys):
    assert main(["read", str(_write_schlib(tmp_path))]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "symbols: 2" in out
    assert "RES (pins=2)" in out
    assert "CAP (pins=1)" in out


def test_read_altium_schlib_cli_json(tmp_path, capsys):
    assert main(["read", "--json", str(_write_schlib(tmp_path))]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1.2"
    assert doc["source_format"] == "altium"
    assert sorted(s["name"] for s in doc["symbols"]) == ["CAP", "RES"]


def test_read_altium_pcbdoc_cli_text(tmp_path, capsys):
    assert main(["read", str(_write_pcbdoc(tmp_path))]) == EXIT["OK"]
    out = capsys.readouterr().out
    assert "footprints: 2" in out
    assert "nets: 2" in out
    assert "U1" in out and "R1" in out


def test_read_altium_pcbdoc_cli_json(tmp_path, capsys):
    assert main(["read", "--json", str(_write_pcbdoc(tmp_path))]) == EXIT["OK"]
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1.2"
    assert doc["source_format"] == "altium"
    assert doc["nets"] == ["GND", "V3V3"]
    assert {f["designator"] for f in doc["footprints"]} == {"U1", "R1"}
