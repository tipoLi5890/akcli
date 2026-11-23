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


def _props(text: str) -> bytes:
    """A length-prefixed, NUL-terminated ``|KEY=VALUE|...`` property string."""
    raw = text.encode("latin-1") + b"\x00"
    return struct.pack("<I", len(raw)) + raw


def _pad_record(number: str, *, x=0, y=0, sx=12000, sy=8000, hole=0,
                shape=2, rotation=0.0, plated=1, layer=1, kicad=False) -> bytes:
    """One type-0x02 pad record (offsets mirror altium_pcb_bin.parse_pads).

    ``kicad=True`` writes the pad the way real libraries do — a 120-byte
    geometry block (mid/bottom size and shape filled in) followed by the
    per-layer block — which is the least KiCad's own importer accepts.
    """
    geo = bytearray(120 if kicad else 61)
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
    tail = b""
    if kicad:
        struct.pack_into("<iiii", geo, 29, sx, sy, sx, sy)   # mid + bottom size
        geo[50] = geo[51] = shape
        tail = _block(b"")                                   # per-layer block
    return (bytes([0x02]) + _block(_pascal(number))
            + _block(b"") * 3 + _block(bytes(geo)) + tail)


def _primitive_head(size: int, layer: int, keepout: bool) -> bytearray:
    """The 13 bytes every track/arc payload starts with, as real files write
    them: layer, flags, keepout marker, then net/polygon/component = none."""
    p = bytearray(size)
    p[0] = layer
    p[1] = 0x0C
    p[2] = 2 if keepout else 0
    struct.pack_into("<HHH", p, 3, 0xFFFF, 0xFFFF, 0xFFFF)
    p[9:13] = b"\xff\xff\xff\xff"
    return p


def _long_layer(layer: int) -> int:
    """The 32-bit layer id newer files append: 0x0102_0000 + N for a
    mechanical layer (u8 ids 57..72 = Mechanical1..16), left unset otherwise."""
    return 0x01020000 + (layer - 56) if 57 <= layer <= 72 else 0


def _track_record(layer=33, x1=0, y1=0, x2=10000, y2=0, width=1000, *,
                  keepout=False, long_layer=None, size=49) -> bytes:
    """One type-0x04 track record in the real payload layout (45 or 49 bytes
    in the wild): start/end s32@13..28, width s32@29, long layer id u32@41."""
    p = _primitive_head(size, layer, keepout)
    struct.pack_into("<iiii", p, 13, x1, y1, x2, y2)
    struct.pack_into("<i", p, 29, width)
    if size >= 45:
        struct.pack_into("<I", p, 41,
                         _long_layer(layer) if long_layer is None else long_layer)
    return bytes([0x04]) + _block(bytes(p))


def _arc_record(layer=33, cx=0, cy=0, radius=10000, start=0.0, end=360.0,
                width=1000, *, keepout=False, long_layer=None, size=60) -> bytes:
    """One type-0x01 arc record in the real payload layout (60 bytes in the
    wild): centre s32@13/17, radius s32@21, start/end angle f64@25/33
    (degrees, counter-clockwise), width s32@41, long layer id u32@52."""
    p = _primitive_head(size, layer, keepout)
    struct.pack_into("<iii", p, 13, cx, cy, radius)
    struct.pack_into("<dd", p, 25, start, end)
    struct.pack_into("<i", p, 41, width)
    if size >= 56:
        struct.pack_into("<I", p, 52,
                         _long_layer(layer) if long_layer is None else long_layer)
    return bytes([0x01]) + _block(bytes(p))


def _text_record() -> bytes:
    """A type-0x05 text record: a geometry block plus its string block."""
    return bytes([0x05]) + _block(b"\x00" * 40) + _block(_pascal("TXT"))


def _footprint_stream(name: str, records: bytes) -> bytes:
    return _block(_pascal(name)) + records


def _write_pcblib(path: Path, storages: dict[str, bytes], *,
                  kicad: bool = False) -> Path:
    """Pack footprint streams into a ``.PcbLib`` container.

    ``kicad=True`` adds what KiCad's own importer needs before it will convert
    the library (`kicad-cli fp upgrade`): a ``Library/Data`` index of the
    footprint names and a ``Parameters`` stream per footprint.
    """
    streams = {"Library/Data": b"\x00" * 32}
    names = []
    for storage, data in storages.items():
        streams[f"{storage}/Data"] = data
        name = data[5:5 + data[4]].decode("latin-1")
        names.append(name)
        if kicad:
            streams[f"{storage}/Parameters"] = _props(
                f"|PATTERN={name}|HEIGHT=0mil|DESCRIPTION=|ITEMGUID="
                "|REVISIONGUID=")
    if kicad:
        index = _props("|KIND=Protel_Advanced_PCB_Library|VERSION=3.00")
        index += struct.pack("<I", len(names))
        streams["Library/Data"] = index + b"".join(
            _block(_pascal(name)) for name in names)
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
# content vs extension (the .PcbLib-saved-as-.SchDoc trap, second half)
# --------------------------------------------------------------------------- #
_SCHLIB_HEADER = (b"|HEADER=Protel for Windows - Schematic Library Editor "
                  b"Binary File Version 5.0|")


