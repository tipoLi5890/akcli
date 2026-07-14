"""Tests for the Altium ``.SchLib`` -> ``model.Library`` reader.

A SchLib stores each symbol in its own storage ``Data`` stream, which would
collide under bare-name keying -- the reader must recover EVERY symbol via the
path-qualified directory walk, and refuse binary symbol records loudly.

Fixtures are built at test time from the committed pure-stdlib generators
(``_gen/ole_writer.py``); no binary blob is committed for the library case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from akcli.errors import AkcliError
from akcli.model import PinType
from akcli.readers import altium_schlib

GEN = Path(__file__).resolve().parent / "fixtures" / "_gen"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))
import altium_fixture  # noqa: E402  (committed fixture generator, stdlib only)
import ole_writer  # noqa: E402


def _frame_records(records: list[dict]) -> bytes:
    """Frame a list of field-dicts into a symbol ``Data`` stream (no HEADER)."""
    return b"".join(bytes(altium_fixture._frame(r)) for r in records)


def _res_symbol() -> list[dict]:
    return [
        {"RECORD": "1", "LibReference": "RES", "PartCount": "1"},
        {"RECORD": "34", "OwnerIndex": "0", "Text": "R?"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "1", "Name": "A",
         "Location.X": "0", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "0", "Electrical": "4", "OwnerPartId": "1"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "2", "Name": "B",
         "Location.X": "100", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "2", "Electrical": "4", "OwnerPartId": "1"},
    ]


def _cap_symbol() -> list[dict]:
    return [
        {"RECORD": "1", "LibReference": "CAP", "PartCount": "1"},
        {"RECORD": "34", "OwnerIndex": "0", "Text": "C?"},
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "1", "Name": "+",
         "Location.X": "0", "Location.Y": "0", "PinLength": "10",
         "PinConglomerate": "1", "Electrical": "7", "OwnerPartId": "1"},
    ]


def _write_lib(tmp_path: Path, extra: dict | None = None) -> Path:
    streams = {
        "FileHeader": _frame_records([{"HEADER": "Protel for Windows - Schematic Library"}]),
        "RES/Data": _frame_records(_res_symbol()),
        "CAP/Data": _frame_records(_cap_symbol()),
    }
    if extra:
        streams.update(extra)
    p = tmp_path / "parts.SchLib"
    ole_writer.write_ole(str(p), streams)
    return p


def test_all_symbols_recovered_not_collapsed(tmp_path):
    lib = altium_schlib.read(_write_lib(tmp_path))
    names = sorted(s.name for s in lib.symbols)
    # both 'Data' streams survive (bare-name keying would have kept only one).
    assert names == ["CAP", "RES"]
    assert lib.source_format == "altium"


def test_symbol_pins_and_types(tmp_path):
    lib = altium_schlib.read(_write_lib(tmp_path))
    by_name = {s.name: s for s in lib.symbols}
    res = by_name["RES"]
    assert [p.number for p in res.pins] == ["1", "2"]
    assert res.pins[0].electrical_type is PinType.PASSIVE
    cap = by_name["CAP"]
    assert cap.pins[0].electrical_type is PinType.POWER_IN


def test_per_stream_owner_index_base(tmp_path):
    # Each symbol's RECORD-1 is index 0 within its own stream; the designator
    # RECORD-34 OwnerIndex=0 resolves locally (no blind cross-stream [1:]).
    lib = altium_schlib.read(_write_lib(tmp_path))
    assert all(s.lib_id == s.name for s in lib.symbols)


def test_binary_symbol_records_refused_loudly(tmp_path):
    # craft a Data stream whose single record has a non-zero (binary) flag byte.
    payload = b"|RECORD=2|Designator=1|" + b"\x00"
    binary_frame = len(payload).to_bytes(3, "little") + b"\x01" + payload
    p = _write_lib(tmp_path, extra={"BIN/Data": binary_frame})
    with pytest.raises(AkcliError) as ei:
        altium_schlib.read(p)
    assert ei.value.code == "ALTIUM_UNSUPPORTED"


def test_export_stamps_schema_version(tmp_path):
    lib = altium_schlib.read(_write_lib(tmp_path))
    assert lib.export()["schema_version"] == "1.2"
