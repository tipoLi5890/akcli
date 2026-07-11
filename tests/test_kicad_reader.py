"""Tests for :mod:`altium_kicad_cli.readers.kicad` (SPEC §3.4).

Parses the synthetic KiCad 7 and KiCad 8 R-divider fixtures into the normalized
model and asserts:

* pin electrical types are resolved from ``lib_symbols`` onto instance pins;
* the divider / power / decoupling nets form correctly (shared ``netbuild``);
* v7 and v8 — the same circuit in two file-format versions — yield the same
  logical nets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli import model
from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.readers import kicad

FIX = Path(__file__).parent / "fixtures" / "kicad"
V7 = FIX / "board_v7.kicad_sch"
V8 = FIX / "board_v8.kicad_sch"


def _net_with(sch: model.Schematic, ref_pin: tuple[str, str]) -> model.Net:
    return next(n for n in sch.nets if ref_pin in n.members)


def _members(sch: model.Schematic, ref_pin: tuple[str, str]) -> set:
    return set(_net_with(sch, ref_pin).members)


# --------------------------------------------------------------------------- #
# components + pin-type resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [V7, V8])
def test_reads_five_components(path):
    sch = kicad.read_sch(path)
    assert sch.source_format == "kicad"
    refs = {c.designator for c in sch.components}
    assert refs == {"R1", "R2", "C1", "#PWR01", "#PWR02"}
    assert all(not c.undesignated for c in sch.components)


@pytest.mark.parametrize("path", [V7, V8])
def test_pin_electrical_types_resolved_from_lib_symbols(path):
    sch = kicad.read_sch(path)
    comps = {c.designator: c for c in sch.components}
    # R/C instance pins carry no type in the file; resolved to PASSIVE from lib.
    r1 = comps["R1"]
    assert len(r1.pins) == 2
    assert all(p.electrical_type is model.PinType.PASSIVE for p in r1.pins)
    # power symbol pins resolve to POWER_IN.
    assert comps["#PWR01"].pins[0].electrical_type is model.PinType.POWER_IN
    assert comps["#PWR02"].pins[0].electrical_type is model.PinType.POWER_IN


@pytest.mark.parametrize("path", [V7, V8])
def test_component_value_and_footprint(path):
    sch = kicad.read_sch(path)
    comps = {c.designator: c for c in sch.components}
    assert comps["R1"].value == "10k"
    assert comps["C1"].value == "100n"
    assert comps["R1"].footprint == "Resistor_SMD:R_0402_1005Metric"
    assert comps["R1"].library_ref == "Device:R"


# --------------------------------------------------------------------------- #
# net inference (shared with Altium via netbuild)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [V7, V8])
def test_divider_midpoint_net(path):
    sch = kicad.read_sch(path)
    mid = _net_with(sch, ("R1", "2"))
    assert {("R1", "2"), ("R2", "1"), ("C1", "1")} <= set(mid.members)
    # the divider midpoint must NOT leak the rails.
    assert ("R1", "1") not in mid.members
    assert ("R2", "2") not in mid.members
    # MID carries a local label + a global_label "VOUT" on the same node.
    assert mid.is_named
    assert set([mid.name, *mid.aliases]) == {"MID", "VOUT"}
    assert mid.confidence < 1.0  # multi-name net flagged


@pytest.mark.parametrize("path", [V7, V8])
def test_gnd_net_includes_power_pin_via_t_junction(path):
    sch = kicad.read_sch(path)
    gnd = _net_with(sch, ("R2", "2"))
    assert {("R2", "2"), ("C1", "2"), ("#PWR02", "1")} <= set(gnd.members)
    assert gnd.name == "GND"


@pytest.mark.parametrize("path", [V7, V8])
def test_v33_net(path):
    sch = kicad.read_sch(path)
    v33 = _net_with(sch, ("R1", "1"))
    assert {("R1", "1"), ("#PWR01", "1")} <= set(v33.members)
    assert ("R1", "2") not in v33.members
    assert v33.name == "+3V3"


@pytest.mark.parametrize("path", [V7, V8])
def test_three_distinct_nets(path):
    sch = kicad.read_sch(path)
    a = _net_with(sch, ("R1", "1")).stable_id
    b = _net_with(sch, ("R1", "2")).stable_id
    c = _net_with(sch, ("R2", "2")).stable_id
    assert len({a, b, c}) == 3


# --------------------------------------------------------------------------- #
# v7 == v8 (same circuit, two format versions)
# --------------------------------------------------------------------------- #
def test_v7_and_v8_yield_same_logical_nets():
    s7 = kicad.read_sch(V7)
    s8 = kicad.read_sch(V8)
    groups7 = {frozenset(n.members) for n in s7.nets}
    groups8 = {frozenset(n.members) for n in s8.nets}
    assert groups7 == groups8
    # and the canonical names match net-for-net.
    names7 = {frozenset(n.members): n.name for n in s7.nets}
    names8 = {frozenset(n.members): n.name for n in s8.nets}
    assert names7 == names8


def test_v7_and_v8_same_components_and_pin_types():
    s7 = kicad.read_sch(V7)
    s8 = kicad.read_sch(V8)

    def sig(sch):
        return sorted(
            (
                c.designator,
                c.library_ref,
                tuple((p.number, p.electrical_type.value) for p in c.pins),
            )
            for c in sch.components
        )

    assert sig(s7) == sig(s8)


# --------------------------------------------------------------------------- #
# read_primitives + error path
# --------------------------------------------------------------------------- #
def test_read_primitives_emits_expected_counts():
    prims = kicad.read_primitives(V8)
    assert len(prims.wires) == 5
    assert len(prims.junctions) == 2
    # 1 local label + 1 global_label + 2 power-port pseudo-labels.
    assert len(prims.labels) == 4
    # 3 R/C two-pin parts + 2 single-pin power parts = 8 pin handles.
    assert len(prims.pins) == 8


def test_read_sch_metadata_present():
    sch = kicad.read_sch(V8)
    meta = sch.metadata
    assert meta["component_count"] == 5
    assert meta["pin_count"] == 8
    assert meta["passive_pin_ratio"] == pytest.approx(6 / 8)
    assert meta["unnamed_net_count"] == 0


def test_wrong_root_tag_raises():
    with pytest.raises(AkcliError) as ei:
        kicad.read_sch(FIX / "symbols" / "Device.kicad_sym")
    assert ei.value.code == "ALTIUM_MALFORMED"


# --------------------------------------------------------------------------- #
# duplicate designators vs multi-unit parts
# --------------------------------------------------------------------------- #
_DUP_LIB = """\
	(lib_symbols
		(symbol "RR" (pin_numbers (hide yes)) (pin_names (offset 0))
			(exclude_from_sim no) (in_bom yes) (on_board yes)
			(property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
			(property "Value" "RR" (at 0 0 0) (effects (font (size 1.27 1.27))))
			(symbol "RR_1_1"
				(pin passive line (at 0 3.81 270) (length 1.27)
					(name "~" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27)))))
				(pin passive line (at 0 -3.81 90) (length 1.27)
					(name "~" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27)))))))
		(symbol "DUAL" (pin_numbers (hide yes)) (pin_names (offset 0))
			(exclude_from_sim no) (in_bom yes) (on_board yes)
			(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
			(property "Value" "DUAL" (at 0 0 0) (effects (font (size 1.27 1.27))))
			(symbol "DUAL_1_1"
				(pin passive line (at -5.08 0 0) (length 2.54)
					(name "A" (effects (font (size 1.27 1.27))))
					(number "1" (effects (font (size 1.27 1.27))))))
			(symbol "DUAL_2_1"
				(pin passive line (at -5.08 0 0) (length 2.54)
					(name "B" (effects (font (size 1.27 1.27))))
					(number "2" (effects (font (size 1.27 1.27))))))))"""

_DUP_ROOT = "ad000000-0000-4000-8000-000000000000"


def _dup_symbol(n: int, lib_id: str, ref: str, x_mm: float, unit: int = 1) -> str:
    return f"""\
	(symbol (lib_id "{lib_id}") (at {x_mm} 50.8 0) (unit {unit})
		(uuid "ad000000-0000-4000-8000-{n:012d}")
		(property "Reference" "{ref}" (at 0 0 0) (effects (font (size 1.27 1.27))))
		(property "Value" "{lib_id}" (at 0 0 0) (effects (font (size 1.27 1.27))))
		(pin "1" (uuid "ad000000-0000-4000-8000-{n + 1:012d}"))
		(pin "2" (uuid "ad000000-0000-4000-8000-{n + 2:012d}"))
		(instances (project "dup"
			(path "/{_DUP_ROOT}" (reference "{ref}") (unit {unit})))))"""


def _dup_sch(tmp_path, symbols: list[str]):
    text = (f'(kicad_sch\n\t(version 20231120)\n\t(generator "eeschema")\n'
            f'\t(uuid "{_DUP_ROOT}")\n\t(paper "A4")\n'
            + _DUP_LIB + "\n" + "\n".join(symbols) + "\n)\n")
    p = tmp_path / "dup.kicad_sch"
    p.write_text(text)
    return p


def test_duplicate_designator_kept_as_distinct_component(tmp_path):
    """Two same-ref same-lib_id same-unit placements: NOT silently merged.

    Both stay in the component list under the shared designator (eeschema also
    keeps the shared reference on both placements' netlist nodes), with
    distinct unique_ids so checks/bom.py BOM_DUPLICATE_DESIGNATOR fires, a
    synthesized handle in parameters, and a reader warning.
    """
    p = _dup_sch(tmp_path, [
        _dup_symbol(100, "RR", "R5", 50.8),
        _dup_symbol(200, "RR", "R5", 76.2),
    ])
    sch = kicad.read_sch(p)

    r5s = [c for c in sch.components if c.designator == "R5"]
    assert len(r5s) == 2
    assert len({c.unique_id for c in r5s}) == 2
    dups = [c for c in r5s if c.parameters.get("akcli_duplicate")]
    assert [c.parameters["akcli_duplicate"] for c in dups] == ["R5@dup1"]
    assert any("duplicate designator 'R5'" in w for w in sch.warnings)

    from altium_kicad_cli.checks import bom
    codes = {f.code for f in bom.run(sch)}
    assert "BOM_DUPLICATE_DESIGNATOR" in codes

    # both placements' pins exist as primitives (4 pin handles under R5)
    members = [m for net in sch.nets for m in net.members]
    assert members.count(("R5", "1")) == 2


def test_multi_unit_placements_merge_without_warning(tmp_path):
    """Same ref + lib_id but DIFFERENT units = one multi-unit component."""
    p = _dup_sch(tmp_path, [
        _dup_symbol(100, "DUAL", "U1", 50.8, unit=1),
        _dup_symbol(200, "DUAL", "U1", 76.2, unit=2),
    ])
    sch = kicad.read_sch(p)

    u1s = [c for c in sch.components if c.designator == "U1"]
    assert len(u1s) == 1
    assert sorted(pin.number for pin in u1s[0].pins) == ["1", "2"]
    assert sch.warnings == []

    from altium_kicad_cli.checks import bom
    assert "BOM_DUPLICATE_DESIGNATOR" not in {f.code for f in bom.run(sch)}


# --------------------------------------------------------------------------- #
# read_pcb (no committed .kicad_pcb fixture; inline smoke test)
# --------------------------------------------------------------------------- #
def test_read_pcb_inline(tmp_path):
    pcb = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (net 2 "+3V3")\n'
        '  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu")\n'
        '    (at 100 100 90)\n'
        '    (property "Reference" "R1" (at 0 0 0))\n'
        '    (property "Value" "10k" (at 0 0 0)))\n'
        ')\n'
    )
    p = tmp_path / "board.kicad_pcb"
    p.write_text(pcb)
    result = kicad.read_pcb(p)
    assert result.source_format == "kicad"
    assert result.nets == ["GND", "+3V3"]
    assert len(result.footprints) == 1
    fp = result.footprints[0]
    assert fp.designator == "R1"
    assert fp.value == "10k"
    assert fp.layer == "F.Cu"
    assert fp.rotation == pytest.approx(90.0)