def test_pcblib_bytes_with_schdoc_extension_read_as_pcblib(tmp_path, capsys):
    """A DECISIVE container layout beats a lying extension — with a warning,
    never silently, and never through the wrong decoder."""
    p = _write_pcblib(tmp_path / "renamed.SchDoc",
                      {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    assert _detect_format_ex(p) == ("altium_pcblib", "content-over-extension")
    assert "extension says altium_sch" in capsys.readouterr().err

    assert main(["read", str(p), "--json"]) == 0
    io = capsys.readouterr()
    doc = json.loads(io.out)
    assert doc["metadata"]["detected_format"] == "altium_pcblib"
    assert doc["metadata"]["detection_method"] == "content-over-extension"
    assert [f["name"] for f in doc["footprints"]] == ["FP1"]
    assert "renamed.SchDoc" in io.err and "altium_pcblib" in io.err


def test_matching_content_keeps_the_extension_method(tmp_path, capsys):
    assert _detect_format_ex(FIX / "t_junction.SchDoc") == (
        "altium_sch", "extension")
    p = _write_pcblib(tmp_path / "part.PcbLib",
                      {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    assert _detect_format_ex(p) == ("altium_pcblib", "extension")
    assert capsys.readouterr().err == ""          # agreement is silent


def test_corrupt_ole_with_schdoc_extension_keeps_the_structured_error(
        tmp_path, capsys):
    """An unreadable container is not DECISIVE evidence of anything, so the
    extension still routes it to the reader that reports ALTIUM_* (exit 3)."""
    blob, _ = cfbf_builder.build_cfbf({"FileHeader": b"\x00" * 64})
    p = tmp_path / "truncated.SchDoc"
    p.write_bytes(blob[:600])                    # header sector + a stump
    assert _detect_format_ex(p) == ("altium_sch", "extension")
    assert main(["read", str(p), "--json"]) == 3
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"].startswith("ALTIUM_")

    # same for a hostile-corpus container, and for any Altium extension
    for name in ("fat_cycle.SchDoc", "cycle.PcbLib"):
        q = tmp_path / name
        q.write_bytes(cfbf_builder.malformed_fat_cycle())
        assert _detect_format_ex(q)[1] == "extension"
        assert main(["read", str(q), "--json"]) == 3
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["code"].startswith("ALTIUM_")


def test_unclassifiable_file_header_is_unknown_not_schematic(tmp_path):
    """A FileHeader stream nobody can classify used to be waved through as a
    schematic; bare, it is now `unknown` (exit 5) like any other mystery
    container — while a real extension still decides."""
    blob, _ = cfbf_builder.build_cfbf({"FileHeader": b"\x07\x00\x00\x00mystery"})
    bare = tmp_path / "mystery"
    bare.write_bytes(blob)
    assert _detect_format_ex(bare) == ("unknown", "content")
    assert main(["read", str(bare)]) == 5
    named = tmp_path / "mystery.SchDoc"
    named.write_bytes(blob)
    assert _detect_format_ex(named) == ("altium_sch", "extension")


def test_schlib_symbol_named_like_a_pcb_storage_stays_a_schlib(tmp_path, capsys):
    """A SchLib keeps one storage per symbol, named by the user. A symbol
    called `Library` (or `Board6`) is not a PcbLib/PcbDoc: the schematic
    FileHeader is the stronger evidence, so the extension is NOT overruled."""
    for sym in ("Library", "Board6"):
        blob, _ = cfbf_builder.build_cfbf(
            {"FileHeader": _SCHLIB_HEADER, f"{sym}/Data": b"\x00" * 16})
        named = tmp_path / f"{sym}.SchLib"
        named.write_bytes(blob)
        assert _detect_format_ex(named) == ("altium_schlib", "extension")
        bare = tmp_path / f"{sym}_bare"
        bare.write_bytes(blob)
        assert _detect_format_ex(bare) == ("altium_schlib", "content")
    assert capsys.readouterr().err == ""


def test_missing_altium_file_is_still_not_found(tmp_path, capsys):
    p = tmp_path / "nope.SchDoc"
    assert _detect_format_ex(p) == ("altium_sch", "extension")
    assert main(["read", str(p)]) == 4


def test_detect_format_leaks_no_file_handle(tmp_path):
    """The content sniff used to `path.open("rb").read(64)` and leave the
    handle to the garbage collector (a ResourceWarning per extensionless
    read; on Windows an open handle also blocks a later rename/delete)."""
    import gc
    import warnings

    bare = tmp_path / "sheet_without_extension"
    bare.write_text('(kicad_sch (version 20231120) (generator "t"))',
                    encoding="utf-8")
    ole = _write_pcblib(tmp_path / "noext",
                        {"FP1": _footprint_stream("FP1", _pad_record("1"))})
    gc.collect()                 # garbage older than this test is not ours
    with warnings.catch_warnings(record=True) as caught:
        # "always", not "error": an unclosed file is reported from __del__,
        # where an error filter can only surface as an unraisable exception.
        warnings.simplefilter("always")
        assert _detect_format_ex(bare) == ("kicad_sch", "content")
        assert _detect_format_ex(ole) == ("altium_pcblib", "content")
        assert _detect_format_ex(FIX / "t_junction.SchDoc")[0] == "altium_sch"
        gc.collect()
    # judged by file name, so a leak some other test left for this collection
    # to find cannot fail (or excuse) this one
    ours = (bare.name, ole.name, "t_junction.SchDoc")
    leaks = [str(w.message) for w in caught
             if issubclass(w.category, ResourceWarning)
             and any(name in str(w.message) for name in ours)]
    assert leaks == [], leaks


# --------------------------------------------------------------------------- #
# .PcbLib reader
# --------------------------------------------------------------------------- #
def test_read_pcblib_pads_and_unsupported_primitives(tmp_path):
    records = _pad_record("1", x=10000, y=-20000) + _pad_record("2") + _text_record()
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
    assert any("UNSUPPORTED_PRIMITIVE" in w and "text" in w for w in fp.warnings)


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
    assert doc["schema_version"] == "2.1"
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
