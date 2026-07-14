"""Tests for the op-list authoring kit (`akcli ops list|template`).

Includes the drift guard: the in-code required/optional tables must match
``schemas/ops.schema.json`` so `ops template` never teaches a stale shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from akcli import cli, ops

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "schemas" / "ops.schema.json").read_text()
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
        schema_fields = set(branch.get("properties", {})) - {"op"}
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
