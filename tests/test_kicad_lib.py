"""Tests for :mod:`akcli.readers.kicad_lib` (SPEC §3.4).

Covers ``.kicad_sym`` parsing, pin-type / local-coordinate extraction, the
``C_Polarized`` ``(extends "C")`` inheritance case, ``resolve`` qualified vs
unqualified matching, and ``body_sexpr`` preservation for the future writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import model, units
from akcli.errors import AkcliError
from akcli.readers import kicad_lib, sexpr

FIX = Path(__file__).parent / "fixtures" / "kicad"
DEVICE = FIX / "symbols" / "Device.kicad_sym"
POWER = FIX / "symbols" / "power.kicad_sym"


def _by_name(lib: model.Library, name: str) -> model.SymbolDef:
    return next(s for s in lib.symbols if s.name == name)


# --------------------------------------------------------------------------- #
# read() / parsing
# --------------------------------------------------------------------------- #
def test_read_device_library_symbols():
    lib = kicad_lib.read(DEVICE)
    assert lib.source_format == "kicad"
    assert lib.schema_version == model.SCHEMA_VERSION
    names = {s.name for s in lib.symbols}
    assert {"R", "C", "L", "C_Polarized"} <= names


def test_r_pins_have_passive_type_and_local_coords():
    lib = kicad_lib.read(DEVICE)
    r = _by_name(lib, "R")
    assert len(r.pins) == 2
    assert all(p.electrical_type is model.PinType.PASSIVE for p in r.pins)
    by_num = {p.number: p for p in r.pins}
    assert set(by_num) == {"1", "2"}
    # local coords are mils (+Y up, symbol-local). pin1 at (0, 3.81 mm) -> 150 mil.
    assert by_num["1"].x_mil == pytest.approx(0.0)
    assert by_num["1"].y_mil == pytest.approx(units.nm_to_mil(units.mm_to_nm(3.81)))
    assert by_num["2"].y_mil == pytest.approx(units.nm_to_mil(units.mm_to_nm(-3.81)))


def test_body_sexpr_preserved_as_snode():
    lib = kicad_lib.read(DEVICE)
    r = _by_name(lib, "R")
    assert isinstance(r.body_sexpr, sexpr.SNode)
    assert r.body_sexpr.tag == "symbol"
    # the raw node round-trips: it still names the symbol it came from.
    assert r.body_sexpr[1].value == "R"


def test_part_count_is_one_for_single_unit_parts():
    lib = kicad_lib.read(DEVICE)
    assert _by_name(lib, "R").part_count == 1
    assert _by_name(lib, "C").part_count == 1


def test_power_library_single_power_in_pin():
    lib = kicad_lib.read(POWER)
    assert {s.name for s in lib.symbols} == {"GND", "+3V3"}
    for name in ("GND", "+3V3"):
        sym = _by_name(lib, name)
        assert len(sym.pins) == 1
        assert sym.pins[0].electrical_type is model.PinType.POWER_IN


def test_not_a_symbol_lib_raises():
    with pytest.raises(AkcliError) as ei:
        kicad_lib.read(FIX / "board_v8.kicad_sch")
    assert ei.value.code == "ALTIUM_MALFORMED"


# --------------------------------------------------------------------------- #
# extends resolution (the C_Polarized case)
# --------------------------------------------------------------------------- #
def test_c_polarized_declares_extends_and_no_own_pins():
    lib = kicad_lib.read(DEVICE)
    cpol = _by_name(lib, "C_Polarized")
    assert cpol.extends == "C"
    # C_Polarized adds only graphics; it declares none of its own pins.
    assert cpol.pins == []


def test_resolve_c_polarized_inherits_pins_from_base():
    lib = kicad_lib.read(DEVICE)
    resolved = kicad_lib.resolve("C_Polarized", [lib])
    assert resolved.name == "C_Polarized"
    assert resolved.extends == "C"
    base = _by_name(lib, "C")
    # inherits exactly the base C pins (numbers + electrical types).
    assert [(p.number, p.electrical_type) for p in resolved.pins] == [
        (p.number, p.electrical_type) for p in base.pins
    ]
    assert len(resolved.pins) == 2


def test_resolve_plain_symbol_returns_own_pins():
    lib = kicad_lib.read(DEVICE)
    resolved = kicad_lib.resolve("R", [lib])
    assert len(resolved.pins) == 2
    assert resolved.body_sexpr is not None


def test_resolve_qualified_against_unqualified_library():
    # a qualified request (Device:R) resolves against a bare-named standalone lib.
    lib = kicad_lib.read(DEVICE)
    resolved = kicad_lib.resolve("Device:R", [lib])
    assert resolved.name == "R"
    assert len(resolved.pins) == 2


def test_resolve_missing_symbol_raises():
    lib = kicad_lib.read(DEVICE)
    with pytest.raises(AkcliError) as ei:
        kicad_lib.resolve("Device:DoesNotExist", [lib])
    assert ei.value.code == "SYMBOL_NOT_FOUND"


# --------------------------------------------------------------------------- #
# inline lib_symbols + helpers
# --------------------------------------------------------------------------- #
def test_symbols_from_inline_lib_symbols():
    text = (FIX / "board_v8.kicad_sch").read_text()
    root = sexpr.parse(text)
    libsym = root.find("lib_symbols")
    syms = kicad_lib.symbols_from_lib_symbols(libsym)
    names = {s.name for s in syms}
    assert {"Device:R", "Device:C", "power:+3V3", "power:GND"} <= names
    # extends is None for these direct symbols; pins resolved with types.
    devr = next(s for s in syms if s.name == "Device:R")
    assert len(devr.pins) == 2
    assert all(p.electrical_type is model.PinType.PASSIVE for p in devr.pins)


def test_resolve_against_inline_cache_qualified():
    root = sexpr.parse((FIX / "board_v8.kicad_sch").read_text())
    lib = kicad_lib.library_from_lib_symbols(root.find("lib_symbols"))
    gnd = kicad_lib.resolve("power:GND", [lib])
    assert len(gnd.pins) == 1
    assert gnd.pins[0].electrical_type is model.PinType.POWER_IN


def test_pin_offsets_returns_local_tuples():
    lib = kicad_lib.read(DEVICE)
    offs = kicad_lib.pin_offsets(_by_name(lib, "R"))
    assert sorted(n for n, _, _ in offs) == ["1", "2"]
    d = {n: (x, y) for n, x, y in offs}
    assert d["1"][0] == pytest.approx(0.0)
    assert d["1"][1] == pytest.approx(units.nm_to_mil(units.mm_to_nm(3.81)))


# --------------------------------------------------------------------------- #
# multi-unit + alternate (DeMorgan) body styles — 74xx-style symbols
# --------------------------------------------------------------------------- #
_TWO_STYLE_LIB = """
(kicad_symbol_lib (version 20231120) (generator "test")
  (symbol "G" (pin_numbers (hide yes)) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (symbol "G_1_1"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "A" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "Y" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))
    (symbol "G_1_2"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "A" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "Y" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))))
    (symbol "G_2_1"
      (pin power_in line (at 0 -7.62 90) (length 2.54)
        (name "GND" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27))))))))
"""


def test_alternate_body_style_pins_not_duplicated(tmp_path):
    """A ``_<unit>_2`` (DeMorgan) body re-draws the SAME pins: collecting it
    duplicated every pin number, and the writer's per-pin UUIDs (keyed by pin
    number) then collided — placing any 74xx part was refused DUPLICATE_UUID."""
    p = tmp_path / "G.kicad_sym"
    p.write_text(_TWO_STYLE_LIB)
    lib = kicad_lib.read(p)
    g = _by_name(lib, "G")
    assert sorted(pin.number for pin in g.pins) == ["1", "2", "3"]


def test_collected_pins_carry_their_unit(tmp_path):
    p = tmp_path / "G.kicad_sym"
    p.write_text(_TWO_STYLE_LIB)
    lib = kicad_lib.read(p)
    g = _by_name(lib, "G")
    units_of = {pin.number: pin.owner_part_id for pin in g.pins}
    assert units_of == {"1": 1, "2": 1, "3": 2}
