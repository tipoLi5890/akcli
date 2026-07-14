"""`akcli library` workspace: lib-table parsing, audit findings, repair plans.

The scenario mirrors the real-world failure that motivated the feature: a
JLC-converted symbol library whose Footprint fields use the converter's default
``footprint:`` nickname while the project's fp-lib-table registers a different
name (here ``proj_jlc``) — plus missing footprints, bare-relative 3D paths, and
a legacy (GUI-invisible) footprint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import libtable
from akcli.cli import main

_FP_TABLE = """(fp_lib_table
  (version 7)
  (lib (name "proj_jlc")(type "KiCad")(uri "${KIPRJMOD}/libs/jlc.pretty")(options "")(descr ""))
)"""

_SYM_TABLE = """(sym_lib_table
  (version 7)
  (lib (name "proj_jlc")(type "KiCad")(uri "${KIPRJMOD}/libs/proj_jlc.kicad_sym")(options "")(descr ""))
)"""

_SYM_LIB = """(kicad_symbol_lib (version 20231120) (generator kicad_symbol_editor)
  (symbol "LED_X" (pin_numbers hide) (in_bom yes) (on_board yes)
    (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LED_X" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "footprint:LED-X" (at 0 0 0) (effects (font (size 1.27 1.27))))
  )
)"""

_SCH = """(kicad_sch (version 20230121) (generator eeschema)
  (uuid 22222222-2222-2222-2222-222222222222)
  (paper "A4")
  (lib_symbols
    (symbol "proj_jlc:LED_X" (pin_numbers hide) (in_bom yes) (on_board yes)
      (property "Reference" "D" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "LED_X" (at 0 0 0) (effects (font (size 1.27 1.27))))
    )
  )
  (symbol (lib_id "proj_jlc:LED_X") (at 100 100 0) (unit 1)
    (uuid 33333333-3333-3333-3333-333333333333)
    (property "Reference" "D1" (at 100 95 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LED_X" (at 100 105 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "footprint:LED-X" (at 100 100 0) (effects (font (size 1.27 1.27)) hide))
  )
)"""

_MOD = """(footprint "LED-X" (version 20240108) (generator pcbnew)
  (layer "F.Cu")
  (attr smd)
  (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "packages3d/LED-X.step" (offset (xyz 0 0 0)))
)"""

_MOD_LEGACY = """(module OLD_ONE (layer F.Cu) (tedit 5A02FF4D)
  (pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu))
)"""


@pytest.fixture()
def project(tmp_path) -> Path:
    (tmp_path / "board.kicad_pro").write_text("{}")
    (tmp_path / "fp-lib-table").write_text(_FP_TABLE)
    (tmp_path / "sym-lib-table").write_text(_SYM_TABLE)
    (tmp_path / "board.kicad_sch").write_text(_SCH)
    libs = tmp_path / "libs"
    pretty = libs / "jlc.pretty"
    pretty.mkdir(parents=True)
    (pretty / "LED-X.kicad_mod").write_text(_MOD)
    (pretty / "OLD_ONE.kicad_mod").write_text(_MOD_LEGACY)
    (libs / "proj_jlc.kicad_sym").write_text(_SYM_LIB)
    return tmp_path


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_read_table_parses_entries(project):
    t = libtable.read_table(project / "fp-lib-table")
    assert t.kind == "fp"
    assert t.get("proj_jlc").uri == "${KIPRJMOD}/libs/jlc.pretty"


def test_audit_flags_the_nickname_trap(project):
    ws = libtable.discover(project)
    findings = libtable.audit(ws)
    codes = _codes(findings)
    # the #1 real-world trap: Footprint field says 'footprint:*', table says 'proj_jlc'
    assert "FOOTPRINT_LIB_UNREGISTERED" in codes
    # bare-relative 3D path -> missing step file
    assert "MODEL_MISSING" in codes
    # v5 module is API-parseable but GUI-invisible
    assert "FOOTPRINT_LEGACY_FORMAT" in codes


def test_audit_missing_lib_path(project):
    (project / "fp-lib-table").write_text(_FP_TABLE.replace("jlc.pretty", "nope.pretty"))
    findings = libtable.audit(libtable.discover(project))
    assert "LIB_PATH_MISSING" in _codes(findings)


def test_audit_missing_footprint(project):
    sch = (project / "board.kicad_sch").read_text()
    (project / "board.kicad_sch").write_text(
        sch.replace('"footprint:LED-X"', '"proj_jlc:GONE"'))
    findings = libtable.audit(libtable.discover(project))
    assert "FOOTPRINT_MISSING" in _codes(findings)


def test_repair_rename_fixes_the_audit(project):
    ws = libtable.discover(project)
    edits = libtable.plan_rename(ws, "footprint", "proj_jlc")
    assert len(edits) == 2                      # the .kicad_sym and the schematic
    for e in edits:
        e.path.write_text(e.new_text)
    findings = libtable.audit(libtable.discover(project))
    assert "FOOTPRINT_LIB_UNREGISTERED" not in _codes(findings)
    assert '"proj_jlc:LED-X"' in (project / "board.kicad_sch").read_text()


def test_repair_model_paths_absolute(project):
    ws = libtable.discover(project)
    edits = libtable.plan_model_paths(ws, "absolute")
    assert len(edits) == 1
    edits[0].path.write_text(edits[0].new_text)
    text = (project / "libs" / "jlc.pretty" / "LED-X.kicad_mod").read_text()
    expect = (project / "libs" / "jlc.pretty" / "packages3d" / "LED-X.step").resolve()
    assert expect.as_posix() in text


def test_repair_model_paths_var_prefix(project):
    ws = libtable.discover(project)
    edits = libtable.plan_model_paths(ws, "${PROJ_3D}")
    assert len(edits) == 1
    assert '${PROJ_3D}/LED-X.step' in edits[0].new_text


def test_repair_model_paths_rejects_bad_mode(project):
    with pytest.raises(ValueError):
        libtable.plan_model_paths(libtable.discover(project), "portable")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_library_audit_json(project, capsys):
    code = main(["library", "audit", str(project), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1                             # findings present
    codes = {f["code"] for f in doc["findings"]}
    assert "FOOTPRINT_LIB_UNREGISTERED" in codes
    assert doc["fp_lib_table"].endswith("fp-lib-table")


def test_cli_library_repair_plan_then_apply(project, capsys):
    code = main(["library", "repair", str(project),
                 "--rename-footprint-lib", "footprint=proj_jlc"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert '"footprint:LED-X"' in (project / "board.kicad_sch").read_text()

    code = main(["library", "repair", str(project),
                 "--rename-footprint-lib", "footprint=proj_jlc", "--apply"])
    assert code == 0
    assert '"proj_jlc:LED-X"' in (project / "board.kicad_sch").read_text()
    assert (project / "board.kicad_sch.bak").exists()

    code = main(["library", "audit", str(project), "--json"])
    capsys.readouterr()
    # nickname trap fixed; remaining findings are the 3D/legacy warnings
    assert code == 1


def test_cli_library_repair_requires_an_action(project, capsys):
    assert main(["library", "repair", str(project)]) == 2


# --------------------------------------------------------------------------- #
# library import-altium (.PcbLib -> .pretty)
# --------------------------------------------------------------------------- #
def _make_pcblib(tmp_path: Path) -> Path:
    import struct
    import sys as _sys
    GEN = Path(__file__).resolve().parent / "fixtures" / "_gen"
    if str(GEN) not in _sys.path:
        _sys.path.insert(0, str(GEN))
    import cfbf_builder

    def block(payload: bytes) -> bytes:
        return struct.pack("<I", len(payload)) + payload

    def pascal(s: str) -> bytes:
        b = s.encode("latin-1")
        return bytes([len(b)]) + b

    geo = bytearray(61)
    geo[0] = 1
    struct.pack_into("<H", geo, 3, 0xFFFF)
    struct.pack_into("<H", geo, 7, 0xFFFF)
    struct.pack_into("<i", geo, 21, 12000)   # 1.2 mil-e4 -> 0.03048 mm... (units)
    struct.pack_into("<i", geo, 25, 8000)
    geo[49] = 2
    struct.pack_into("<d", geo, 52, 0.0)
    geo[60] = 1
    pad = bytes([0x02]) + block(pascal("1")) + block(b"") * 3 + block(bytes(geo))
    stream = block(pascal("FP(WEIRD)")) + pad
    blob, _ = cfbf_builder.build_cfbf(
        {"Library/Data": b"\x00" * 8, "FPW/Data": stream})
    p = tmp_path / "weird.PcbLib"
    p.write_bytes(blob)
    return p


def test_import_altium_dry_run_writes_nothing(tmp_path, capsys):
    src = _make_pcblib(tmp_path)
    out = tmp_path / "weird.pretty"
    assert main(["library", "import-altium", str(src), "--out", str(out)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert not out.exists()


def test_import_altium_apply_writes_mod_and_provenance(tmp_path, capsys):
    src = _make_pcblib(tmp_path)
    out = tmp_path / "weird.pretty"
    assert main(["library", "import-altium", str(src), "--out", str(out),
                 "--courtyard", "0.25", "--apply", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    # parenthesized vendor name sanitized, and DECLARED in the warnings
    assert doc["footprints"][0]["name"] == "FP-WEIRD"
    assert any("renamed" in w for w in doc["warnings"])
    assert any("courtyard synthesized" in w for w in doc["warnings"])

    from akcli.readers import footprint_lib
    back = footprint_lib.read_kicad_mod(out / "FP-WEIRD.kicad_mod")
    fp = back.footprints[0]
    assert [p.number for p in fp.pads] == ["1"]
    assert fp.courtyard is True
    assert fp.format_version is not None      # modern format, GUI-visible

    prov = json.loads((out / "provenance.json").read_text())
    assert prov["source"]["sha256"]
    assert prov["footprints"][0]["source_name"] == "FP(WEIRD)"


def test_import_altium_is_deterministic(tmp_path):
    src = _make_pcblib(tmp_path)
    out1, out2 = tmp_path / "a.pretty", tmp_path / "b.pretty"
    assert main(["library", "import-altium", str(src), "--out", str(out1), "--apply"]) == 0
    assert main(["library", "import-altium", str(src), "--out", str(out2), "--apply"]) == 0
    assert (out1 / "FP-WEIRD.kicad_mod").read_text() == (out2 / "FP-WEIRD.kicad_mod").read_text()
