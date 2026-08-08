"""Tests for the op-list authoring kit (`akcli ops list|template`).

Includes the drift guards: the in-code required/optional tables, the per-field
KIND table and the enum frozensets must all match ``schemas/ops.schema.json``
so `ops template` never teaches a stale shape and the zero-dependency
validator never accepts what jsonschema rejects (or vice versa).
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli, ops

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "schemas" / "ops.schema.json")
    .read_text(encoding="utf-8")
)


def _schema_ops() -> dict[str, dict]:
    """{op name: its schema branch} from the anyOf/oneOf op union."""
    out = {}
    stack = [SCHEMA]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            props = node.get("properties", {})
            op_const = props.get("op", {}).get("const")
            if op_const:
                out[op_const] = node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def test_tables_match_schema():
    branches = _schema_ops()
    # every op AND macro must have a schema branch, and vice versa
    assert set(branches) == set(ops.OP_NAMES) | set(ops.MACRO_OPS), (
        "schema op union drifted from OP_NAMES | MACRO_OPS"
    )
    for name, branch in branches.items():
        required = [f for f in branch.get("required", []) if f != "op"]
        if name in ops.MACRO_OPS:
            req_table, opt_table = ops.MACRO_REQUIRED, ops.MACRO_OPTIONAL
        else:
            req_table, opt_table = ops._OP_REQUIRED, ops._OP_OPTIONAL
        assert sorted(req_table.get(name, [])) == sorted(required), (
            f"{name}: required fields drifted from schema"
        )
        # "group" and "sheet" are universal (accepted on every op; see
        # resolve_groups / the multi-sheet envelope) and live in every branch
        # for additionalProperties:false, not in the per-op field tables.
        schema_fields = set(branch.get("properties", {})) - {"op", "group", "sheet"}
        known = set(req_table.get(name, [])) | set(opt_table.get(name, {}))
        unknown = known - schema_fields
        assert not unknown, f"{name}: kit fields {unknown} not in schema"
        if name in ops._OP_FIELDS:
            # the validator's field-TYPE table is the unknown-field registry:
            # it must cover exactly the schema branch's fields
            assert set(ops._OP_FIELDS[name]) == schema_fields, (
                f"{name}: _OP_FIELDS drifted from schema properties"
            )


def test_every_op_has_field_type_table():
    assert set(ops._OP_FIELDS) == set(ops.OP_NAMES)


# ------------------------------------------- field KIND <-> schema shape ----
# ``_OP_FIELDS`` is the zero-dependency validator's type system; the schema
# branch is the jsonschema one. They are hand-maintained mirrors of ONE
# contract, and test_tables_match_schema only compares field NAMES — a
# `"num"` slot declared `{"type": "string"}` in the schema slipped through.
# This table is the translation, and every kind must have a row: a new kind
# fails here until it is mapped to the schema shape it promises.

KIND_TO_SCHEMA: dict[str, dict] = {
    "str": {"type": "string"},
    "num": {"type": "number"},
    "posint": {"type": "integer", "minimum": 1},
    "bool": {"type": "boolean"},
    "dict": {"type": "object"},
    "point": {"$ref": "#/$defs/point"},
    "endpoint": {"$ref": "#/$defs/endpoint"},
    "anchor": {"$ref": "#/$defs/anchor"},
    "anchor_ref": {"$ref": "#/$defs/anchorref"},
    "rotation": {"$ref": "#/$defs/rotation"},
    "mirror": {"$ref": "#/$defs/mirror"},
    "scope": {"$ref": "#/$defs/scope"},
    "fill": {"enum": ops._VALID_FILLS},
    "route_style": {"enum": ops._VALID_ROUTE_STYLES},
    # composite kinds — the validator checks these in dedicated helpers
    # (_check_match / _check_sheet_pins / the add_wire|add_bus block)
    "match": {
        "type": "object",
        "required": ["kind"],
        "additionalProperties": False,
        "properties": {
            "kind": {"enum": ops._MATCH_KINDS},
            "name": {"type": "string"},
            "at": {"$ref": "#/$defs/point"},
        },
    },
    "sheetpins": {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["name", "type", "side", "offset_mil"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "type": {"enum": ops._SHEET_PIN_TYPES},
                "side": {"enum": ops._SHEET_PIN_SIDES},
                "offset_mil": {"type": "number"},
            },
        },
    },
    # `vertices` is the one op-specific kind: add_wire accepts pin refs,
    # add_bus is points-only (mirrors the _is_endpoint/_is_point split in
    # the validator's wire block).
    "vertices": {"type": "array", "minItems": 2, "items": "<per-op>"},
}

_VERTEX_ITEMS = {
    "add_wire": {"$ref": "#/$defs/endpoint"},
    "add_bus": {"$ref": "#/$defs/point"},
}


def _norm(node):
    """Schema shape stripped to its TYPE contract (prose and order removed)."""
    if isinstance(node, dict):
        return {k: (frozenset(v) if k in ("enum", "required") else _norm(v))
                for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_norm(v) for v in node]
    return node


def test_field_kinds_match_schema():
    branches = _schema_ops()
    assert set(KIND_TO_SCHEMA) == {k for f in ops._OP_FIELDS.values()
                                   for k in f.values()}, (
        "KIND_TO_SCHEMA must cover exactly the kinds used by _OP_FIELDS")
    defs = SCHEMA["$defs"]
    for name, fields in sorted(ops._OP_FIELDS.items()):
        props = branches[name]["properties"]
        for field, kind in sorted(fields.items()):
            want = dict(KIND_TO_SCHEMA[kind])
            if kind == "vertices":
                want["items"] = _VERTEX_ITEMS[name]
            got = props[field]      # name coverage: test_tables_match_schema
            assert _norm(got) == _norm(want), (
                f"{name}.{field}: kind {kind!r} promises {want!r} but the "
                f"schema branch declares {got!r}")
            ref = got.get("$ref")
            if ref:
                assert ref.removeprefix("#/$defs/") in defs, (
                    f"{name}.{field}: dangling {ref}")


def test_enums_match_schema():
    """Every enum frozenset in ops.py IS a schema enum — assert set equality."""
    defs, branches = SCHEMA["$defs"], _schema_ops()
    sheet_pin = branches["add_sheet"]["properties"]["pins"]["items"]["properties"]
    cases = [
        ("$defs/rotation", ops._VALID_ROTATIONS, defs["rotation"]),
        ("$defs/mirror", ops._VALID_MIRRORS, defs["mirror"]),
        ("$defs/scope", ops._VALID_SCOPES, defs["scope"]),
        ("target_format", ops._VALID_TARGETS,
         SCHEMA["properties"]["target_format"]),
        ("add_rectangle.fill", ops._VALID_FILLS,
         branches["add_rectangle"]["properties"]["fill"]),
        ("route_net.style", ops._VALID_ROUTE_STYLES,
         branches["route_net"]["properties"]["style"]),
        ("delete_object.match.kind", ops._MATCH_KINDS,
         branches["delete_object"]["properties"]["match"]["properties"]["kind"]),
        ("add_sheet.pins[].type", ops._SHEET_PIN_TYPES, sheet_pin["type"]),
        ("add_sheet.pins[].side", ops._SHEET_PIN_SIDES, sheet_pin["side"]),
        ("place_array.direction", frozenset(ops._ARRAY_DIRECTIONS),
         branches["place_array"]["properties"]["direction"]),
    ]
    for where, table, node in cases:
        enum = node["enum"]
        assert len(enum) == len(set(enum)), f"{where}: duplicate enum value"
        assert set(enum) == set(table), (
            f"{where}: schema enum {sorted(map(str, enum))} drifted from the "
            f"ops.py table {sorted(map(str, table))}")


def test_template_fills_required_fields():
    for name in sorted(ops.OP_NAMES):
        op = ops.op_template(name)
        assert op["op"] == name
        for field in ops._OP_REQUIRED.get(name, []):
            assert field in op, f"{name}: template misses required {field!r}"


def test_cli_ops_list(capsys):
    assert cli.main(["ops", "list"]) == 0
    out = capsys.readouterr().out
    assert "place_component" in out and "delete_object" in out
    assert "required:" in out


def test_cli_ops_template(capsys):
    assert cli.main(["ops", "template", "move_component"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["protocol_version"] == ops.PROTOCOL_VERSION
    (op,) = doc["ops"]
    assert op["op"] == "move_component"
    assert {"designator", "x_mil", "y_mil"} <= set(op)


def test_cli_ops_template_unknown_op(capsys):
    assert cli.main(["ops", "template", "not_an_op"]) == 2


def test_cli_ops_bare_is_usage(capsys):
    assert cli.main(["ops"]) == 2
