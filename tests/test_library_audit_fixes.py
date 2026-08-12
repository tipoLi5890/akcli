"""Regression tests for the two ``library audit`` false-positive classes:

1. global lib-table (with ``(type "Table")`` indirection) resolution — standard
   KiCad nicknames must NOT be reported as unregistered;
2. KiCad ``{token}`` name un-escaping — a symbol whose library name is stored
   escaped (``A{slash}B``) and referenced raw (``A/B``) must MATCH.

Plus a true-negative gate: a nickname absent from BOTH the project and the
global table is still an error (don't swallow real problems). And a writer
round-trip check: ``draw`` writes the escaped form KiCad expects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import libtable
from akcli.cli import main


def _sch(*, sym_lib_id: str, footprint: str, inline_symbol_name: str) -> str:
    """A one-component schematic with an inline lib_symbols entry (2-pin R)."""
    return f"""(kicad_sch (version 20230121) (generator eeschema)
  (uuid 12121212-1212-1212-1212-121212121212)
  (paper "A4")
  (lib_symbols
    (symbol "{inline_symbol_name}" (in_bom yes) (on_board yes)
      (property "Reference" "D" (at 0 0 0))
      (property "Value" "V" (at 0 0 0))
      (symbol "R_1_1"
        (pin passive line (at 0 2.54 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (symbol (lib_id "{sym_lib_id}") (at 100 100 0) (unit 1)
    (uuid 34343434-3434-3434-3434-343434343434)
    (property "Reference" "D1" (at 100 95 0))
    (property "Value" "V" (at 100 105 0))
    (property "Footprint" "{footprint}" (at 100 100 0))
    (pin "1" (uuid 34340000-0000-0000-0000-000000000001))
    (pin "2" (uuid 34340000-0000-0000-0000-000000000002)))
)"""


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


# --------------------------------------------------------------------------- #
# 1. nested global table
# --------------------------------------------------------------------------- #
def test_nested_global_table_resolves(tmp_path):
    """The global table is one ``(type "Table")`` entry pointing at a template
    table; its entries must be merged so standard nicknames resolve."""
    template = tmp_path / "template"
    template.mkdir()
    (template / "fp-lib-table").write_text(
        '(fp_lib_table (version 7)\n'
        '  (lib (name "MyStd")(type "KiCad")(uri "MyStd.pretty")(options "")(descr ""))\n)')
    (template / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        '  (lib (name "Device")(type "KiCad")(uri "Device.kicad_sym")(options "")(descr ""))\n)')

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "fp-lib-table").write_text(
        f'(fp_lib_table (version 7)\n'
        f'  (lib (name "KiCad")(type "Table")(uri "{template.as_posix()}/fp-lib-table")(options "")(descr ""))\n)')
    (cfg / "sym-lib-table").write_text(
        f'(sym_lib_table (version 7)\n'
        f'  (lib (name "KiCad")(type "Table")(uri "{template.as_posix()}/sym-lib-table")(options "")(descr ""))\n)')

    # nested Table is expanded into the real entries
    fp = libtable.read_table(cfg / "fp-lib-table")
    assert fp.get("MyStd") is not None
    assert fp.get("KiCad") is None                  # the indirection entry is gone

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.kicad_sch").write_text(
        _sch(sym_lib_id="Device:R", footprint="MyStd:R_0402",
             inline_symbol_name="Device:R"))

    ws = libtable.discover(proj, kicad_config_home=cfg)
    assert ws.has_global_fp and ws.has_global_sym
    findings = libtable.audit(ws)
    # Device (sym) and MyStd (fp) both resolve via the global table -> nothing
    assert "FOOTPRINT_LIB_UNREGISTERED" not in _codes(findings)
    assert "SYMBOL_LIB_UNREGISTERED" not in _codes(findings)
    assert not [f for f in findings if f.severity.value in ("warning", "error")]


# --------------------------------------------------------------------------- #
# 2. escape round-trip (audit no longer false-flags '/'-named symbols)
# --------------------------------------------------------------------------- #
def test_escaped_symbol_name_no_false_missing(tmp_path):
    name_raw = "19-237/R6GHBHC-A04/2T"
    name_esc = "19-237{slash}R6GHBHC-A04{slash}2T"
    # the .kicad_sym stores the ESCAPED name (as KiCad writes it)
    (tmp_path / "proj_jlc.kicad_sym").write_text(
        f'(kicad_symbol_lib (version 20231120) (generator x)\n'
        f'  (symbol "{name_esc}" (in_bom yes) (on_board yes)\n'
        f'    (property "Reference" "D" (at 0 0 0))\n'
        f'    (property "Value" "V" (at 0 0 0)))\n)')
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        '  (lib (name "proj_jlc")(type "KiCad")(uri "${KIPRJMOD}/proj_jlc.kicad_sym")(options "")(descr ""))\n)')
    # the schematic references the symbol with a RAW '/'
    (tmp_path / "board.kicad_sch").write_text(
        _sch(sym_lib_id=f"proj_jlc:{name_raw}", footprint="Resistor_SMD:R_0402",
             inline_symbol_name=f"proj_jlc:{name_raw}"))

    ws = libtable.discover(tmp_path)              # global from conftest isolation
    findings = libtable.audit(ws)
    assert "SYMBOL_MISSING" not in _codes(findings)
    assert "SYMBOL_LIB_UNREGISTERED" not in _codes(findings)


def test_draw_writes_escaped_lib_id(tmp_path):
    """The writer emits KiCad's escaped form so a KiCad save leaves no diff."""
    name_raw, name_esc = "A/B", "A{slash}B"
    (tmp_path / "lib.kicad_sym").write_text(
        f'(kicad_symbol_lib (version 20231120) (generator x)\n'
        f'  (symbol "{name_esc}" (in_bom yes) (on_board yes)\n'
        f'    (symbol "{name_esc}_1_1"\n'
        f'      (pin passive line (at 0 2.54 270) (length 1.27)\n'
        f'        (name "~" (effects (font (size 1.27 1.27))))\n'
        f'        (number "1" (effects (font (size 1.27 1.27)))))\n'
        f'      (pin passive line (at 0 -2.54 90) (length 1.27)\n'
        f'        (name "~" (effects (font (size 1.27 1.27))))\n'
        f'        (number "2" (effects (font (size 1.27 1.27)))))))\n)')
    board = tmp_path / "board.kicad_sch"
    assert main(["new", str(board)]) == 0
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": 2, "target_format": "kicad",
        "ops": [{"op": "place_component", "lib_id": f"lib:{name_raw}",
                 "designator": "D1", "x_mil": 1000, "y_mil": 1000}],
    }))
    code = main(["draw", str(board), "--ops", str(ops),
                 "--symbols", str(tmp_path / "lib.kicad_sym"), "--apply"])
    assert code == 0
    text = board.read_text()
    assert f'(lib_id "lib:{name_esc}")' in text        # escaped instance id
    assert f'(lib_id "lib:{name_raw}")' not in text     # never the raw form
    assert f'(symbol "lib:{name_esc}"' in text          # escaped lib_symbols entry


# --------------------------------------------------------------------------- #
# 3. true-negative gate: a genuinely unregistered nickname is still an error
# --------------------------------------------------------------------------- #
def test_truly_unregistered_footprint_still_errors(tmp_path):
    (tmp_path / "board.kicad_sch").write_text(
        _sch(sym_lib_id="Device:R", footprint="NoSuchLib:Foo",
             inline_symbol_name="Device:R"))
    ws = libtable.discover(tmp_path)              # global from conftest (no NoSuchLib)
    assert ws.has_global_fp
    findings = libtable.audit(ws)
    errs = [f for f in findings
            if f.code == "FOOTPRINT_LIB_UNREGISTERED" and f.severity.value == "error"]
    assert len(errs) == 1
    assert "NoSuchLib" in errs[0].message and "will not find" in errs[0].message
