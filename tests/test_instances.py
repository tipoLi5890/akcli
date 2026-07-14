"""Tests for :mod:`akcli.writers.instances` (SPEC §3.5).

Covers the four guarantees the module owes the writer:

* :func:`write_instance` writes the reference designator into **both**
  ``(property "Reference")`` and the ``(instances ...)`` block, in sync;
* idempotency — a repeated write leaves the symbol byte-identical;
* :func:`alloc_pwr_ref` hands out unique ``#PWR0<n>`` references;
* :func:`instances_path` returns the flat root-sheet path and rejects sub-sheets
  with ``HIERARCHICAL_UNSUPPORTED``; plus the deterministic UUIDv5 helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli.errors import AkcliError
from akcli.readers import sexpr
from akcli.writers import instances

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"
ROOT_UUID = "8a000000-0000-4000-8000-000000000000"


def _doc():
    return sexpr.parse(V8.read_text())


def _bare_symbol(unit: int = 1) -> sexpr.SNode:
    """A placed symbol with a Reference property but *no* instances block yet."""
    text = (
        '(symbol\n'
        '\t(lib_id "Device:R")\n'
        '\t(at 10 10 0)\n'
        f'\t(unit {unit})\n'
        '\t(uuid "11111111-0000-4000-8000-000000000099")\n'
        '\t(property "Reference" "R?"\n'
        '\t\t(at 0 0 0)\n'
        '\t\t(effects (font (size 1.27 1.27))))\n'
        '\t(pin "1" (uuid "11111111-0000-4000-8000-0000000000a1"))\n'
        '\t(pin "2" (uuid "11111111-0000-4000-8000-0000000000a2")))'
    )
    return sexpr.parse(text)


# --------------------------------------------------------------------------- #
# instances_path / root_uuid
# --------------------------------------------------------------------------- #
def test_root_uuid():
    assert instances.root_uuid(_doc()) == ROOT_UUID


@pytest.mark.parametrize("sheet", ["", "/", ROOT_UUID, "/" + ROOT_UUID, None])
def test_instances_path_flat(sheet):
    assert instances.instances_path(_doc(), sheet) == "/" + ROOT_UUID


@pytest.mark.parametrize(
    "sheet",
    [
        "/" + ROOT_UUID + "/abcdef00-0000-4000-8000-000000000001",
        "deadbeef-0000-4000-8000-000000000002",
        "a/b/c",
    ],
)
def test_instances_path_subsheet_rejected(sheet):
    with pytest.raises(AkcliError) as ei:
        instances.instances_path(_doc(), sheet)
    assert ei.value.code == "HIERARCHICAL_UNSUPPORTED"


# --------------------------------------------------------------------------- #
# project_name
# --------------------------------------------------------------------------- #
def test_project_name_from_existing_instances():
    assert instances.project_name(_doc()) == "board_v8"


def test_project_name_default_when_empty():
    empty = sexpr.parse(f'(kicad_sch (uuid "{ROOT_UUID}"))')
    assert instances.project_name(empty) == instances.DEFAULT_PROJECT


# --------------------------------------------------------------------------- #
# alloc_pwr_ref
# --------------------------------------------------------------------------- #
def test_alloc_pwr_ref_next_after_fixture():
    # board_v8 already uses #PWR01 and #PWR02.
    assert instances.alloc_pwr_ref(_doc()) == "#PWR03"


def test_alloc_pwr_ref_empty_sheet():
    empty = sexpr.parse(f'(kicad_sch (uuid "{ROOT_UUID}"))')
    assert instances.alloc_pwr_ref(empty) == "#PWR01"


def test_alloc_pwr_ref_handles_double_digit():
    doc = sexpr.parse(
        f'(kicad_sch (uuid "{ROOT_UUID}")'
        '  (symbol (property "Reference" "#PWR09"))'
        '  (symbol (property "Reference" "#PWR010")))'
    )
    assert instances.alloc_pwr_ref(doc) == "#PWR011"


# --------------------------------------------------------------------------- #
# write_instance — both fields, in sync
# --------------------------------------------------------------------------- #
def _reference_property_value(sym: sexpr.SNode) -> str | None:
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


def _instance_fields(sym: sexpr.SNode) -> tuple[str, str, str, str]:
    inst = sym.find("instances")
    proj = inst.find("project")
    path = proj.find("path")
    ref = path.find("reference")
    unit = path.find("unit")
    return (
        proj.children[1].value,
        path.children[1].value,
        ref.children[1].value,
        unit.children[1].value,
    )


def test_write_instance_writes_both_in_sync():
    doc = _doc()
    sym = _bare_symbol()
    path = instances.instances_path(doc)
    instances.write_instance(doc, sym, "R3", path)

    assert _reference_property_value(sym) == "R3"
    proj, ipath, iref, iunit = _instance_fields(sym)
    assert proj == "board_v8"          # copied from existing instances in doc
    assert ipath == "/" + ROOT_UUID
    assert iref == "R3"                # in sync with the property
    assert iunit == "1"


def test_write_instance_honours_symbol_unit():
    doc = _doc()
    sym = _bare_symbol(unit=2)
    instances.write_instance(doc, sym, "U5", instances.instances_path(doc))
    _, _, _, iunit = _instance_fields(sym)
    assert iunit == "2"


def test_write_instance_explicit_project_override():
    doc = _doc()
    sym = _bare_symbol()
    instances.write_instance(
        doc, sym, "R3", instances.instances_path(doc), project="myproj"
    )
    proj, _, _, _ = _instance_fields(sym)
    assert proj == "myproj"


def test_write_instance_output_reparses():
    doc = _doc()
    sym = _bare_symbol()
    instances.write_instance(doc, sym, "R3", instances.instances_path(doc))
    # The mutated subtree must serialize to valid, re-parseable S-expression.
    reparsed = sexpr.parse(sexpr.dumps(sym))
    assert reparsed.tag == "symbol"
    assert _reference_property_value(reparsed) == "R3"
    assert _instance_fields(reparsed)[2] == "R3"


def test_write_instance_idempotent():
    doc = _doc()
    sym = _bare_symbol()
    path = instances.instances_path(doc)
    instances.write_instance(doc, sym, "R3", path)
    once = sexpr.dumps(sym)
    instances.write_instance(doc, sym, "R3", path)
    twice = sexpr.dumps(sym)
    assert once == twice
    # And exactly one instances block exists (no duplication on replay).
    assert len(sym.find_all("instances")) == 1


def test_write_instance_updates_existing_block():
    # Take an existing fully-annotated symbol from the fixture and rewrite it.
    doc = _doc()
    sym = next(
        s for s in doc.find_all("symbol")
        if _reference_property_value(s) == "R1"
    )
    instances.write_instance(doc, sym, "R9", instances.instances_path(doc))
    assert _reference_property_value(sym) == "R9"
    assert _instance_fields(sym)[2] == "R9"
    assert len(sym.find_all("instances")) == 1


def test_write_instance_creates_reference_property_when_missing():
    doc = sexpr.parse(f'(kicad_sch (uuid "{ROOT_UUID}"))')
    sym = sexpr.parse(
        '(symbol (lib_id "power:GND") (at 0 0 0) (unit 1)'
        ' (uuid "22222222-0000-4000-8000-000000000001"))'
    )
    instances.write_instance(doc, sym, "#PWR03", instances.instances_path(doc))
    assert _reference_property_value(sym) == "#PWR03"
    assert _instance_fields(sym)[2] == "#PWR03"


# --------------------------------------------------------------------------- #
# deterministic_uuid
# --------------------------------------------------------------------------- #
def test_deterministic_uuid_stable_and_distinct():
    a = instances.deterministic_uuid(ROOT_UUID, "R3", 0)
    b = instances.deterministic_uuid(ROOT_UUID, "R3", 0)
    assert a == b                                   # idempotent
    assert a != instances.deterministic_uuid(ROOT_UUID, "R3", 1)   # op index matters
    assert a != instances.deterministic_uuid(ROOT_UUID, "R4", 0)   # designator matters
    # canonical UUID string
    import uuid as _u

    assert str(_u.UUID(a)) == a


def test_deterministic_uuid_non_uuid_namespace():
    # A non-canonical sheet id still yields a stable, valid UUID.
    a = instances.deterministic_uuid("not-a-uuid", "R3", 0)
    b = instances.deterministic_uuid("not-a-uuid", "R3", 0)
    assert a == b
    import uuid as _u

    assert str(_u.UUID(a)) == a
