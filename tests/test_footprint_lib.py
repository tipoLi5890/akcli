"""Footprint-library readers + fail-loud format detection (Phase 0/1).

Covers the historic silent-failure trap: a ``.PcbLib`` (an OLE2 container) used
to be sniffed as ``altium_sch`` and "read" into an empty schematic with exit 0.
Now: the extension routes to the PcbLib reader, a bare OLE container is
classified by its storage layout, an unknown layout fails loudly, and a
non-empty source normalizing to nothing raises the ``EMPTY_IMPORT`` warning
(fatal under ``read --strict``).
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.commands._shared import _detect_format, _detect_format_ex
from akcli.errors import AkcliError
from akcli.readers import footprint_lib

FIX = Path(__file__).resolve().parent / "fixtures"
GEN = FIX / "_gen"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))
import cfbf_builder  # noqa: E402  (fixture generator, self-contained stdlib)


# --------------------------------------------------------------------------- #
# synthetic .PcbLib building blocks
# --------------------------------------------------------------------------- #
def _block(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def _pascal(s: str) -> bytes:
    b = s.encode("latin-1")
    return bytes([len(b)]) + b


def _pad_record(number: str, *, x=0, y=0, sx=12000, sy=8000, hole=0,
                shape=2, rotation=0.0, plated=1, layer=1) -> bytes:
    """One type-0x02 pad record (offsets mirror altium_pcb_bin.parse_pads)."""
    geo = bytearray(61)
    geo[0] = layer
    struct.pack_into("<H", geo, 3, 0xFFFF)          # no net
    struct.pack_into("<H", geo, 7, 0xFFFF)          # no component
    struct.pack_into("<i", geo, 13, x)
    struct.pack_into("<i", geo, 17, y)
    struct.pack_into("<i", geo, 21, sx)
    struct.pack_into("<i", geo, 25, sy)
    struct.pack_into("<i", geo, 45, hole)
    geo[49] = shape
    struct.pack_into("<d", geo, 52, rotation)
    geo[60] = plated
    return (bytes([0x02]) + _block(_pascal(number))
            + _block(b"") * 3 + _block(bytes(geo)))


def _track_record() -> bytes:
    return bytes([0x04]) + _block(b"\x00" * 40)


def _footprint_stream(name: str, records: bytes) -> bytes:
    return _block(_pascal(name)) + records


def _write_pcblib(path: Path, storages: dict[str, bytes]) -> Path:
    streams = {"Library/Data": b"\x00" * 32}
    for name, data in storages.items():
        streams[f"{name}/Data"] = data
    blob, _meta = cfbf_builder.build_cfbf(streams)
    path.write_bytes(blob)
    return path


# --------------------------------------------------------------------------- #
# format detection
# --------------------------------------------------------------------------- #
def test_pcblib_extension_detected(tmp_path):
    p = _write_pcblib(tmp_path / "part.PcbLib",
                      {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    assert _detect_format(p) == "altium_pcblib"


def test_bare_ole_pcblib_sniffed_by_content(tmp_path):
    p = _write_pcblib(tmp_path / "noext",
                      {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    fmt, method = _detect_format_ex(p)
    assert (fmt, method) == ("altium_pcblib", "content")


def test_bare_ole_pcbdoc_sniffed_by_content(tmp_path):
    blob, _ = cfbf_builder.build_cfbf({"Board6/Data": b"\x00" * 16})
    p = tmp_path / "noext"
    p.write_bytes(blob)
    assert _detect_format(p) == "altium_pcb"


def test_unknown_ole_is_not_assumed_schematic(tmp_path):
    """The .PcbLib-as-SchDoc trap: an unrecognized OLE layout must NOT read as
    an empty schematic with exit 0."""
    blob, _ = cfbf_builder.build_cfbf({"Mystery/Data": b"\x00" * 16})
    p = tmp_path / "mystery"
    p.write_bytes(blob)
    assert _detect_format(p) == "unknown"
    assert main(["read", str(p)]) == 5


def test_schdoc_extension_still_routes_to_schematic():
    assert _detect_format(FIX / "t_junction.SchDoc") == "altium_sch"


def test_kicad_mod_extension_detected(tmp_path):
    p = tmp_path / "r.kicad_mod"
    p.write_text('(footprint "R" (version 20240108) (layer "F.Cu"))')
    assert _detect_format(p) == "kicad_mod"


# --------------------------------------------------------------------------- #
# .PcbLib reader
# --------------------------------------------------------------------------- #
def test_read_pcblib_pads_and_unsupported_primitives(tmp_path):
    records = _pad_record("1", x=10000, y=-20000) + _pad_record("2") + _track_record()
    p = _write_pcblib(tmp_path / "two.PcbLib",
                      {"FPX": _footprint_stream("FPX", records)})
    lib = footprint_lib.read_pcblib(p)
    assert [f.name for f in lib.footprints] == ["FPX"]
    fp = lib.footprints[0]
    assert [pad.number for pad in fp.pads] == ["1", "2"]
    # 10000 units = 1 mil = 0.0254 mm; Altium +Y up flips to +Y down.
    assert fp.pads[0].x_mm == pytest.approx(0.0254)
    assert fp.pads[0].y_mm == pytest.approx(0.0508)
    assert fp.pads[0].size_x_mm == pytest.approx(1.2 * 0.0254)
    assert any("UNSUPPORTED_PRIMITIVE" in w and "track" in w for w in fp.warnings)


def test_read_pcblib_npth_and_thru_hole(tmp_path):
    records = (_pad_record("1", hole=300000, plated=1, layer=74)
               + _pad_record("2", hole=300000, plated=0, layer=74))
    p = _write_pcblib(tmp_path / "th.PcbLib",
                      {"TH": _footprint_stream("TH", records)})
    fp = footprint_lib.read_pcblib(p).footprints[0]
    assert fp.pads[0].pad_type == "thru_hole"
    assert fp.pads[1].pad_type == "np_thru_hole"
    assert fp.pads[0].drill_mm == pytest.approx(30 * 0.0254)
    assert fp.attributes == ["through_hole"]


def test_read_pcblib_no_footprints_fails_loudly(tmp_path):
    blob, _ = cfbf_builder.build_cfbf({"Library/Data": b"\x00" * 8})
    p = tmp_path / "empty.PcbLib"
    p.write_bytes(blob)
    with pytest.raises(AkcliError) as ei:
        footprint_lib.read_pcblib(p)
    assert ei.value.code == "ALTIUM_UNSUPPORTED"


def test_cli_read_pcblib_json(tmp_path, capsys):
    p = _write_pcblib(tmp_path / "one.PcbLib",
                      {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    assert main(["read", str(p), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == "1.2"
    assert doc["metadata"]["detected_format"] == "altium_pcblib"
    assert doc["metadata"]["object_counts"] == {"symbols": 0, "footprints": 1}
    assert doc["footprints"][0]["pads"][0]["number"] == "1"


# --------------------------------------------------------------------------- #
# .kicad_mod / .pretty reader
# --------------------------------------------------------------------------- #
_MOD_V6 = """(footprint "R_0402" (version 20240108) (generator pcbnew)
  (layer "F.Cu")
  (attr smd)
  (fp_line (start -1 -0.5) (end 1 -0.5) (layer "F.CrtYd") (width 0.05))
  (pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "packages3d/R_0402.step" (offset (xyz 0 0 0)))
)"""

_MOD_LEGACY = """(module R_LEGACY (layer F.Cu) (tedit 5A02FF4D)
  (pad 1 smd rect (at -0.5 0) (size 0.5 0.5) (layers F.Cu F.Paste F.Mask))
)"""


def test_read_kicad_mod_v6(tmp_path):
    p = tmp_path / "R_0402.kicad_mod"
    p.write_text(_MOD_V6)
    lib = footprint_lib.read_kicad_mod(p)
    fp = lib.footprints[0]
    assert fp.name == "R_0402"
    assert fp.format_version == "20240108"
    assert fp.courtyard is True
    assert fp.attributes == ["smd"]
    assert fp.models == ["packages3d/R_0402.step"]
    assert len(fp.pads) == 2
    assert fp.pads[0].shape == "roundrect"
    assert fp.pads[0].layers == ["F.Cu", "F.Paste", "F.Mask"]
    assert not fp.warnings


def test_read_kicad_mod_legacy_module_warns(tmp_path):
    """KiCad v5 `(module ...)`: API-parseable but GUI-invisible — must warn."""
    p = tmp_path / "R_LEGACY.kicad_mod"
    p.write_text(_MOD_LEGACY)
    lib = footprint_lib.read_kicad_mod(p)
    fp = lib.footprints[0]
    assert fp.format_version is None
    assert any("LEGACY_FORMAT" in w for w in fp.warnings)


def test_read_pretty_dir(tmp_path):
    d = tmp_path / "test.pretty"
    d.mkdir()
    (d / "A.kicad_mod").write_text(_MOD_V6)
    (d / "B.kicad_mod").write_text(_MOD_LEGACY)
    lib = footprint_lib.read_pretty(d)
    assert sorted(f.name for f in lib.footprints) == ["R_0402", "R_LEGACY"]
    assert any("LEGACY_FORMAT" in w for w in lib.warnings)


# --------------------------------------------------------------------------- #
# EMPTY_IMPORT / --strict
# --------------------------------------------------------------------------- #
def test_empty_import_warns_and_strict_fails(tmp_path, capsys):
    p = tmp_path / "empty.kicad_sch"
    p.write_text('(kicad_sch (version 20230121) (generator eeschema))')
    assert main(["read", str(p)]) == 0
    assert "EMPTY_IMPORT" in capsys.readouterr().err
    assert main(["read", str(p), "--strict"]) == 1


def test_non_empty_read_has_no_empty_import(capsys):
    assert main(["read", str(FIX / "t_junction.SchDoc")]) == 0
    assert "EMPTY_IMPORT" not in capsys.readouterr().err
