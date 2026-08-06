"""Census: every op in the registry gets an apply-twice byte-equality proof.

CLAUDE.md's Invariants section states "Every new op needs an apply-twice
byte-equality test" as a cross-cutting rule, but — unlike the op<->handler
census (``test_ops.py::test_writer_handlers_census_matches_registry``), the
schema-export census (``test_schema_exports.py``) or the docs count gates
(``test_docs_conformance.py``) — nothing MACHINE-ENFORCED that every
registered op actually holds the property. A new op could ship with a writer
bug that breaks byte-identical re-apply and no test would notice. This file
closes that gap: it parametrizes over the FULL op registry (``ops.OP_NAMES``
core+sugar, plus ``ops.MACRO_OPS``) and, for every one of them, drives a
minimal op-list through ``akcli draw --apply`` TWICE against the same file,
asserting the bytes are identical (SPEC invariant: deterministic UUIDv5 seeded
from node CONTENT, never the op index, plus ``_append_top_idempotent``
replace-in-place).

Some ops are only meaningful anchored to something else (``set_component_*``
need an existing designator; ``add_no_connect``/``add_wire``/``add_net_label``
read a pin; ``add_bus_entry`` must land on a bus/wire or the connectivity gate
refuses the write; ``add_sheet`` needs its child file to already exist). Each
such op gets CONTEXT ops prepended (``_Case.context``) ahead of the op under
test (``_Case.target``) — see ``_CASES`` below.

Fixtures are fully generic (a self-contained symbol library of R/C/LED/
Crystal/a 2-unit generic cell + GND/VCC/PWR_FLAG power symbols — no
project-specific parts) so this file runs without any real KiCad install,
exactly like ``tests/test_macros2.py``'s ``_MIN_SYM``.

The census assertion at the bottom is fail-closed: a future op added to
``src/akcli/ops.py`` without a matching entry HERE (exercised or explicitly
excluded with a reason) fails the suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from akcli import ops
from akcli.errors import EXIT

from ._cli_harness import blank_sheet, run_cli

# --------------------------------------------------------------------------- #
# a fully generic, self-contained symbol library (no KiCad install needed)
# --------------------------------------------------------------------------- #
# Nicknames in `lib_id` ("Device:R", "power:GND", ...) are free-form labels
# when symbols are supplied via `--symbols`: resolution matches the bare
# symbol NAME after the colon against whatever `--symbols` file(s) were
# given, never the nickname (see _find_symbol in writers/lib_cache.py /
# readers/kicad_lib.py). One file can therefore serve every nickname below.
#
# `place_led_indicator` / `place_crystal` hardcode "Device:LED" /
# "Device:Crystal" internally (ops.py `_expand_led_indicator` /
# `_expand_crystal`), so this library must define bare symbols named "LED"
# and "Crystal" for those macros to resolve at all.
_SYM_LIB = """\
(kicad_symbol_lib (version 20231120) (generator akcli_test)
  (symbol "R" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "C" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "C_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "LED" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LED" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "LED_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "K" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "A" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "Crystal" (pin_numbers (hide yes)) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "Y" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Crystal" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "Crystal_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
  (symbol "CELL2U" (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "CELL2U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "CELL2U_1_1"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "IN+" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin input line (at -5.08 -2.54 0) (length 2.54)
        (name "IN-" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "OUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27))))))
    (symbol "CELL2U_2_1"
      (pin input line (at -5.08 2.54 0) (length 2.54)
        (name "IN+" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
      (pin input line (at -5.08 -2.54 0) (length 2.54)
        (name "IN-" (effects (font (size 1.27 1.27)))) (number "5" (effects (font (size 1.27 1.27)))))
      (pin output line (at 5.08 0 180) (length 2.54)
        (name "OUT" (effects (font (size 1.27 1.27)))) (number "6" (effects (font (size 1.27 1.27)))))))
  (symbol "GND" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "GND_1_1"
      (pin power_in line (at 0 0 270) (length 0)
        (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "VCC" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "VCC" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "VCC_1_1"
      (pin power_in line (at 0 0 90) (length 0)
        (name "VCC" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
  (symbol "PWR_FLAG" (power) (pin_names (offset 0))
    (exclude_from_sim no) (in_bom yes) (on_board yes)
    (property "Reference" "#FLG" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Value" "PWR_FLAG" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "PWR_FLAG_1_1"
      (pin power_out line (at 0 0 90) (length 0)
        (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))))
)
"""


@pytest.fixture(scope="session")
def sym_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write ``_SYM_LIB`` once for the whole session (every case reuses it)."""
    p = tmp_path_factory.mktemp("op_idempotency_syms") / "census.kicad_sym"
    p.write_text(_SYM_LIB, encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------- #
# op-list case table: context ops (anchors the target op needs) + target op
# --------------------------------------------------------------------------- #
def _r(designator: str, x_mil: float, y_mil: float, value: str = "10k") -> dict:
    return {"op": "place_component", "lib_id": "Device:R", "designator": designator,
            "x_mil": x_mil, "y_mil": y_mil, "value": value}


def _make_child_sheet(tmp_path: Path) -> None:
    """``add_sheet``'s child file must exist on disk before the op runs
    (``SHEET_FILE_MISSING`` otherwise) — filesystem prep, not an op."""
    blank_sheet(tmp_path, "child.kicad_sch")


@dataclass(frozen=True)
class _Case:
    context: list[dict] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    # filesystem prep that isn't itself an op (only add_sheet needs this)
    setup: Callable[[Path], None] | None = None

    @property
    def oplist(self) -> list[dict]:
        return [*self.context, self.target]


_CASES: dict[str, _Case] = {
    # ------------------------------------------------------------- core ops
    "place_component": _Case(target=_r("R1", 1000, 1000)),
    "set_component_transform": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "set_component_transform", "designator": "R1", "rotation": 90},
    ),
    "set_component_parameters": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "set_component_parameters", "designator": "R1", "value": "20k"},
    ),
    "add_wire": _Case(
        # pin-to-pin self loop: both endpoints land exactly on a pin, so the
        # connectivity gate's dangling-endpoint check is satisfied for free.
        context=[_r("R1", 1000, 1000)],
        target={"op": "add_wire", "vertices": ["R1.1", "R1.2"]},
    ),
    "route_net": _Case(
        # off-axis pins force the auto-router's L-corner path, not just a
        # straight segment.
        context=[_r("R1", 1000, 1000), _r("R2", 2000, 1500)],
        target={"op": "route_net", "from": "R1.2", "to": "R2.1"},
    ),
    "add_junction": _Case(target={"op": "add_junction", "at": [1000, 1000]}),
    "add_no_connect": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "add_no_connect", "pin": "R1.1"},
    ),
    "add_net_label": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "add_net_label", "name": "SIG_A", "at": "R1.1"},
    ),
    "place_power_port": _Case(
        target={"op": "place_power_port", "lib_id": "power:GND",
                "net_name": "GND", "at": [1000, 1000]},
    ),
    "add_bus": _Case(
        target={"op": "add_bus", "vertices": [[1000, 1000], [1500, 1000]]},
    ),
    "add_bus_entry": _Case(
        # both bus_entry ends (`at` and `at + size`) must land on a bus/wire
        # segment or DANGLING_BUS_ENTRY refuses the write; a single 2-segment
        # orthogonal wire routed through BOTH points satisfies both ends with
        # no components needed.
        context=[{"op": "add_wire",
                  "vertices": [[1000, 1000], [1000, 1100], [1100, 1100]]}],
        target={"op": "add_bus_entry", "at": [1000, 1000]},   # + default size [100,100]
    ),
    "add_text": _Case(target={"op": "add_text", "text": "note", "at": [1000, 1000]}),
    "add_rectangle": _Case(
        target={"op": "add_rectangle", "start": [1000, 1000], "end": [1300, 1300]},
    ),
    "add_text_box": _Case(
        target={"op": "add_text_box", "text": "box", "at": [1000, 1000],
                "size": [800, 400]},
    ),
    "add_sheet": _Case(
        target={"op": "add_sheet", "name": "CHILD", "file": "child.kicad_sch",
                "at": [1000, 1000], "size": [1000, 800]},
        setup=_make_child_sheet,
    ),
    "set_title_block": _Case(target={"op": "set_title_block", "title": "Census Sheet"}),
    "delete_component": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "delete_component", "designator": "R1"},
    ),
    "delete_object": _Case(
        context=[_r("R1", 1000, 1000),
                 {"op": "add_wire", "vertices": ["R1.1", "R1.2"]}],
        target={"op": "delete_object", "match": {"kind": "wire"}},
    ),
    "move_component": _Case(
        context=[_r("R1", 1000, 1000)],
        target={"op": "move_component", "designator": "R1",
                "x_mil": 2000, "y_mil": 2000},
    ),
    "rename_net": _Case(
        # the context label is deliberately re-emitted (same anchor+name) by
        # EVERY apply before the rename runs, so the rename always has a real
        # "OLD_NET" -> "NEW_NET" transition to perform, on both passes.
        context=[_r("R1", 1000, 1000),
                 {"op": "add_net_label", "name": "OLD_NET", "at": "R1.1"}],
        target={"op": "rename_net", "from": "OLD_NET", "to": "NEW_NET"},
    ),
    "place_gnd": _Case(target={"op": "place_gnd", "at": [1000, 1000]}),
    "place_vcc": _Case(target={"op": "place_vcc", "at": [1500, 1000]}),

    # ---------------------------------------------------------------- macros
    "place_divider": _Case(target={
        "op": "place_divider", "x_mil": 1000, "y_mil": 1000,
        "top_net": "RAIL_A", "mid_net": "MID_N", "bottom_net": "RAIL_B"}),
    "place_decoupling": _Case(target={
        "op": "place_decoupling", "x_mil": 1000, "y_mil": 1000,
        "power_net": "RAIL_A"}),
    "place_pullup": _Case(target={
        "op": "place_pullup", "x_mil": 1000, "y_mil": 1000,
        "net": "SIG_N", "rail_net": "RAIL_A"}),
    "place_led_indicator": _Case(target={
        "op": "place_led_indicator", "x_mil": 1000, "y_mil": 1000,
        "net": "SIG_N"}),
    "place_rc_filter": _Case(target={
        "op": "place_rc_filter", "x_mil": 1000, "y_mil": 1000,
        "in_net": "SIG_A", "out_net": "SIG_B"}),
    "place_crystal": _Case(target={
        "op": "place_crystal", "x_mil": 1000, "y_mil": 1000,
        "in_net": "XI", "out_net": "XO"}),
    "connect_and_label": _Case(
        # pin-ref endpoints (not bare points): the wire lands exactly on
        # R1.2/R2.1, satisfying the dangling-endpoint gate; the macro's mid()
        # anchor resolution needs the two pins axis-aligned, so R2 sits
        # directly below R1 (same pattern place_divider uses internally).
        context=[_r("R1", 1000, 1000), _r("R2", 1000, 1400)],
        target={"op": "connect_and_label", "from": "R1.2", "to": "R2.1",
                "net": "SIG_N"},
    ),
    "place_pwr_flag": _Case(target={"op": "place_pwr_flag", "at": [1000, 1000]}),
    "terminate_unused_unit": _Case(target={
        "op": "terminate_unused_unit", "designator": "U1", "lib_id": "Device:CELL2U",
        "unit": 2, "at": [1000, 1000],
        "in_plus": "4", "in_minus": "5", "out": "6"}),
    "place_array": _Case(target={
        "op": "place_array", "lib_id": "Device:R", "designator_prefix": "R",
        "count": 3, "x_mil": 1000, "y_mil": 1000}),
}

# Ops in the registry that are NOT exercised above, with a one-line reason
# each. Kept separate from _CASES (and asserted disjoint from it below) so a
# name can never silently satisfy the census both ways. Empty at the time of
# writing: the writer's deterministic-UUID + replace-in-place design (see
# CLAUDE.md Invariants) held for every op the census could reach; nothing here
# was found to violate byte-identical re-apply.
_EXCLUDED: dict[str, str] = {}


def _write_oplist(tmp_path: Path, op_list: list[dict], name: str = "ops.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(
        {"protocol_version": ops.PROTOCOL_VERSION, "target_format": "kicad",
         "ops": op_list}),
        encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------- #
# census discipline: every registered op is accounted for, exactly once
# --------------------------------------------------------------------------- #
def test_every_registered_op_is_exercised_or_excluded() -> None:
    """Fail-closed gate: a new op with neither a case nor an exclusion fails
    this test — mirrors ``test_ops.py``'s writer-handler census."""
    registry = set(ops.OP_NAMES) | set(ops.MACRO_OPS)
    accounted = set(_CASES) | set(_EXCLUDED)
    missing = registry - accounted
    stale = accounted - registry
    assert not missing, (
        f"op(s) added to the registry with no idempotency census entry: "
        f"{sorted(missing)} — add a _Case to _CASES or an _EXCLUDED reason")
    assert not stale, (
        f"census references op(s) no longer in the registry: {sorted(stale)} "
        f"— stale _CASES/_EXCLUDED entry")
    overlap = set(_CASES) & set(_EXCLUDED)
    assert not overlap, f"op(s) both exercised and excluded: {sorted(overlap)}"


# --------------------------------------------------------------------------- #
# the actual apply-twice byte-equality proof, per op
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("op_name", sorted(_CASES))
def test_apply_twice_is_byte_identical(op_name: str, tmp_path: Path,
                                       sym_lib: Path) -> None:
    case = _CASES[op_name]
    if case.setup is not None:
        case.setup(tmp_path)
    target = blank_sheet(tmp_path, "sheet.kicad_sch")
    opsfile = _write_oplist(tmp_path, case.oplist)
    argv = ["draw", str(target), "--ops", str(opsfile), "--apply",
            "--symbols", str(sym_lib)]

    rc1, out1, err1 = run_cli(argv)
    assert rc1 == EXIT["OK"], f"{op_name}: first apply refused\n{out1}\n{err1}"
    once = target.read_bytes()

    rc2, out2, err2 = run_cli(argv)
    assert rc2 == EXIT["OK"], f"{op_name}: second apply refused\n{out2}\n{err2}"
    twice = target.read_bytes()

    assert once == twice, (
        f"{op_name}: re-applying the same op-list is not byte-identical "
        f"(apply-twice invariant violated)")
