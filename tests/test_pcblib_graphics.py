"""`.PcbLib` footprint graphics: tracks and arcs -> ``fp_line``/``fp_arc``/``fp_circle``.

The arbiter is KiCad's own Altium importer (``kicad-cli fp upgrade`` converts a
``.PcbLib`` into a ``.pretty``), reached at four distances:

1. **Synthetic records** (every platform): the builders in
   ``tests/test_footprint_lib.py`` write track/arc records in the real payload
   layout, so framing, decoding, the layer map, the writer, ``--layer-map`` and
   determinism are tested without any real library.
2. **Frozen oracle** (every platform): ``tests/fixtures/kicad/altium_import/``
   holds what kicad-cli 10.0.4 wrote for the synthetic library built by
   :func:`_oracle_library`. akcli's import of that library must agree with it
   pad for pad and, per KiCad layer, shape for shape.
3. **Live oracle** (needs ``kicad-cli``): the same comparison against the
   installed KiCad, plus proof that the frozen files have not rotted and that
   KiCad loads what akcli writes without losing a line or an arc.
4. **Real libraries** (needs ``kicad-cli`` and ``AKCLI_PCBLIB_SAMPLE``): real
   vendor libraries cannot be committed, so a developer supplies them::

       AKCLI_PCBLIB_SAMPLE=/path/to/file.PcbLib \\
           PYTHONPATH=src python -m pytest tests/test_pcblib_graphics.py -q

   The variable names one ``.PcbLib`` or a directory searched recursively for
   them. Every library is converted both ways and compared per footprint: the
   pad set (number, position, size, drill) and, per KiCad layer, the multiset
   of lines, arcs and circles (points and width within :data:`TOL`).

Where akcli deliberately does NOT follow KiCad's importer, the difference is
pinned by :func:`test_frozen_oracle_documents_what_akcli_skips`.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from akcli import model, safety, sexpr
from akcli.drivers import kicad_cli
from akcli.readers import altium_pcb_bin, footprint_lib
from akcli.writers import footprint_mod
from tests import test_footprint_lib as B
from tests._cli_harness import run_cli

ORACLE = Path(__file__).resolve().parent / "fixtures" / "kicad" / "altium_import"

MIL = 10000                 # Altium units per mil
MM = 0.0254                 # mm per mil
# KiCad's importer snaps every Altium coordinate to 10 nm; akcli keeps the
# exact value at 1 nm. Two snapped terms meet in an arc point, hence 20 nm.
TOL = 2e-5

needs_cli = pytest.mark.skipif(kicad_cli.find() is None,
                               reason="kicad-cli not installed")
_TIMEOUT = 180.0


# --------------------------------------------------------------------------- #
# the synthetic library both oracles are run on
# --------------------------------------------------------------------------- #
# Every Altium layer id akcli maps by default, with the KiCad layer KiCad's
# importer puts a track on (kicad-cli 10.0.4; frozen in AKCLI_LAYERS.kicad_mod).
KICAD_IMPORTER_LAYERS: dict[int, str] = {
    1: "F.Cu", 32: "B.Cu", 33: "F.SilkS", 34: "B.SilkS", 35: "F.Paste",
    36: "B.Paste", 37: "F.Mask", 38: "B.Mask", 55: "Dwgs.User", 73: "Dwgs.User",
    **{i: f"In{i - 1}.Cu" for i in range(2, 32)},
    **{i: f"User.{i - 56}" for i in range(57, 73)},
}

# (start, end) angles of the arcs in AKCLI_GFX: quadrants, a reflex arc, one
# crossing 0 degrees, fractional angles, and two spellings of a full turn.
ARC_ANGLES = [(0.0, 90.0), (90.0, 0.0), (270.0, 90.0), (45.0, 180.0),
              (350.0, 10.0), (120.5, 301.25), (0.0, 360.0), (30.0, 30.0)]


def _gfx_records() -> bytes:
    """Pads plus every kind of graphic akcli imports."""
    track, arc = B._track_record, B._arc_record
    records = [
        B._pad_record("1", x=-50 * MIL, sx=40 * MIL, sy=60 * MIL, kicad=True),
        B._pad_record("2", x=50 * MIL, sx=40 * MIL, sy=60 * MIL, kicad=True),
        B._pad_record("3", y=-150 * MIL, sx=70 * MIL, sy=70 * MIL,
                      hole=35 * MIL, shape=1, layer=74, kicad=True),
        # silkscreen outline, then one segment off every grid
        track(33, -100 * MIL, 80 * MIL, 100 * MIL, 80 * MIL, 6 * MIL),
        track(33, 100 * MIL, 80 * MIL, 100 * MIL, -80 * MIL, 6 * MIL),
        track(33, 100 * MIL, -80 * MIL, -100 * MIL, -80 * MIL, 6 * MIL),
        track(33, -100 * MIL, -80 * MIL, -100 * MIL, 80 * MIL, 6 * MIL),
        track(33, 123457, -765433, 987651, 314159, 78743),
        track(34, -90 * MIL, 70 * MIL, 90 * MIL, 70 * MIL, 8 * MIL),
        track(1, -50 * MIL, 0, 50 * MIL, 0, 10 * MIL),          # copper
        track(32, -50 * MIL, 20 * MIL, 50 * MIL, 20 * MIL, 10 * MIL),
        track(69, -110 * MIL, 90 * MIL, 110 * MIL, 90 * MIL, 4 * MIL),
        track(69, 110 * MIL, 90 * MIL, 110 * MIL, -90 * MIL, 4 * MIL),
        track(71, -120 * MIL, 100 * MIL, 120 * MIL, 100 * MIL, 2 * MIL),
        track(57, -120 * MIL, 110 * MIL, 120 * MIL, 110 * MIL, 2 * MIL,
              long_layer=0x01020011),                           # Mechanical17
    ]
    for i, (sa, ea) in enumerate(ARC_ANGLES):
        records.append(arc(33, (i - 4) * 100 * MIL, 300 * MIL, 30 * MIL,
                           sa, ea, 6 * MIL))
    records += [
        arc(33, 123457, -765433, 271828, 12.25, 200.75, 31415),
        arc(34, 0, -300 * MIL, 40 * MIL, 0.0, 180.0, 8 * MIL),
        arc(1, 200 * MIL, -300 * MIL, 25 * MIL, 10.0, 200.0, 10 * MIL),
        arc(69, -200 * MIL, -300 * MIL, 50 * MIL, 0.0, 360.0, 4 * MIL),
        arc(71, 400 * MIL, -300 * MIL, 50 * MIL, 200.0, 340.0, 2 * MIL),
    ]
    return b"".join(records)


def _layers_records() -> bytes:
    """One track per Altium layer id akcli maps, told apart by its x."""
    return B._pad_record("1", y=-100 * MIL, kicad=True) + b"".join(
        B._track_record(layer, layer * 10 * MIL, 0, layer * 10 * MIL, 20 * MIL,
                        5 * MIL)
        for layer in sorted(KICAD_IMPORTER_LAYERS))


def _skip_records() -> bytes:
    """One drawn segment plus everything akcli declines to import."""
    track, arc = B._track_record, B._arc_record
    return b"".join([
        B._pad_record("1", kicad=True),
        track(33, -100 * MIL, 80 * MIL, 100 * MIL, 80 * MIL, 6 * MIL),
        track(56, 0, 0, 0, 100 * MIL, 4 * MIL, keepout=True),   # as real files
        track(56, 50 * MIL, 0, 50 * MIL, 100 * MIL, 4 * MIL),
        track(33, 100 * MIL, 0, 100 * MIL, 100 * MIL, 4 * MIL, keepout=True),
        arc(33, 200 * MIL, 0, 30 * MIL, 0.0, 90.0, 4 * MIL, keepout=True),
        track(74, 300 * MIL, 0, 300 * MIL, 100 * MIL, 4 * MIL),  # MultiLayer
        track(39, 400 * MIL, 0, 400 * MIL, 100 * MIL, 4 * MIL),  # InternalPlane1
        track(57, 500 * MIL, 0, 500 * MIL, 100 * MIL, 4 * MIL,
              long_layer=0x0102002E),                            # Mechanical46
    ])


_ORACLE_FOOTPRINTS = {
    "AKCLI_GFX": _gfx_records, "AKCLI_LAYERS": _layers_records,
    "AKCLI_SKIP": _skip_records,
}


def _oracle_library(path: Path) -> Path:
    """The synthetic library the frozen fixtures were converted from."""
    return B._write_pcblib(
        path, {f"FP{i}": B._footprint_stream(name, records())
               for i, (name, records) in enumerate(_ORACLE_FOOTPRINTS.items())},
        kicad=True)


def _one(tmp_path: Path, records: bytes, **read_kw) -> model.FootprintDef:
    p = B._write_pcblib(tmp_path / "one.PcbLib",
                        {"FP": B._footprint_stream("FP", records)})
    return footprint_lib.read_pcblib(p, **read_kw).footprints[0]


# --------------------------------------------------------------------------- #
# comparison: akcli's footprint vs KiCad's
# --------------------------------------------------------------------------- #
def _shape(g: dict) -> tuple[str, tuple[float, ...]]:
    """A graphic as (kind, numbers), blind to drawing direction. KiCad writes
    a full-turn arc whose angles are not exactly 0..360 as an arc that starts
    and ends on one point; that is a circle, and compared as one."""
    if g["kind"] == "line":
        a, b = sorted([tuple(g["start"]), tuple(g["end"])])
        return "line", (*a, *b, g["width"])
    if g["kind"] == "arc":
        s, m, e = tuple(g["start"]), tuple(g["mid"]), tuple(g["end"])
        if math.dist(s, e) <= TOL:
            return "circle", ((s[0] + m[0]) / 2, (s[1] + m[1]) / 2,
                              math.dist(s, m) / 2, g["width"])
        a, b = sorted([s, e])
        return "arc", (*a, *m, *b, g["width"])
    return "circle", (*g["center"], g["radius"], g["width"])


def _pair_off(ours: list[tuple], theirs: list[tuple]) -> tuple[list, list]:
    """Greedy nearest matching within TOL -> (ours unmatched, theirs unmatched)."""
    left = list(theirs)
    missing = []
    for item in ours:
        best = min(range(len(left)), default=None,
                   key=lambda i: max(abs(p - q) for p, q in zip(item, left[i])))
        if best is not None and max(
                abs(p - q) for p, q in zip(item, left[best])) <= TOL:
            left.pop(best)
        else:
            missing.append(item)
    return missing, left


def _differences(ours: model.FootprintDef, theirs: model.FootprintDef) -> list[str]:
    """Every disagreement between akcli's footprint and KiCad's, as text."""
    out: list[str] = []

    def pads(fp: model.FootprintDef) -> dict[str, list[tuple]]:
        table: dict[str, list[tuple]] = {}
        for p in fp.pads:
            table.setdefault(p.number, []).append(
                (p.x_mm, p.y_mm, p.size_x_mm, p.size_y_mm, p.drill_mm or 0.0))
        return table

    mine, kicad = pads(ours), pads(theirs)
    for number in sorted(set(mine) | set(kicad)):
        a, b = _pair_off(mine.get(number, []), kicad.get(number, []))
        if a or b:
            out.append(f"pad {number}: akcli-only {a}, KiCad-only {b}")

    def shapes(fp: model.FootprintDef) -> dict[tuple[str, str], list[tuple]]:
        table: dict[tuple[str, str], list[tuple]] = {}
        for g in fp.graphics:
            kind, numbers = _shape(g)
            table.setdefault((g["layer"], kind), []).append(numbers)
        return table

    mine_g, kicad_g = shapes(ours), shapes(theirs)
    # KiCad copies a MultiLayer graphic onto every copper layer; akcli skips
    # it with a warning. Only then may KiCad hold copper graphics akcli lacks.
    fanned_out = any("on MultiLayer skipped" in w for w in ours.warnings)
    for key in sorted(set(mine_g) | set(kicad_g)):
        a, b = _pair_off(mine_g.get(key, []), kicad_g.get(key, []))
        if b and not a and fanned_out and key[0].endswith(".Cu"):
            continue
        if a or b:
            out.append(f"{key[1]}s on {key[0]}: akcli-only {a}, KiCad-only {b}")
    return out


def _kicad_convert(src: Path, out: Path) -> bool:
    """``kicad-cli fp upgrade``: a ``.PcbLib`` (or ``.pretty``) -> ``out``."""
    exe = kicad_cli.find()
    assert exe is not None
    proc = safety.run_subprocess(
        [exe, "fp", "upgrade", "--force", "--output", str(out), str(src)],
        _TIMEOUT)
    return proc.returncode == 0 and any(out.glob("*.kicad_mod"))


def _assert_parity(src: Path, kicad_dir: Path) -> int:
    """akcli's read of ``src`` vs KiCad's conversion in ``kicad_dir``; returns
    the number of footprints compared."""
    def fold(name: str) -> str:
        return "".join(c for c in name.casefold() if c.isalnum())

    kicad = footprint_lib.read_pretty(kicad_dir).footprints
    by_name = {fp.name: fp for fp in kicad}
    by_fold = {fold(fp.name): fp for fp in kicad}
    problems = []
    ours = footprint_lib.read_pcblib(src).footprints
    for index, fp in enumerate(ours):
        theirs = by_name.get(fp.name) or by_fold.get(fold(fp.name))
        if theirs is None:
            problems.append(f"footprint #{index}: absent from KiCad's output")
            continue
        problems += [f"footprint #{index}: {d}" for d in _differences(fp, theirs)]
    assert not problems, "\n".join(problems)
    return len(ours)


# --------------------------------------------------------------------------- #
# 1. synthetic records: decoding
# --------------------------------------------------------------------------- #
def test_track_becomes_a_line_in_the_kicad_frame(tmp_path):
    fp = _one(tmp_path, B._pad_record("1") + B._track_record(
        33, -100 * MIL, 80 * MIL, 100 * MIL, -80 * MIL, 6 * MIL))
    assert fp.graphics == [{
        "kind": "line", "layer": "F.SilkS", "source_layer": "TopOverlay",
        "start": (-2.54, -2.032), "end": (2.54, 2.032),   # +Y up -> +Y down
        "width": 0.1524}]
    assert not fp.warnings


def test_both_wild_track_payload_sizes_decode(tmp_path):
    for size in (33, 45, 49):
        fp = _one(tmp_path, B._track_record(33, 0, 0, 10 * MIL, 0, 6 * MIL,
                                            size=size))
        assert fp.graphics[0]["end"] == (0.254, 0.0)


@pytest.mark.parametrize("sa, ea, start, mid, end", [
    # KiCad's importer, kicad-cli 10.0.4, 30-mil radius at the origin
    (0.0, 90.0, (0, -0.762), (0.538815, -0.538815), (0.762, 0)),
    (90.0, 0.0, (0.762, 0), (-0.538815, 0.538815), (0, -0.762)),
    (270.0, 90.0, (0, -0.762), (0.762, 0), (0, 0.762)),
    (180.0, 0.0, (0.762, 0), (0, 0.762), (-0.762, 0)),
])
def test_arc_runs_counter_clockwise_from_start_to_end(tmp_path, sa, ea,
                                                      start, mid, end):
    """The classic mirrored-arc trap. Altium sweeps counter-clockwise from the
    start angle in a +Y-up frame; KiCad's triple starts at the Altium END
    angle. `mid` is what pins the side of the chord the arc bulges to."""
    (g,) = _one(tmp_path, B._arc_record(33, 0, 0, 30 * MIL, sa, ea)).graphics
    assert g["kind"] == "arc" and g["layer"] == "F.SilkS"
    for got, want in ((g["start"], start), (g["mid"], mid), (g["end"], end)):
        assert got == pytest.approx(want, abs=1e-6)


@pytest.mark.parametrize("sa, ea", [(0.0, 360.0), (30.0, 30.0), (10.0, 370.0),
                                    (0.0, 0.0)])
def test_full_turn_arc_is_a_circle(tmp_path, sa, ea):
    (g,) = _one(tmp_path, B._arc_record(
        33, 100 * MIL, 50 * MIL, 20 * MIL, sa, ea, 4 * MIL)).graphics
    assert g == {"kind": "circle", "layer": "F.SilkS",
                 "source_layer": "TopOverlay", "center": (2.54, -1.27),
                 "radius": 0.508, "width": 0.1016}


def test_default_layer_map_is_kicads(tmp_path):
    for layer, kicad in sorted(KICAD_IMPORTER_LAYERS.items()):
        (g,) = _one(tmp_path, B._track_record(layer)).graphics
        assert g["layer"] == kicad, layer
        assert g["source_layer"] == altium_pcb_bin.LAYER_NAMES[layer]
    assert set(footprint_lib.DEFAULT_LAYER_MAP.values()) <= \
        footprint_lib.KICAD_FOOTPRINT_LAYERS


def test_mechanical_layer_lands_on_a_user_layer_with_a_warning(tmp_path):
    fp = _one(tmp_path, B._track_record(71) + B._track_record(71)
              + B._arc_record(69) + B._track_record(33))
    assert [g["layer"] for g in fp.graphics] == [
        "User.15", "User.15", "User.13", "F.SilkS"]
    warned = [w for w in fp.warnings if w.startswith("MECHANICAL_LAYER")]
    assert len(warned) == 2
    assert "1 graphic(s) on Mechanical13 placed on User.13" in warned[0]
    assert "2 graphic(s) on Mechanical15 placed on User.15" in warned[1]
    assert "--layer-map Mechanical15=<KiCad layer>" in warned[1]


def test_layer_map_overrides_and_silences_the_warning(tmp_path):
    fp = _one(tmp_path, B._track_record(71) + B._track_record(69),
              layer_map={"Mechanical15": "F.CrtYd"})
    assert [g["layer"] for g in fp.graphics] == ["F.CrtYd", "User.13"]
    assert fp.courtyard is True
    assert [w.split(":")[0] for w in fp.warnings] == ["MECHANICAL_LAYER"]
    assert "Mechanical13" in fp.warnings[0]


def test_long_layer_id_names_mechanical_layers_past_16(tmp_path):
    fp = _one(tmp_path,
              B._track_record(57, long_layer=0x01020011)       # Mechanical17
              + B._track_record(57, long_layer=0x0102002D)     # Mechanical45
              + B._track_record(57, long_layer=0x0102002E)     # Mechanical46
              + B._track_record(57, long_layer=0x01020010)     # u8 wins: Mech1
              + B._arc_record(57, long_layer=0x01020011))
    assert [(g["source_layer"], g["layer"]) for g in fp.graphics] == [
        ("Mechanical17", "User.17"), ("Mechanical45", "User.45"),
        ("Mechanical1", "User.1"), ("Mechanical17", "User.17")]
    assert any("1 track record(s) on Mechanical46 skipped" in w
               and "--layer-map Mechanical46=<KiCad layer>" in w
               for w in fp.warnings)


def test_keepout_and_unmapped_layers_are_skipped_loudly(tmp_path):
    p = B._write_pcblib(tmp_path / "skip.PcbLib",
                        {"FP": B._footprint_stream("FP", _skip_records())})
    lib, (counts,) = footprint_lib.read_pcblib_report(p)
    fp = lib.footprints[0]
    assert [g["layer"] for g in fp.graphics] == ["F.SilkS"]
    assert counts == {"imported": {"pad": 1, "track": 1},
                      "skipped": {"arc": 1, "track": 6}}
    text = "\n".join(fp.warnings)
    for needle in ("2 track record(s) on KeepOut skipped",
                   "1 track record(s) on TopOverlay skipped (keepout regions",
                   "1 arc record(s) on TopOverlay skipped (keepout regions",
                   "1 track record(s) on MultiLayer skipped",
                   "1 track record(s) on InternalPlane1 skipped",
                   "1 track record(s) on Mechanical46 skipped"):
        assert needle in text, needle
    assert all(w.startswith("UNSUPPORTED_PRIMITIVE") for w in fp.warnings)
    # the applied map names every layer met, and says which were skipped
    assert lib.metadata["layer_map"] == {
        "InternalPlane1": None, "KeepOut": None, "Mechanical46": None,
        "MultiLayer": None, "TopOverlay": "F.SilkS"}
    assert lib.metadata["primitives"] == counts


def test_layer_map_can_draw_a_layer_with_no_default(tmp_path):
    fp = _one(tmp_path, _skip_records(),
              layer_map={"KeepOut": "Cmts.User", "MultiLayer": "F.Cu"})
    assert sorted(g["layer"] for g in fp.graphics) == [
        "Cmts.User", "Cmts.User", "F.Cu", "F.SilkS"]
    # a keepout object on an ordinary layer is never drawn as that layer
    assert any("on TopOverlay skipped (keepout regions" in w for w in fp.warnings)


def test_unknown_layer_id_is_skipped_not_guessed(tmp_path):
    fp = _one(tmp_path, B._track_record(90) + B._track_record(33))
    assert len(fp.graphics) == 1
    assert any("1 track record(s) on 90 skipped (unknown Altium layer id)" in w
               for w in fp.warnings)


def test_bad_payload_costs_one_primitive_not_the_footprint(tmp_path):
    short = bytes([0x04]) + B._block(b"\x21" * 20)
    fp = _one(tmp_path, B._pad_record("1") + short + B._track_record(33)
              + B._arc_record(33, start=float("nan"), end=90.0)
              + B._pad_record("2"))
    assert [p.number for p in fp.pads] == ["1", "2"]
    assert len(fp.graphics) == 1
    assert any("payload too short: 20 byte(s)" in w for w in fp.warnings)
    assert any("non-finite arc angle" in w for w in fp.warnings)


def test_other_record_types_stay_skipped(tmp_path):
    def one_block(rtype: int) -> bytes:
        return bytes([rtype]) + B._block(b"\x00" * 24)

    p = B._write_pcblib(tmp_path / "rest.PcbLib", {"FP": B._footprint_stream(
        "FP", B._pad_record("1") + B._text_record() + one_block(0x06)
        + one_block(0x0B) + one_block(0x0C) + one_block(0x03)
        + B._track_record(33))})
    lib, (counts,) = footprint_lib.read_pcblib_report(p)
    assert counts["skipped"] == {"component-body": 1, "fill": 1, "region": 1,
                                 "text": 1, "via": 1}
    assert counts["imported"] == {"pad": 1, "track": 1}
    assert sum("only pads, tracks and arcs are imported" in w
               for w in lib.footprints[0].warnings) == 5


def test_altium_layer_names_are_canonicalised():
    canon = footprint_lib.canonical_altium_layer
    assert canon("topoverlay") == "TopOverlay"
    assert canon(" MECHANICAL15 ") == "Mechanical15"
    assert canon("Mechanical200") == "Mechanical200"       # long-id layers
    for bad in ("", "Mechanical", "Mechanical0", "Mechanical015", "Mech15",
                "TopLayer", "F.SilkS", "Mechanical65536"):
        assert canon(bad) is None, bad


def test_read_json_carries_graphics_additively(tmp_path):
    p = B._write_pcblib(tmp_path / "g.PcbLib", {"FP": B._footprint_stream(
        "FP", B._pad_record("1") + B._track_record(33) + B._arc_record(71))})
    rc, out, err = run_cli(["read", str(p), "--json"])
    assert rc == 0
    doc = json.loads(out)
    assert doc["schema_version"] == "2.1"            # additive: minor bump
    fp = doc["footprints"][0]
    assert [g["kind"] for g in fp["graphics"]] == ["line", "circle"]
    assert fp["graphics"][0]["start"] == [0.0, 0.0]
    assert doc["metadata"]["layer_map"] == {"Mechanical15": "User.15",
                                            "TopOverlay": "F.SilkS"}
    assert doc["metadata"]["primitives"] == {
        "imported": {"arc": 1, "pad": 1, "track": 1}, "skipped": {}}
    assert "MECHANICAL_LAYER" in err


# --------------------------------------------------------------------------- #
# 1. synthetic records: writer + CLI
# --------------------------------------------------------------------------- #
def test_writer_emits_kicad_grammar():
    fp = model.FootprintDef(name="G", graphics=[
        {"kind": "line", "layer": "F.SilkS", "source_layer": "TopOverlay",
         "start": (-1.0, -0.5), "end": (1.0, -0.5), "width": 0.12},
        {"kind": "arc", "layer": "B.SilkS", "source_layer": "BottomOverlay",
         "start": (0.0, -0.381), "mid": (0.269408, -0.269408),
         "end": (0.381, 0.0), "width": 0.1524},
        {"kind": "circle", "layer": "User.13", "source_layer": "Mechanical13",
         "center": (2.0, 1.0), "radius": 0.5, "width": 0.1},
    ])
    assert footprint_mod.to_kicad_mod(fp) == (
        '(footprint "G"\n'
        "\t(version 20240108)\n"
        '\t(generator "akcli")\n'
        '\t(layer "F.Cu")\n'
        "\t(attr smd)\n"
        "\t(fp_line\n"
        "\t\t(start -1 -0.5)\n"
        "\t\t(end 1 -0.5)\n"
        "\t\t(stroke\n\t\t\t(width 0.12)\n\t\t\t(type solid)\n\t\t)\n"
        '\t\t(layer "F.SilkS")\n'
        "\t)\n"
        "\t(fp_arc\n"
        "\t\t(start 0 -0.381)\n"
        "\t\t(mid 0.269408 -0.269408)\n"
        "\t\t(end 0.381 0)\n"
        "\t\t(stroke\n\t\t\t(width 0.1524)\n\t\t\t(type solid)\n\t\t)\n"
        '\t\t(layer "B.SilkS")\n'
        "\t)\n"
        "\t(fp_circle\n"
        "\t\t(center 2 1)\n"
        "\t\t(end 2 0.5)\n"
        "\t\t(stroke\n\t\t\t(width 0.1)\n\t\t\t(type solid)\n\t\t)\n"
        "\t\t(fill none)\n"
        '\t\t(layer "User.13")\n'
        "\t)\n"
        ")\n")


def test_written_graphics_read_back_identically(tmp_path):
    """Writer and `.kicad_mod` reader agree on the same model."""
    src = _oracle_library(tmp_path / "syn.PcbLib")
    out = tmp_path / "syn.pretty"
    assert run_cli(["library", "import-altium", str(src), "-o", str(out),
                    "--apply"])[0] == 0
    back = {fp.name: fp for fp in footprint_lib.read_pretty(out).footprints}
    for fp in footprint_lib.read_pcblib(src).footprints:
        assert not _differences(fp, back[fp.name])
        assert len(back[fp.name].graphics) == len(fp.graphics)


def test_import_plan_and_provenance_record_map_and_counts(tmp_path):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    out = tmp_path / "syn.pretty"
    argv = ["library", "import-altium", str(src), "-o", str(out),
            "--layer-map", "mechanical15=F.CrtYd,Mechanical13=F.Fab",
            "--layer-map", "KeepOut=Cmts.User", "--courtyard", "0.25"]

    rc, text, _ = run_cli(argv)                                  # dry-run plan
    assert rc == 0 and not out.exists()
    assert "Mechanical15 -> F.CrtYd" in text and "MultiLayer -> skipped" in text
    assert "AKCLI_GFX  pads=3 tracks=12 arcs=13 skipped=0" in text
    assert "AKCLI_SKIP  pads=1 tracks=3 arcs=0 skipped=5" in text

    rc, text, _ = run_cli([*argv, "--apply", "--json"])
    assert rc == 0
    doc = json.loads(text)
    rows = {row["name"]: row for row in doc["footprints"]}
    assert rows["AKCLI_GFX"]["primitives"] == {
        "imported": {"arc": 13, "pad": 3, "track": 12}, "skipped": {}}
    assert rows["AKCLI_SKIP"]["primitives"] == {
        "imported": {"pad": 1, "track": 3}, "skipped": {"arc": 1, "track": 4}}
    assert doc["layer_map"]["Mechanical15"] == "F.CrtYd"
    assert doc["layer_map"]["KeepOut"] == "Cmts.User"
    assert doc["layer_map"]["MultiLayer"] is None
    assert list(doc["layer_map"]) == sorted(doc["layer_map"])

    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert prov["options"] == {"courtyard_mm": 0.25, "layer_map": {
        "KeepOut": "Cmts.User", "Mechanical13": "F.Fab",
        "Mechanical15": "F.CrtYd"}}
    assert prov["layer_map"] == doc["layer_map"]
    assert prov["footprints"] == doc["footprints"]

    warnings = "\n".join(doc["warnings"])
    assert "Mechanical15 placed" not in warnings        # mapped: no warning
    assert "Mechanical17 placed on User.17" in warnings   # unmapped: warned
    # the mapped courtyard is the footprint's courtyard: nothing synthesized
    assert "AKCLI_GFX: courtyard synthesized" not in warnings
    assert "AKCLI_LAYERS: courtyard synthesized" not in warnings
    assert "AKCLI_SKIP: courtyard synthesized" in warnings
    gfx = (out / "AKCLI_GFX.kicad_mod").read_text(encoding="utf-8")
    assert gfx.count('(layer "F.CrtYd")') == 2 and "User.15" not in gfx


@pytest.mark.parametrize("value, needle", [
    ("Mechanical15", "expected ALTIUM=KICAD"),
    ("=F.Fab", "expected ALTIUM=KICAD"),
    ("Mechanical15=F.Fab,", "expected ALTIUM=KICAD"),
    ("Mech15=F.Fab", "unknown Altium layer 'Mech15'"),
    ("TopLayer=F.Cu", "unknown Altium layer 'TopLayer'"),
    ("Mechanical15=F.Courtyard", "'F.Courtyard' is not a KiCad footprint layer"),
    ("Mechanical15=f.crtyd", "'f.crtyd' is not a KiCad footprint layer"),
    ("Mechanical15=F.Fab,mechanical15=F.CrtYd", "Mechanical15 is mapped twice"),
])
def test_layer_map_usage_errors(tmp_path, value, needle):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    out = tmp_path / "syn.pretty"
    rc, text, err = run_cli(["library", "import-altium", str(src), "-o",
                             str(out), "--layer-map", value, "--apply",
                             "--json"])
    assert rc == 2
    assert needle in err and needle in json.loads(text)["error"]["message"]
    assert not out.exists()                              # nothing written


def test_layer_map_accepts_a_repeated_identical_pair(tmp_path):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    rc, _, _ = run_cli(["library", "import-altium", str(src), "-o",
                        str(tmp_path / "o.pretty"), "--layer-map",
                        "Mechanical15=F.Fab", "--layer-map",
                        " mechanical15 = F.Fab "])
    assert rc == 0


def test_layer_map_entry_that_matches_nothing_is_reported(tmp_path):
    """Mapping Mechanical14 when the courtyard sits on 15 must not look done."""
    src = B._write_pcblib(tmp_path / "one.PcbLib", {"FP": B._footprint_stream(
        "FP", B._pad_record("1") + B._track_record(71))})
    rc, text, _ = run_cli(["library", "import-altium", str(src), "-o",
                           str(tmp_path / "o.pretty"), "--json", "--layer-map",
                           "Mechanical14=F.CrtYd"])
    assert rc == 0
    warnings = json.loads(text)["warnings"]
    assert ("--layer-map Mechanical14=F.CrtYd: no track or arc on "
            "Mechanical14 in this library") in warnings
    assert any("Mechanical15 placed on User.15" in w for w in warnings)


def test_import_with_graphics_is_byte_deterministic(tmp_path):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    out = tmp_path / "syn.pretty"
    argv = ["library", "import-altium", str(src), "-o", str(out), "--apply",
            "--layer-map", "Mechanical15=F.CrtYd"]
    assert run_cli(argv)[0] == 0
    first = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
    assert sorted(first) == ["AKCLI_GFX.kicad_mod", "AKCLI_LAYERS.kicad_mod",
                             "AKCLI_SKIP.kicad_mod", "provenance.json"]
    assert run_cli(argv)[0] == 0                         # re-import in place
    assert {p.name: p.read_bytes() for p in sorted(out.iterdir())} == first
    assert all(b"\r" not in blob for blob in first.values())
    # a second directory differs only where the output path is recorded
    other = tmp_path / "other.pretty"
    assert run_cli([*argv[:4], str(other), *argv[5:]])[0] == 0
    for name, blob in first.items():
        if name.endswith(".kicad_mod"):
            assert (other / name).read_bytes() == blob


# --------------------------------------------------------------------------- #
# 2. frozen oracle: what kicad-cli 10.0.4 wrote for the synthetic library
# --------------------------------------------------------------------------- #
def test_frozen_oracle_agrees_with_akcli(tmp_path):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    assert sorted(p.stem for p in ORACLE.glob("*.kicad_mod")) == sorted(
        _ORACLE_FOOTPRINTS)
    assert _assert_parity(src, ORACLE) == 3


def test_frozen_oracle_pins_the_arc_convention(tmp_path):
    """Not a tautology on akcli's own maths: these are KiCad's numbers."""
    kicad = {fp.name: fp for fp in footprint_lib.read_pretty(ORACLE).footprints}
    arcs = [g for g in kicad["AKCLI_GFX"].graphics
            if g["kind"] == "arc" and g["layer"] == "F.SilkS"]
    # the (0, 90) arc: 30-mil radius centred 400 mil left, 300 mil up
    cx, cy, r = -400 * MM, -300 * MM, 30 * MM
    (quarter,) = [g for g in arcs
                  if g["start"] == pytest.approx((cx, cy - r), abs=TOL)]
    assert quarter["end"] == pytest.approx((cx + r, cy), abs=TOL)
    assert quarter["mid"][0] > cx and quarter["mid"][1] < cy   # up-right bulge
    # exactly (0, 360) is KiCad's only fp_circle; (30, 30) is a closed arc
    silk = (ORACLE / "AKCLI_GFX.kicad_mod").read_text(encoding="utf-8")
    assert silk.count("(fp_circle") == 2 and silk.count("(fill no)") == 2


def test_frozen_oracle_documents_what_akcli_skips(tmp_path):
    """The deliberate differences from KiCad's importer, as KiCad wrote them:
    keepout objects become rule areas, a MultiLayer graphic is copied onto
    all 32 copper layers, an internal-plane track becomes a rule area on no
    layer at all, and a mechanical layer past 45 disappears. akcli skips all
    four and says so (test_keepout_and_unmapped_layers_are_skipped_loudly)."""
    root = sexpr.parse((ORACLE / "AKCLI_SKIP.kicad_mod").read_text(
        encoding="utf-8"))
    layers = [n.find("layer").children[1].value for n in root.find_all("fp_line")]
    assert layers.count("F.SilkS") == 1
    copper = [name for name in layers if name.endswith(".Cu")]
    assert len(copper) == 32 and len(set(copper)) == 32
    assert len(layers) == 33                             # nothing else drawn
    assert not root.find_all("fp_arc")                   # the keepout arc
    zones = root.find_all("zone")
    assert len(zones) == 5 and all(z.find("keepout") is not None for z in zones)
    on = sorted((z.find("layers") or z.find("layer")).children[1].value
                for z in zones)
    assert on == ["F.Cu", "F.Cu", "F.SilkS", "F.SilkS", "UNDEFINED"]


# --------------------------------------------------------------------------- #
# 3. live oracle: the installed KiCad
# --------------------------------------------------------------------------- #
@needs_cli
def test_live_kicad_importer_agrees_with_akcli(tmp_path):
    src = _oracle_library(tmp_path / "syn.PcbLib")
    live = tmp_path / "kicad.pretty"
    assert _kicad_convert(src, live), "KiCad refused the synthetic library"
    assert _assert_parity(src, live) == 3
    # ...and the committed fixtures are still what KiCad writes
    frozen = {fp.name: fp for fp in footprint_lib.read_pretty(ORACLE).footprints}
    for fp in footprint_lib.read_pretty(live).footprints:
        assert not _differences(fp, frozen[fp.name]), fp.name
        assert len(fp.graphics) == len(frozen[fp.name].graphics)


@needs_cli
def test_kicad_loads_what_akcli_writes(tmp_path):
    """`fp upgrade` re-saves akcli's `.kicad_mod` files: KiCad must accept
    every one and keep every line, arc, circle and pad where akcli put it."""
    src = _oracle_library(tmp_path / "syn.PcbLib")
    ours = tmp_path / "akcli.pretty"
    assert run_cli(["library", "import-altium", str(src), "-o", str(ours),
                    "--apply", "--layer-map", "Mechanical15=F.CrtYd"])[0] == 0
    (ours / "provenance.json").unlink()        # not part of a KiCad library
    resaved = tmp_path / "resaved.pretty"
    assert _kicad_convert(ours, resaved), "KiCad refused akcli's output"
    after = {fp.name: fp for fp in footprint_lib.read_pretty(resaved).footprints}
    before = footprint_lib.read_pretty(ours).footprints
    assert sorted(after) == sorted(fp.name for fp in before)
    for fp in before:
        kinds = [g["kind"] for g in fp.graphics]
        assert sorted(g["kind"] for g in after[fp.name].graphics) == sorted(kinds)
        assert not _differences(fp, after[fp.name]), fp.name


# --------------------------------------------------------------------------- #
# 4. real libraries (developer-supplied; never committed)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.environ.get("AKCLI_PCBLIB_SAMPLE"),
                    reason="set AKCLI_PCBLIB_SAMPLE to a real .PcbLib (or a "
                           "directory of them)")
def test_real_libraries_match_kicads_importer(tmp_path):
    if kicad_cli.find() is None:
        pytest.skip("kicad-cli not installed")
    sample = Path(os.environ["AKCLI_PCBLIB_SAMPLE"])
    libraries = ([sample] if sample.is_file() else sorted(
        p for p in sample.rglob("*") if p.suffix.lower() == ".pcblib"))
    assert libraries, f"no .PcbLib under {sample}"
    compared = 0
    for index, lib in enumerate(libraries):
        out = tmp_path / f"kicad{index}.pretty"
        if not _kicad_convert(lib, out):
            continue                      # KiCad cannot read it: no oracle
        compared += _assert_parity(lib, out)
    assert compared, "KiCad converted none of the sample libraries"
