"""Tests for :mod:`akcli.writers.kicad` — the op-list executor (SPEC §3.5).

Covers, against the synthetic KiCad-8 R-divider fixture (``board_v8.kicad_sch``):

* ``--dry-run`` (default) edits in memory, verifies, and writes **nothing**;
* ``--apply`` writes atomically (with optional backup) only when every op
  succeeded AND post-write :func:`connectivity.verify` is error-free;
* idempotent replay — re-running an op-list converges (no duplicate
  wires/labels/symbols, byte-identical file);
* ``place_component`` caches the symbol, emits per-pin ``(pin "N" (uuid))`` nodes,
  and writes the reference into BOTH the ``Reference`` property and ``(instances)``;
* a ``"REF.PIN"`` wire endpoint is snapped to the pin's exact world coordinate;
* protocol-major mismatch / non-``kicad`` target / structural errors raise;
* a per-op failure (unknown symbol, missing pin ref) is reported as an error
  result and suppresses the write;
* rotating an already-wired component is caught by the post-write verify.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli.errors import AkcliError
from akcli.readers import kicad_lib, sexpr
from akcli.report import Severity
from akcli.writers import connectivity, geometry
from akcli.writers import kicad as kw

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _copy(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_bytes(V8.read_bytes())
    return tgt


def _oplist(*ops, protocol=1, target="kicad") -> dict:
    return {"protocol_version": protocol, "target_format": target, "ops": list(ops)}


def _verify_errors(doc) -> list[str]:
    return [
        f.code
        for f in connectivity.verify(doc)
        if f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]


def _reference(sym) -> str | None:
    inst = sym.find("instances")
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                r = path.find("reference")
                if r is not None and len(r.children or []) >= 2:
                    return r.children[1].value
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


def _symbol_by_ref(doc, ref):
    for s in doc.find_all("symbol"):
        if s.find("lib_id") is not None and _reference(s) == ref:
            return s
    return None


# --------------------------------------------------------------------------- #
# dry-run vs apply
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing(tmp_path):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    findings: list = []
    results = kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:C",
                 "designator": "C9", "x_mil": 4000, "y_mil": 4000}),
        str(tgt), apply=False, verify_out=findings,
    )
    assert [r.status for r in results] == ["ok"]
    assert tgt.read_bytes() == before          # nothing written
    assert findings == []                       # clean


def test_apply_writes_and_reparses_clean(tmp_path):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    results = kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:C",
                 "designator": "C9", "x_mil": 4000, "y_mil": 4000, "value": "1u"}),
        str(tgt), apply=True,
    )
    assert results[0].status == "ok"
    assert results[0].created_uuids
    assert tgt.read_bytes() != before
    doc = sexpr.parse(tgt.read_text())          # still valid s-expr
    assert _verify_errors(doc) == []
    assert _symbol_by_ref(doc, "C9") is not None


def test_apply_is_idempotent(tmp_path):
    tgt = _copy(tmp_path)
    ops = _oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
         "x_mil": 7000, "y_mil": 4000},
        {"op": "add_wire", "vertices": ["R9.1", "R9.2"]},
        {"op": "add_text", "text": "note", "at": [3000, 3000]},
    )
    kw.apply(ops, str(tgt), apply=True)
    once = tgt.read_bytes()
    kw.apply(ops, str(tgt), apply=True)
    assert tgt.read_bytes() == once             # converges byte-identically


def test_idempotent_does_not_duplicate_nodes(tmp_path):
    tgt = _copy(tmp_path)
    ops = _oplist(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
         "x_mil": 7000, "y_mil": 4000},
        {"op": "add_wire", "vertices": ["R9.1", "R9.2"]},
    )
    kw.apply(ops, str(tgt), apply=True)
    n1 = len(sexpr.parse(tgt.read_text()).find_all("wire"))
    kw.apply(ops, str(tgt), apply=True)
    n2 = len(sexpr.parse(tgt.read_text()).find_all("wire"))
    assert n1 == n2


# --------------------------------------------------------------------------- #
# place_component internals
# --------------------------------------------------------------------------- #
def test_place_component_emits_per_pin_and_instances(tmp_path):
    tgt = _copy(tmp_path)
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:R",
                 "designator": "R9", "x_mil": 7000, "y_mil": 4000}),
        str(tgt), apply=True,
    )
    doc = sexpr.parse(tgt.read_text())
    sym = _symbol_by_ref(doc, "R9")
    assert sym is not None
    # per-pin nodes: R has two pins
    pin_numbers = [p.children[1].value for p in sym.find_all("pin")]
    assert pin_numbers == ["1", "2"]
    # both the Reference property AND the instances block carry "R9"
    inst = sym.find("instances")
    assert inst is not None
    ref_prop = next(
        p for p in sym.find_all("property") if p.children[1].value == "Reference"
    )
    assert ref_prop.children[2].value == "R9"


def test_place_component_deterministic_uuid(tmp_path):
    a = _copy(tmp_path / "a")
    b = _copy(tmp_path / "b")
    op = _oplist({"op": "place_component", "lib_id": "Device:R",
                  "designator": "R9", "x_mil": 7000, "y_mil": 4000})
    ra = kw.apply(op, str(a), apply=False)
    rb = kw.apply(op, str(b), apply=False)
    assert ra[0].created_uuids == rb[0].created_uuids   # stable across runs


# --------------------------------------------------------------------------- #
# pin-ref snapping
# --------------------------------------------------------------------------- #
def test_wire_pinref_snaps_to_pin_world(tmp_path):
    tgt = _copy(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R9",
             "x_mil": 7000, "y_mil": 4000},
            {"op": "add_wire", "vertices": ["R9.1", "R9.2"]},
        ),
        str(tgt), apply=True,
    )
    doc = sexpr.parse(tgt.read_text())
    assert _verify_errors(doc) == []            # the wire actually connects

    # recompute the two pin worlds and confirm the new wire hits them exactly
    sym = _symbol_by_ref(doc, "R9")
    lib = kicad_lib.library_from_lib_symbols(doc.find("lib_symbols"))
    symdef = kicad_lib.resolve("Device:R", [lib])
    comp = kw._instance_component(sym, "Device:R")
    worlds = {geometry.pin_world(symdef, comp, p) for p in symdef.pins}

    found = False
    for w in doc.find_all("wire"):
        verts = set()
        for xy in w.find("pts").find_all("xy"):
            from akcli import units
            verts.add((units.mm_to_nm(float(xy.children[1].value)),
                       units.mm_to_nm(float(xy.children[2].value))))
        if verts == worlds:
            found = True
    assert found, "no wire matched the two R9 pin world coordinates"


# --------------------------------------------------------------------------- #
# net label / junction / power / text
# --------------------------------------------------------------------------- #
def test_primitive_ops_create_nodes(tmp_path):
    tgt = _copy(tmp_path)
    results = kw.apply(
        _oplist(
            {"op": "add_net_label", "name": "SIG", "at": [3000, 3000]},
            {"op": "add_junction", "at": [3000, 3000]},
            {"op": "add_text", "text": "hi", "at": [2000, 2000]},
            {"op": "place_gnd", "at": [4000, 4000]},
        ),
        str(tgt), apply=True, verify_out=(f := []),
    )
    assert all(r.status == "ok" for r in results)
    assert [x.code for x in f if x.severity in (Severity.ERROR, Severity.CRITICAL)] == []
    doc = sexpr.parse(tgt.read_text())
    assert doc.find_all("label")
    assert doc.find_all("junction")
    assert doc.find_all("text")
    # place_gnd places a power:GND symbol with an auto #PWR ref
    refs = {_reference(s) for s in doc.find_all("symbol") if s.find("lib_id")}
    assert any(r and r.startswith("#PWR0") for r in refs)


def test_set_component_parameters(tmp_path):
    tgt = _copy(tmp_path)
    kw.apply(
        _oplist({"op": "set_component_parameters", "designator": "C1",
                 "value": "220n", "footprint": "C_0603",
                 "parameters": {"MPN": "XYZ"}}),
        str(tgt), apply=True,
    )
    doc = sexpr.parse(tgt.read_text())
    sym = _symbol_by_ref(doc, "C1")
    props = {p.children[1].value: p.children[2].value for p in sym.find_all("property")}
    assert props["Value"] == "220n"
    assert props["Footprint"] == "C_0603"
    assert props["MPN"] == "XYZ"


def test_transform_fresh_component(tmp_path):
    tgt = _copy(tmp_path)
    kw.apply(
        _oplist(
            {"op": "place_component", "lib_id": "Device:R", "designator": "R20",
             "x_mil": 9000, "y_mil": 9000},
            {"op": "set_component_transform", "designator": "R20",
             "rotation": 90, "mirror": "x"},
        ),
        str(tgt), apply=True,
    )
    doc = sexpr.parse(tgt.read_text())
    sym = _symbol_by_ref(doc, "R20")
    assert sym.find("at").children[3].value == "90"
    assert sym.find("mirror").children[1].value == "x"


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def test_apply_with_backup_dir(tmp_path):
    tgt = _copy(tmp_path)
    original = tgt.read_bytes()
    bdir = tmp_path / "backups"
    kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:C",
                 "designator": "C9", "x_mil": 4000, "y_mil": 4000}),
        str(tgt), apply=True, backup_dir=str(bdir),
    )
    bak = bdir / (tgt.name + ".bak")
    assert bak.exists()
    assert bak.read_bytes() == original         # pre-write snapshot preserved


# --------------------------------------------------------------------------- #
# error / guard paths
# --------------------------------------------------------------------------- #
def test_protocol_major_mismatch_raises(tmp_path):
    tgt = _copy(tmp_path)
    with pytest.raises(AkcliError) as ei:
        kw.apply(_oplist(protocol=2), str(tgt), apply=True)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_non_kicad_target_raises(tmp_path):
    tgt = _copy(tmp_path)
    with pytest.raises(AkcliError) as ei:
        kw.apply(_oplist(target="altium"), str(tgt), apply=True)
    assert ei.value.code == "OP_UNSUPPORTED"


def test_structural_error_raises(tmp_path):
    tgt = _copy(tmp_path)
    with pytest.raises(AkcliError):
        kw.apply(
            _oplist({"op": "place_component"}),    # missing required fields
            str(tgt), apply=True,
        )


def test_unknown_symbol_errors_and_no_write(tmp_path):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    results = kw.apply(
        _oplist({"op": "place_component", "lib_id": "Device:NOPE",
                 "designator": "X1", "x_mil": 1000, "y_mil": 1000}),
        str(tgt), apply=True,
    )
    assert results[0].status == "error"
    assert results[0].error_code == "SYMBOL_NOT_FOUND"
    assert tgt.read_bytes() == before           # op error suppresses the write


def test_missing_pin_ref_errors_and_no_write(tmp_path):
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    results = kw.apply(
        _oplist({"op": "add_wire", "vertices": ["ZZ.1", "ZZ.2"]}),
        str(tgt), apply=True,
    )
    assert results[0].status == "error"
    assert tgt.read_bytes() == before


def test_transform_of_wired_component_fails_verify(tmp_path):
    # Rotating R1 (already wired in the fixture) orphans its wires -> verify error
    # -> the apply must NOT write.
    tgt = _copy(tmp_path)
    before = tgt.read_bytes()
    findings: list = []
    kw.apply(
        _oplist({"op": "set_component_transform", "designator": "R1",
                 "rotation": 90}),
        str(tgt), apply=True, verify_out=findings,
    )
    assert any(
        f.code == connectivity.DANGLING_ENDPOINT for f in findings
    )
    assert tgt.read_bytes() == before
