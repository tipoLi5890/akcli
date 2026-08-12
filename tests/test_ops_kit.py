"""Tests for the op-list authoring kit (`akcli ops list|template`).

Also the DECLARATION gates. Before 0.21 the op vocabulary was written out
three times (the ``ops.py`` tables, ``schemas/ops.schema.json``, the writer
registry) and this file held four mutual-pinning matrices to stop the copies
drifting apart. There is one copy now — :mod:`akcli.opspec` — so the matrices
are gone and what remains is the two questions a single source still can't
answer for itself:

* is the GENERATED artifact (both JSON schemas) current with the declaration?
  — ``test_generated_schema_is_current``
* is the declaration COMPLETE, i.e. does every spec carry the parts the rest
  of the system reads out of it (handler/expander wiring, coordinate
  classification, a schema fragment per kind, known designator roles)?
  — the ``test_opspec_*`` group.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from akcli import cli, ops, opspec
from akcli.ops.groups import _translate_op_coords

ROOT = Path(__file__).resolve().parents[1]


def _tools_schemas():
    """``tools/schemas.py`` imported by path (tools/ is not on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "_tools_schemas", ROOT / "tools" / "schemas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------- the generated artifacts ----
def test_generated_schema_is_current():
    """Both JSON schemas are byte-exactly what opspec renders TODAY.

    Same assertion as ``python3 tools/schemas.py check``, run inside pytest so
    a spec edit that was never regenerated (or a hand edit to either schema)
    fails the suite rather than only the one CI step someone might skip.
    """
    gen = _tools_schemas()
    for name, want in (("ops.schema.json", gen.render(gen.build_schema())),
                       ("ops.capabilities.json",
                        gen.render(gen.build_capabilities()))):
        have = (ROOT / "schemas" / name).read_text(encoding="utf-8")
        assert have == want, (
            f"schemas/{name} is not what opspec renders — run "
            "`python3 tools/schemas.py build` (and review the diff)")


# ------------------------------------------------- declaration completeness --
_KINDS_WITH_COORDS = frozenset({"point", "endpoint", "anchor", "vertices"})
# The coordinate roles ``Field.coord`` may take, and the field kinds each is
# meaningful on (``akcli.ops.groups`` reads exactly this classification).
_COORD_KINDS: dict[str, frozenset[str]] = {
    "x": frozenset({"num"}),
    "y": frozenset({"num"}),
    "point": frozenset({"point", "endpoint", "anchor"}),
    "vertices": frozenset({"vertices"}),
}


def test_opspec_coord_roles_are_well_formed():
    for spec in opspec.ALL:
        for f in spec.fields:
            if f.coord is None:
                continue
            assert spec.kind != "macro", (
                f"{spec.name}.{f.name}: macros expand BEFORE groups resolve, "
                "so a macro field must not be coordinate-classified")
            assert f.coord in _COORD_KINDS, (
                f"{spec.name}.{f.name}: unknown coord role {f.coord!r}")
            assert f.kind in _COORD_KINDS[f.coord], (
                f"{spec.name}.{f.name}: coord {f.coord!r} is meaningless on "
                f"kind {f.kind!r}")
            if f.coord in ("x", "y"):
                assert f.name == f"{f.coord}_mil", (
                    f"{spec.name}.{f.name}: coord {f.coord!r} names the "
                    f"{f.coord}_mil half of a scalar position pair")


@pytest.mark.parametrize("name", sorted(opspec.core_names() | opspec.sugar_names()))
def test_opspec_coord_classification_matches_group_resolution(name):
    """Every coordinate-bearing core field is classified, and the
    classification is what ``resolve_groups`` actually does.

    A ``point``/``endpoint``/``anchor``/``vertices`` slot either translates
    with its group (``coord`` set) or is position-INDEPENDENT (``coord``
    None: an ``offset_mil`` delta, a ``size`` extent). Both answers are
    exercised here against the real translator, so a new op whose point slot
    nobody wired into ``akcli.ops.groups`` fails instead of silently landing
    at the world origin.
    """
    spec = opspec.by_name(name)
    for f in spec.fields:
        if f.kind not in _KINDS_WITH_COORDS and f.coord is None:
            continue
        if f.coord in ("x", "y"):
            probe, want = 100, 100 + (1000 if f.coord == "x" else 2000)
        elif f.kind == "vertices":
            probe, want = [[10, 20], [30, 40]], [[1010, 2020], [1030, 2040]]
        else:
            probe = [10, 20]
            want = [1010, 2020] if f.coord == "point" else [10, 20]
        out = _translate_op_coords({"op": name, f.name: probe}, 1000, 2000)
        assert out[f.name] == want, (
            f"{name}.{f.name} (kind {f.kind}, coord {f.coord!r}): group "
            f"resolution produced {out[f.name]!r}, expected {want!r} — "
            "classify the slot in opspec (or teach akcli.ops.groups its "
            "field name)")


def test_opspec_handlers_match_the_writer_registry():
    from akcli.writers import handlers as _handlers  # noqa: F401  (registers)
    from akcli.writers.registry import HANDLERS

    for spec in opspec.CORE:
        assert spec.handler, f"{spec.name}: core/sugar spec declares no handler"
        assert spec.name in HANDLERS, (
            f"{spec.name}: declared but no @op handler is registered")
        assert HANDLERS[spec.name].__name__ == spec.handler, (
            f"{spec.name}: opspec says handler {spec.handler!r}, the registry "
            f"has {HANDLERS[spec.name].__name__!r}")
    assert set(HANDLERS) == {s.name for s in opspec.CORE}, (
        "writers.registry.HANDLERS drifted from opspec.CORE")


def test_opspec_expanders_match_the_macro_table():
    for spec in opspec.MACROS:
        assert spec.expander, f"{spec.name}: macro spec declares no expander"
        assert spec.name in ops._MACRO_EXPANDERS, (
            f"{spec.name}: declared but has no expander")
        assert ops._MACRO_EXPANDERS[spec.name].__name__ == spec.expander, (
            f"{spec.name}: opspec says expander {spec.expander!r}, the table "
            f"has {ops._MACRO_EXPANDERS[spec.name].__name__!r}")
    assert set(ops._MACRO_EXPANDERS) == set(opspec.macro_names())


def test_every_kind_has_a_schema_fragment_and_a_validator_rule():
    """Both translations of ``Field.kind`` cover every kind opspec uses.

    ``tools/schemas.py`` compiles a kind into its JSON-schema fragment; the
    zero-dependency validator checks the same kind with a predicate. A new
    kind that is only half-mapped would either sail past the validator or
    escape the schema.
    """
    gen = _tools_schemas()
    used = {f.kind for s in opspec.ALL for f in s.fields}
    used |= {f.kind for f in opspec.UNIVERSAL_FIELDS}
    missing = sorted(used - set(gen._KIND_SCHEMA) - {"vertices"})
    assert not missing, f"kinds with no _KIND_SCHEMA fragment: {missing}"

    # the validator handles these by name rather than through _KIND_CHECKS
    special = {"rotation", "mirror", "scope", "match", "sheetpins", "vertices"}
    core_used = {f.kind for s in opspec.CORE for f in s.fields}
    unchecked = sorted(core_used - set(ops._KIND_CHECKS) - special)
    assert not unchecked, f"kinds with no validator rule: {unchecked}"


def test_opspec_designator_roles_are_known():
    for spec in opspec.ALL:
        if not spec.designator_roles:
            assert not spec.designator_field, spec.name
            continue
        assert spec.designator_field, f"{spec.name}: roles without a field"
        assert spec.designator_field in spec.field_names(), spec.name
        for role in spec.designator_roles:
            assert role in opspec.ROLE_PREFIXES, (
                f"{spec.name}: unknown designator role {role!r}")


def test_array_direction_vectors_cover_the_enum():
    """The expander's unit-step table and the declared enum are one set."""
    assert (frozenset(ops._ARRAY_DIRECTIONS)
            == frozenset(opspec.ENUMS["array_direction"]))


def test_derived_tables_cover_every_op():
    assert set(ops._OP_FIELDS) == set(ops.OP_NAMES)
    assert set(ops._OP_REQUIRED) == set(ops.OP_NAMES)
    assert set(ops._OP_OPTIONAL) == set(ops.OP_NAMES)
    assert set(ops.MACRO_REQUIRED) == set(ops.MACRO_OPS)
    assert set(ops.MACRO_OPTIONAL) == set(ops.MACRO_OPS)


# ------------------------------------------------------------ the CLI kit ----
def test_template_fills_required_fields():
    for name in sorted(ops.OP_NAMES):
        op = ops.op_template(name)
        assert op["op"] == name
        for field in ops._OP_REQUIRED.get(name, []):
            assert field in op, f"{name}: template misses required {field!r}"


def test_template_fills_required_macro_fields():
    for name in sorted(ops.MACRO_OPS):
        op = ops.op_template(name)
        assert op["op"] == name
        for field in ops.MACRO_REQUIRED.get(name, []):
            assert field in op, f"{name}: template misses required {field!r}"
        for field in ops._MACRO_TEMPLATE_OMIT.get(name, frozenset()):
            assert field not in op, f"{name}: template teaches omitted {field!r}"


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
