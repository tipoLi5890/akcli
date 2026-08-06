"""`schemas/symbol-pins.schema.json` must agree with the loader that enforces it.

akcli validates the pin table with a hand-rolled zero-dependency loader
(`symbol_spec.parse_spec`); the published schema is what agents and editors read.
Two copies of one contract drift, so this census asserts they cannot: the key
sets, the enums and the required fields are compared directly, and the fixture
specs are validated by both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import symbol_spec as ss
from akcli.errors import AkcliError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "symbol-pins.schema.json"
MIRROR_PATH = ROOT / "src" / "akcli" / "schemas" / "symbol-pins.schema.json"
FIX = ROOT / "tests" / "fixtures" / "kicad" / "symlib"
SPECS = sorted(FIX.glob("*.spec.json"))

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
_PIN_SCHEMA = SCHEMA["properties"]["pins"]["items"]


# --------------------------------------------------------------------------- #
# loader <-> schema agreement
# --------------------------------------------------------------------------- #
def test_top_level_keys_match_the_loader():
    assert set(SCHEMA["properties"]) == set(ss._TOP_KEYS)
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == {"protocol_version", "name", "pins"}


def test_pin_keys_match_the_loader():
    assert set(_PIN_SCHEMA["properties"]) == set(ss._PIN_KEYS)
    assert _PIN_SCHEMA["additionalProperties"] is False
    assert set(_PIN_SCHEMA["required"]) == {"number", "name", "type", "side"}


def test_body_keys_match_the_loader():
    body = SCHEMA["properties"]["body"]
    assert set(body["properties"]) == set(ss._BODY_KEYS)
    assert body["additionalProperties"] is False


def test_type_enum_is_exactly_the_accepted_vocabulary():
    assert _PIN_SCHEMA["properties"]["type"]["enum"] == sorted(ss.PIN_TYPES)


def test_side_enum_matches_the_module_constant():
    assert _PIN_SCHEMA["properties"]["side"]["enum"] == list(ss.SIDES)


def test_protocol_version_const_tracks_the_module():
    assert SCHEMA["properties"]["protocol_version"]["const"] == ss.PROTOCOL_VERSION


def test_reference_prefix_default_matches_the_loader():
    default = SCHEMA["properties"]["reference_prefix"]["default"]
    spec = ss.parse_spec({"protocol_version": 1, "name": "X", "pins": [
        {"number": "1", "name": "A", "type": "passive", "side": "left"}]})
    assert spec.reference_prefix == default


def test_packaged_mirror_is_byte_identical():
    assert MIRROR_PATH.read_bytes() == SCHEMA_PATH.read_bytes(), (
        "run `python3 tools/sync_schemas.py`")


# --------------------------------------------------------------------------- #
# both validators accept the fixtures, and both reject the same rot
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_file", SPECS, ids=lambda p: p.name)
def test_fixture_specs_validate_against_the_schema(spec_file):
    jsonschema = pytest.importorskip("jsonschema")
    doc = json.loads(spec_file.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(SCHEMA)
    errors = ["/".join(map(str, e.path)) + ": " + e.message
              for e in validator.iter_errors(doc)]
    assert errors == [], f"{spec_file.name} drifted from the schema: {errors}"
    ss.parse_spec(doc, str(spec_file))          # and the loader agrees


@pytest.mark.parametrize("mutation", [
    {"colour": "red"},                                    # unknown top-level key
    {"protocol_version": 2},                              # wrong version
    {"pins": []},                                         # empty pin table
])
def test_schema_and_loader_reject_the_same_documents(mutation):
    jsonschema = pytest.importorskip("jsonschema")
    doc = json.loads(SPECS[0].read_text(encoding="utf-8"))
    doc.update(mutation)
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert list(validator.iter_errors(doc)), f"schema accepted {mutation}"
    with pytest.raises(AkcliError):
        ss.parse_spec(doc, "<test>")


@pytest.mark.parametrize("bad_pin", [
    {"number": "1", "name": "A", "type": "analog", "side": "left"},   # bad type
    {"number": "1", "name": "A", "type": "input", "side": "north"},   # bad side
    {"number": "1", "name": "A", "type": "input"},                    # no side
    {"number": "1", "name": "A", "type": "input", "side": "left",
     "unit": 2},                                                      # multi-unit
])
def test_schema_and_loader_reject_the_same_pins(bad_pin):
    jsonschema = pytest.importorskip("jsonschema")
    doc = {"protocol_version": 1, "name": "X", "pins": [bad_pin]}
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert list(validator.iter_errors(doc)), f"schema accepted {bad_pin}"
    with pytest.raises(AkcliError):
        ss.parse_spec(doc, "<test>")
