"""Unit tests for the design-document -> op-list generator (intentgen).

Loader shape errors, the DESIGN_UNSUPPORTED refusal catalogue, generation
behavior (ordering, grouping, multi-unit, no-connect care) and the
design-intent schema census. The full pipeline round-trip lives in
``test_from_intent_cli.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import intentgen, ops
from akcli.checks import intent as intent_mod
from akcli.errors import AkcliError
from akcli.writers.lib_cache import _coerce_sources

ROOT = Path(__file__).resolve().parents[1]
SYMS = ROOT / "tests" / "fixtures" / "kicad" / "symbols"
GENERIC = SYMS / "Generic.kicad_sym"
DEVICE = SYMS / "Device.kicad_sym"


@pytest.fixture(scope="module")
def libs():
    return _coerce_sources([str(GENERIC), str(DEVICE)])


def _design(nets, components, layout=None, **extra) -> dict:
    doc = {"protocol_version": 1, "nets": nets, "components": components}
    if layout is not None:
        doc["layout"] = layout
    doc.update(extra)
    return doc


def _load(tmp_path: Path, doc: dict) -> intentgen.Design:
    p = tmp_path / "design.json"
    p.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    return intentgen.load_design(p)


def _gen(tmp_path: Path, libs, doc: dict) -> dict:
    return intentgen.generate(_load(tmp_path, doc), libs)


def _err(tmp_path, libs, doc) -> AkcliError:
    with pytest.raises(AkcliError) as ei:
        _gen(tmp_path, libs, doc)
    return ei.value


# --------------------------------------------------------------------------- #
# loader
# --------------------------------------------------------------------------- #
def test_load_defaults(tmp_path):
    d = _load(tmp_path, _design({"A": ["R1.1"]},
                                {"R1": {"lib_id": "Device:R"}}))
    assert d.components["R1"].lib_id == "Device:R"
    assert d.components["R1"].rotation == 0
    assert d.components["R1"].mirror == "none"
    assert d.layout.origin == intentgen.DEFAULT_ORIGIN
    assert d.layout.columns is None
    assert d.layout.margin_mil == intentgen.DEFAULT_MARGIN_MIL
    assert d.layout.group is None
    assert d.layout.no_connect_unlisted is True
    assert set(d.nets) == {"A"}


def test_load_mirror_null_means_none(tmp_path):
    d = _load(tmp_path, _design(
        {"A": ["R1.1"]},
        {"R1": {"lib_id": "Device:R", "mirror": None, "rotation": 90}}))
    assert d.components["R1"].mirror == "none"
    assert d.components["R1"].rotation == 90


def test_load_origin_snaps_to_grid(tmp_path):
    d = _load(tmp_path, _design({"A": ["R1.1"]},
                                {"R1": {"lib_id": "Device:R"}},
                                layout={"origin": [1024, 987]}))
    assert d.layout.origin == (1000.0, 1000.0)


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.pop("components"), "components"),
    (lambda d: d.__setitem__("components", {}), "components"),
    (lambda d: d.__setitem__("components", {"R1": {"lib_id": "Device:R",
                                                   "colour": "red"}}),
     "unknown key"),
    (lambda d: d.__setitem__("components", {"R1": {"value": "1k"}}), "lib_id"),
    (lambda d: d.__setitem__("components", {"R1": {"lib_id": "Device:R",
                                                   "rotation": 45}}),
     "rotation"),
    (lambda d: d.__setitem__("components", {"R1": {"lib_id": "Device:R",
                                                   "mirror": "z"}}), "mirror"),
    (lambda d: d.__setitem__("components", {"R.1": {"lib_id": "Device:R"}}),
     "'.'"),
    (lambda d: d.__setitem__("layout", {"colums": 2}), "unknown key"),
    (lambda d: d.__setitem__("layout", {"columns": 0}), "columns"),
    (lambda d: d.__setitem__("layout", {"margin_mil": -1}), "margin_mil"),
    (lambda d: d.__setitem__("layout", {"group": ""}), "group"),
    (lambda d: d.__setitem__("layout", {"no_connect_unlisted": "yes"}),
     "no_connect_unlisted"),
    (lambda d: d.__setitem__("layout", {"origin": [1]}), "origin"),
])
def test_load_shape_errors_are_bad_config(tmp_path, mutate, fragment):
    doc = _design({"A": ["R1.1"]}, {"R1": {"lib_id": "Device:R"}})
    mutate(doc)
    with pytest.raises(AkcliError) as ei:
        _load(tmp_path, doc)
    assert ei.value.code == "BAD_CONFIG"
    assert fragment in ei.value.message


def test_load_protocol_mismatch(tmp_path):
    doc = _design({"A": ["R1.1"]}, {"R1": {"lib_id": "Device:R"}})
    doc["protocol_version"] = 2
    with pytest.raises(AkcliError) as ei:
        _load(tmp_path, doc)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_load_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8", newline="\n")
    with pytest.raises(AkcliError) as ei:
        intentgen.load_design(p)
    assert ei.value.code == "BAD_CONFIG"


def test_design_file_still_loads_as_intent(tmp_path):
    """The superset promise: the SAME file is a valid `check --intent` input."""
    p = tmp_path / "design.json"
    p.write_text(json.dumps(_design(
        {"A": ["R1.1", "R2.1"]},
        {"R1": {"lib_id": "Device:R"}, "R2": {"lib_id": "Device:R"}},
        layout={"columns": 2})), encoding="utf-8", newline="\n")
    spec = intent_mod.load(p)          # must not raise on components/layout
    assert set(spec.nets) == {"A"}


# --------------------------------------------------------------------------- #
# DESIGN_UNSUPPORTED refusals (one test per documented refusal)
# --------------------------------------------------------------------------- #
def test_refuse_pin_addressed_by_name(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["U1.IN"]},
                                     {"U1": {"lib_id": "Generic:REG3"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    # the message must hand over the correct NUMBER for the named pin
    assert "U1.3" in e.message and "NAME" in e.message


def test_refuse_unknown_pin_number(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["U1.9"]},
                                     {"U1": {"lib_id": "Generic:REG3"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "valid pins" in e.message


def test_refuse_wildcard_member(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["R*.1"]},
                                     {"R1": {"lib_id": "Device:R"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "wildcard" in e.message


def test_refuse_ref_missing_from_components(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["R9.1"]},
                                     {"R1": {"lib_id": "Device:R"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "R9" in e.message and "components" in e.message


def test_refuse_pin_in_two_nets(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["R1.1"], "B": ["R1.1"]},
                                     {"R1": {"lib_id": "Device:R"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "two nets" in e.message


def test_refuse_coincident_pins_on_different_nets(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["U1.1"], "B": ["U1.2"]},
                                     {"U1": {"lib_id": "Generic:STACK2"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "coincident" in e.message


@pytest.mark.parametrize("name", ["D[0..3]", "K[3..0]", "BUS{A B}"])
def test_refuse_bus_syntax_net_names(tmp_path, libs, name):
    e = _err(tmp_path, libs, _design({name: ["R1.1"]},
                                     {"R1": {"lib_id": "Device:R"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "bus syntax" in e.message


def test_refuse_multiunit_with_common_pins(tmp_path, libs):
    e = _err(tmp_path, libs, _design(
        {"A": ["U1.1"]}, {"U1": {"lib_id": "Generic:DUAL_BUF_PWR"}}))
    assert e.code == "DESIGN_UNSUPPORTED"
    assert "unit-0" in e.message


def test_unknown_lib_id_reuses_symbol_not_found(tmp_path, libs):
    e = _err(tmp_path, libs, _design({"A": ["U1.1"]},
                                     {"U1": {"lib_id": "No:Such"}}))
    assert e.code == "SYMBOL_NOT_FOUND"


# --------------------------------------------------------------------------- #
# generation behavior
# --------------------------------------------------------------------------- #
_NETS = {
    "RAIL_A": ["U1.2", "C1.1", "J1.1"],
    "RAIL_IN": ["U1.3", "J1.2"],
    "GND": ["U1.1", "C1.2", "J1.4", "U2.1"],
    "SIG_N": ["U2.2", "J1.3"],
}
_COMPONENTS = {
    "U1": {"lib_id": "Generic:REG3", "value": "REG-ADJ"},
    "C1": {"lib_id": "Device:C", "value": "10u",
           "footprint": "Cap:0402"},
    "J1": {"lib_id": "Generic:CONN_1x4"},
    "U2": {"lib_id": "Generic:DUAL_BUF"},
}


def test_generate_structure_and_order(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(_NETS, _COMPONENTS))
    assert doc["protocol_version"] == ops.PROTOCOL_VERSION
    assert doc["target_format"] == "kicad"
    kinds = [o["op"] for o in doc["ops"]]
    # places first (sorted refs; U2 twice = its two units), then labels
    # (sorted by net then member), then no-connects
    n_place = kinds.count("place_component")
    n_label = kinds.count("add_net_label")
    assert kinds == (["place_component"] * n_place
                     + ["add_net_label"] * n_label
                     + ["add_no_connect"] * kinds.count("add_no_connect"))
    places = [o for o in doc["ops"] if o["op"] == "place_component"]
    assert [(p["designator"], p.get("unit")) for p in places] == [
        ("C1", None), ("J1", None), ("U1", None), ("U2", 1), ("U2", 2)]
    labels = [(o["name"], o["at"]) for o in doc["ops"]
              if o["op"] == "add_net_label"]
    assert labels == sorted(labels)
    assert ("GND", "U2.1") in labels
    ncs = [o["pin"] for o in doc["ops"] if o["op"] == "add_no_connect"]
    assert ncs == ["U2.3", "U2.4"]      # the unreferenced second unit
    # dressing propagates
    c1 = next(p for p in places if p["designator"] == "C1")
    assert c1["value"] == "10u" and c1["footprint"] == "Cap:0402"


def test_generate_is_structurally_valid(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(_NETS, _COMPONENTS))
    prep = ops.prepare_oplist(doc)
    prepared, errs = prep.doc, prep.errors
    assert prep.ok, errs
    assert prepared["ops"]


def test_generate_deterministic_bytes_under_key_reorder(tmp_path, libs):
    a = _gen(tmp_path, libs, _design(_NETS, _COMPONENTS))
    nets_r = dict(reversed(list(_NETS.items())))
    comps_r = dict(reversed(list(_COMPONENTS.items())))
    b = _gen(tmp_path, libs, _design(nets_r, comps_r))
    assert json.dumps(a, indent=2) == json.dumps(b, indent=2)


def test_generate_rotation_mirror_and_grid(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(
        {"A": ["R1.1", "R2.1"]},
        {"R1": {"lib_id": "Device:R", "rotation": 90},
         "R2": {"lib_id": "Device:R", "mirror": "x"}}))
    r1, r2 = (o for o in doc["ops"] if o["op"] == "place_component")
    assert r1["rotation"] == 90 and "mirror" not in r1
    assert r2["mirror"] == "x" and "rotation" not in r2
    for p in (r1, r2):
        assert p["x_mil"] % 50 == 0 and p["y_mil"] % 50 == 0


def test_generate_grouped(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(
        {"A": ["R1.1", "R2.1"]},
        {"R1": {"lib_id": "Device:R"}, "R2": {"lib_id": "Device:R"}},
        layout={"group": "BLOCK", "origin": [2000, 3000]}))
    assert doc["groups"] == {"BLOCK": {"origin": [2000, 3000]}}
    assert all(o.get("group") == "BLOCK" for o in doc["ops"])
    prep = ops.prepare_oplist(doc)
    prepared, errs = prep.doc, prep.errors
    assert prep.ok, errs
    # group-local -> absolute translation happened
    px = [o["x_mil"] for o in prepared["ops"]
          if o.get("op") == "place_component"]
    assert all(x >= 2000 for x in px)


def test_generate_columns_wrap(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(
        {"A": ["R1.1", "R2.1", "R3.1"]},
        {"R1": {"lib_id": "Device:R"}, "R2": {"lib_id": "Device:R"},
         "R3": {"lib_id": "Device:R"}},
        layout={"columns": 2}))
    places = [o for o in doc["ops"] if o["op"] == "place_component"]
    # 2 columns: R1/R2 share a row, R3 wraps below R1
    assert places[0]["y_mil"] == places[1]["y_mil"]
    assert places[2]["x_mil"] == places[0]["x_mil"]
    assert places[2]["y_mil"] > places[0]["y_mil"]


def test_generate_no_connect_opt_out(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(
        {"A": ["J1.1"]}, {"J1": {"lib_id": "Generic:CONN_1x4"}},
        layout={"no_connect_unlisted": False}))
    assert not any(o["op"] == "add_no_connect" for o in doc["ops"])


def test_generate_unreferenced_component_placed_all_nc(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design(
        {"A": ["R1.1", "R1.2"]},
        {"R1": {"lib_id": "Device:R"}, "C9": {"lib_id": "Device:C"}}))
    assert any(o["op"] == "place_component" and o["designator"] == "C9"
               for o in doc["ops"])
    assert [o["pin"] for o in doc["ops"] if o["op"] == "add_no_connect"] \
        == ["C9.1", "C9.2"]


def test_generate_coincident_stack_no_connect_care(tmp_path, libs):
    # both stacked pins on ONE net: legal, and no NC anywhere
    doc = _gen(tmp_path, libs, _design(
        {"GND": ["U1.1", "U1.2"], "V": ["U1.3"]},
        {"U1": {"lib_id": "Generic:STACK2"}}))
    assert not any(o["op"] == "add_no_connect" for o in doc["ops"])
    # one stacked pin netted, its mate unlisted: the mate is electrically
    # that net already — an NC marker would contradict the live connection
    doc = _gen(tmp_path, libs, _design(
        {"GND": ["U1.1"], "V": ["U1.3"]},
        {"U1": {"lib_id": "Generic:STACK2"}}))
    assert not any(o["op"] == "add_no_connect" for o in doc["ops"])


def test_generate_provenance_annotation_is_tolerated(tmp_path, libs):
    doc = _gen(tmp_path, libs, _design({"A": ["R1.1"]},
                                       {"R1": {"lib_id": "Device:R"}}))
    assert doc["_generator"] == "akcli ops from-intent"
    assert ops.validate_oplist(doc) == []


# --------------------------------------------------------------------------- #
# schema census — loader keys <-> design-intent.schema.json, both directions
# --------------------------------------------------------------------------- #
def _schema() -> dict:
    return json.loads((ROOT / "schemas" / "design-intent.schema.json")
                      .read_text(encoding="utf-8"))


def test_schema_top_keys_census():
    schema = _schema()
    assert set(schema["properties"]) == set(intentgen.TOP_KEYS)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"protocol_version", "nets",
                                       "components"}


def test_schema_component_keys_census():
    comp = _schema()["properties"]["components"]["additionalProperties"]
    assert set(comp["properties"]) == set(intentgen.COMPONENT_KEYS)
    assert comp["additionalProperties"] is False
    assert comp["required"] == ["lib_id"]
    assert set(comp["properties"]["rotation"]["enum"]) == {0, 90, 180, 270}


def test_schema_layout_keys_census():
    lay = _schema()["properties"]["layout"]
    assert set(lay["properties"]) == set(intentgen.LAYOUT_KEYS)
    assert lay["additionalProperties"] is False


def test_schema_accepts_generated_example(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    doc = _design(_NETS, _COMPONENTS,
                  layout={"origin": [1000, 1000], "columns": 3,
                          "margin_mil": 100, "no_connect_unlisted": True})
    jsonschema.Draft202012Validator(_schema()).validate(doc)


def test_schema_rejects_unknown_top_key():
    jsonschema = pytest.importorskip("jsonschema")
    doc = _design({"A": ["R1.1"]}, {"R1": {"lib_id": "Device:R"}},
                  extra_key=1)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(doc)
