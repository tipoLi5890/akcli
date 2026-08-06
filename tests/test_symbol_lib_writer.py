"""``writers/symbol_lib`` — the generated ``.kicad_sym`` must read back as itself.

The renderer's contract is arbitrated by akcli's OWN reader
(`readers/kicad_lib.py`): whatever the writer emits, the reader must recover the
same pin numbers, names, electrical types, positions and orientations. That
closes the loop the hand-authored-symbol incident opened — a coordinate typo can
no longer hide between "what I meant to write" and "what the file says".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.model import KICAD_PINTYPE
from akcli.readers import kicad_lib
from akcli.writers import symbol_lib

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "symlib"
SPECS = ["generic_opamp_8.spec.json", "generic_logic_14.spec.json"]


def _render(spec_file: str) -> tuple[ss.Layout, str]:
    lay = ss.layout(ss.load(FIX / spec_file))
    return lay, symbol_lib.to_kicad_sym(lay)


def _read_back(text: str, tmp_path: Path, name: str = "out.kicad_sym"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    lib = kicad_lib.read(path)
    assert len(lib.symbols) == 1
    return lib.symbols[0]


# --------------------------------------------------------------------------- #
# round trip through akcli's own reader
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_file", SPECS)
def test_round_trip_preserves_every_pin(spec_file, tmp_path):
    lay, text = _render(spec_file)
    sym = _read_back(text, tmp_path)

    assert sym.name == lay.name
    assert sym.part_count == 1
    got = {p.number: p for p in sym.pins}
    assert set(got) == {p.spec.number for p in lay.pins}
    for placed in lay.pins:
        pin = got[placed.spec.number]
        assert pin.name == placed.spec.name
        assert pin.electrical_type is KICAD_PINTYPE[placed.spec.type]
        assert (round(pin.x_mil), round(pin.y_mil)) == placed.at
        assert pin.orientation == placed.orientation


@pytest.mark.parametrize("spec_file", SPECS)
def test_round_trip_preserves_the_body_rectangle(spec_file, tmp_path):
    lay, text = _render(spec_file)
    sym = _read_back(text, tmp_path)
    extent = kicad_lib.body_extent_mil(sym)
    assert extent is not None
    assert tuple(round(v) for v in extent) == lay.body


def test_every_electrical_type_token_survives_the_round_trip(tmp_path):
    pins = [{"number": str(i + 1), "name": f"P{i + 1}", "type": token,
             "side": "left" if i % 2 == 0 else "right"}
            for i, token in enumerate(ss.PIN_TYPES)]
    lay = ss.layout(ss.parse_spec(
        {"protocol_version": 1, "name": "GENERIC_TYPES", "pins": pins}))
    sym = _read_back(symbol_lib.to_kicad_sym(lay), tmp_path)
    by_number = {p.number: p for p in sym.pins}
    for i, token in enumerate(ss.PIN_TYPES):
        assert by_number[str(i + 1)].electrical_type is KICAD_PINTYPE[token]


def test_free_and_unspecified_are_emitted_verbatim(tmp_path):
    """Both fold onto PinType.UNSPECIFIED in the model; the FILE keeps them apart."""
    lay = ss.layout(ss.parse_spec({"protocol_version": 1, "name": "GENERIC_FU",
                                   "pins": [
                                       {"number": "1", "name": "A", "type": "free",
                                        "side": "left"},
                                       {"number": "2", "name": "B",
                                        "type": "unspecified", "side": "right"}]}))
    text = symbol_lib.to_kicad_sym(lay)
    assert "(pin free line" in text
    assert "(pin unspecified line" in text


def test_hidden_pins_carry_the_kicad_8_hide_form(tmp_path):
    lay = ss.layout(ss.parse_spec({"protocol_version": 1, "name": "GENERIC_HIDE",
                                   "pins": [
                                       {"number": "1", "name": "VCC",
                                        "type": "power_in", "side": "top"},
                                       {"number": "2", "name": "VCC",
                                        "type": "power_in", "side": "top",
                                        "stack_with": "1", "hidden": True}]}))
    text = symbol_lib.to_kicad_sym(lay)
    # `(hide yes)` directly after `(length ...)` — the kicad-cli-verified pin
    # form, distinct from the property form nested inside `(effects ...)`.
    assert text.count("(length 2.54)\n\t\t\t\t(hide yes)\n") == 1
    # still readable, and the hidden pin keeps its identity
    sym = _read_back(text, tmp_path)
    assert {p.number for p in sym.pins} == {"1", "2"}


def test_unnamed_pin_becomes_kicads_tilde(tmp_path):
    lay = ss.layout(ss.parse_spec({"protocol_version": 1, "name": "GENERIC_NONAME",
                                   "pins": [{"number": "1", "name": "",
                                             "type": "passive", "side": "left"}]}))
    assert '(name "~"' in symbol_lib.to_kicad_sym(lay)


# --------------------------------------------------------------------------- #
# determinism + shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_file", SPECS)
def test_identical_input_yields_identical_bytes(spec_file):
    doc = json.loads((FIX / spec_file).read_text(encoding="utf-8"))
    first = symbol_lib.to_kicad_sym(ss.layout(ss.parse_spec(doc)))
    second = symbol_lib.to_kicad_sym(ss.layout(ss.parse_spec(
        json.loads(json.dumps(doc)))))
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")


def test_header_pins_the_verified_format_tokens():
    _, text = _render("generic_opamp_8.spec.json")
    assert text.startswith("(kicad_symbol_lib\n")
    assert f"\t(version {symbol_lib.FORMAT_VERSION})\n" in text
    assert symbol_lib.FORMAT_VERSION == "20231120"   # KiCad 8 token; see fixtures README
    assert '\t(generator "akcli")\n' in text
    assert text.endswith(")\n")


def test_required_property_block_is_complete():
    _, text = _render("generic_logic_14.spec.json")
    for prop in ("Reference", "Value", "Footprint", "Datasheet"):
        assert f'(property "{prop}" ' in text
    assert '(property "Reference" "U"' in text


def test_symbol_name_is_kicad_escaped(tmp_path):
    lay = ss.layout(ss.parse_spec({"protocol_version": 1, "name": "A/B",
                                   "pins": [{"number": "1", "name": "P",
                                             "type": "passive", "side": "left"}]}))
    text = symbol_lib.to_kicad_sym(lay)
    assert '"A{slash}B"' in text
    # the reader unescapes on the way back in, so the model sees the real name
    assert _read_back(text, tmp_path).name == "A/B"


def test_output_uses_unix_newlines_only():
    _, text = _render("generic_opamp_8.spec.json")
    assert "\r" not in text
