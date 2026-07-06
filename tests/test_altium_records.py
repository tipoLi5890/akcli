"""Tests for Altium record framing + tokenizer (readers/altium_records.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.model import PinType
from altium_kicad_cli.readers import _cfbf
from altium_kicad_cli.readers.altium_records import (
    RECORD_COMPONENT,
    RECORD_NET_LABEL,
    RECORD_PIN,
    RECORD_WIRE,
    RECORDS,
    coord,
    fields,
    gi,
    parse_records,
    pin_electrical_type,
)

FIX = Path(__file__).resolve().parent / "fixtures"


def _fileheader(name: str) -> bytes:
    return _cfbf.read_streams(FIX / f"{name}.SchDoc")["FileHeader"]


# --- record framing ----------------------------------------------------------
def test_demo_record_count_and_ids():
    recs = parse_records(_fileheader("ole_minifat"), drop_header=True)
    # ole_minifat.records.txt: 8 post-header records (indices 0..7).
    assert len(recs) == 8
    assert gi(recs[0], "RECORD") == RECORD_COMPONENT
    assert gi(recs[2], "RECORD") == RECORD_PIN
    assert gi(recs[6], "RECORD") == RECORD_WIRE
    assert gi(recs[7], "RECORD") == RECORD_NET_LABEL


def test_header_detection_drops_only_the_header():
    fh = _fileheader("ole_minifat")
    with_header = parse_records(fh, drop_header=False)
    without = parse_records(fh, drop_header=True)
    # The leading record is the schematic HEADER and only that one is dropped.
    assert "HEADER" in with_header[0]
    assert "RECORD" not in with_header[0]
    assert len(with_header) == len(without) + 1
    assert without == with_header[1:]


def test_drop_header_is_not_a_blind_slice():
    # A buffer whose first record is NOT a HEADER keeps every record.
    buf = _frame({"RECORD": "1", "Location.X": "10"}) + _frame(
        {"RECORD": "34", "OwnerIndex": "0", "Text": "U1"}
    )
    recs = parse_records(buf, drop_header=True)
    assert len(recs) == 2
    assert recs[0]["RECORD"] == "1"


def test_pin_fields_and_designators():
    recs = parse_records(_fileheader("ole_minifat"), drop_header=True)
    pin = recs[2]
    assert pin["Designator"] == "1"
    assert pin["Name"] == "P0.25"
    assert gi(pin, "PinLength") == 10
    assert gi(pin, "OwnerIndex") == 0


# --- tokenizer / fields ------------------------------------------------------
def test_fields_basic_tokenizing():
    d = fields("|RECORD=2|Designator=1|Name=VDD|")
    assert d == {"RECORD": "2", "Designator": "1", "Name": "VDD"}


def test_fields_value_containing_equals_sign():
    d = fields("|RECORD=41|Text=R=10k|")
    assert d["Text"] == "R=10k"


def test_fields_utf8_twin_overrides_latin1():
    # Altium stores a CJK value twice: a latin1 twin and a %UTF8% twin whose bytes
    # were latin1-mangled by the framing. The %UTF8% twin must win and decode clean.
    original = "電阻Ω"
    utf8_bytes = original.encode("utf-8")
    mangled = utf8_bytes.decode("latin-1")  # what framing's latin1 decode produced
    rec = f"|RECORD=41|Comment=??|%UTF8%Comment={mangled}|"
    d = fields(rec)
    assert d["Comment"] == original


# --- gi / coord --------------------------------------------------------------
def test_gi_handles_missing_and_non_numeric():
    d = {"A": "12", "B": "-3", "C": "x", "D": ""}
    assert gi(d, "A") == 12
    assert gi(d, "B") == -3
    assert gi(d, "C") is None
    assert gi(d, "C", 0) == 0
    assert gi(d, "missing", 7) == 7


def test_coord_assembles_int_and_frac_to_mil():
    # 1 Altium SCH unit = 10 mil; _Frac is 1/100000 of a unit.
    assert coord({"Location.X": "100"}, "Location.X") == pytest.approx(1000.0)
    # 100 units + 50000/100000 unit = 100.5 units -> 1005 mil.
    d = {"Location.X": "100", "Location.X_Frac": "50000"}
    assert coord(d, "Location.X") == pytest.approx(1005.0)


def test_coord_default_zero_when_absent():
    assert coord({}, "Location.Y") == 0.0


# --- electrical -> PinType ---------------------------------------------------
def test_pin_electrical_type_map():
    assert pin_electrical_type({"Electrical": "0"}) is PinType.INPUT
    assert pin_electrical_type({"Electrical": "4"}) is PinType.PASSIVE
    assert pin_electrical_type({"Electrical": "7"}) is PinType.POWER_IN
    # default for a pin without an Electrical field is PASSIVE (real boards).
    assert pin_electrical_type({}) is PinType.PASSIVE


def test_records_constants_table():
    assert RECORDS[RECORD_COMPONENT] == "Component"
    assert RECORDS[RECORD_PIN] == "Pin"
    assert RECORDS[29] == "Junction"


# --- robustness --------------------------------------------------------------
def test_truncated_final_record_does_not_crash():
    good = _frame({"RECORD": "1", "Location.X": "10"})
    # declare a length far beyond the buffer for the final record
    truncated = good + bytes([0xFF, 0xFF, 0x00, 0x00]) + b"|RECORD=2|Name=A"
    recs = parse_records(truncated, drop_header=False)
    assert recs[0]["RECORD"] == "1"
    # the truncated tail is sliced safely (no IndexError / hang)
    assert isinstance(recs[-1], dict)


def test_empty_buffer_yields_no_records():
    assert parse_records(b"", drop_header=True) == []


def test_record_cap_enforced(monkeypatch):
    from altium_kicad_cli import safety

    monkeypatch.setattr(safety, "MAX_RECORDS", 3)
    buf = b"".join(_frame({"RECORD": str(i)}) for i in range(10))
    with pytest.raises(AkcliError) as ei:
        parse_records(buf, drop_header=False)
    assert ei.value.code == "ALTIUM_ALLOC_GUARD"


# --- helper: mirror the generator's record framing --------------------------
def _frame(fields_dict: dict) -> bytes:
    payload = "".join(f"|{k}={v}" for k, v in fields_dict.items()) + "|"
    pb = payload.encode("latin-1", "replace") + b"\x00"
    return len(pb).to_bytes(3, "little") + b"\x00" + pb


# ---------------------------------------------------------------------------
# twin-less plain-field decode strategy (strict UTF-8 upgrade, latin1 fallback)
# ---------------------------------------------------------------------------
def test_fields_twinless_high_bytes_keep_latin1_byte_preserving():
    # Twin-less fields must NOT be codec-guessed: two-byte legacy code-page
    # sequences (GBK \xd6\xb5 = one CJK char) are frequently *valid* UTF-8,
    # so a "strict-UTF-8 upgrade" would silently misdecode them. The latin1
    # framing decode is kept and the original bytes stay recoverable.
    raw = b"\xd6\xb5"  # GBK for one CJK char; ALSO valid UTF-8 (U+05B5)
    mangled = raw.decode("latin-1")
    d = fields(f"|RECORD=41|Text={mangled}|Name=Foo|")
    assert d["Text"] == mangled
    assert d["Text"].encode("latin-1") == raw  # bytes recoverable


def test_fields_export_corrupted_fffd_is_faithful():
    # Upstream tool destroyed "6.65kΩ" at export: BOTH twins carry the U+FFFD
    # replacement bytes (EF BF BD) inside the file. No codec can recover it;
    # the decode must be faithful (U+FFFD), not silently guessed.
    stored = "6.65k" + "��"
    mangled = stored.encode("utf-8").decode("latin-1")
    d = fields(f"|RECORD=41|%UTF8%Text={mangled}|Text={mangled}|Name=Value|")
    assert d["Text"] == stored
