"""Footprint-library readers: KiCad ``.kicad_mod`` / ``.pretty`` and Altium ``.PcbLib``.

All three entry points normalize into :class:`model.Library` carrying
:class:`model.FootprintDef` items (schema 1.2). Coordinates are **mm** in the
footprint's own frame (KiCad convention, +Y down); Altium's 1/10000-mil integers
are converted, and its +Y-up frame is flipped so both sources agree.

A ``.PcbLib`` footprint decodes its pads, tracks and arcs; tracks and arcs
become ``FootprintDef.graphics`` (lines, arcs, circles) on the KiCad layer the
layer map gives them (:data:`DEFAULT_LAYER_MAP`, matched to KiCad's own Altium
importer; a caller's ``layer_map`` overrides it per Altium layer).

Fail-loudly rules (the whole point of this module):

* an unrecognized record type inside a ``.PcbLib`` footprint stops that
  footprint's decode with an ``UNSUPPORTED_PRIMITIVE`` warning — primitives are
  never silently dropped;
* primitives that are not imported (text, fills, regions, 3D bodies, vias,
  keepout objects, graphics on a layer with no KiCad target) are counted per
  type and surfaced as warnings;
* a mechanical layer has no fixed meaning, so graphics that land on its
  default ``User.N`` layer raise a ``MECHANICAL_LAYER`` warning naming it;
* a container that yields zero footprints from a non-empty source is the
  caller's ``EMPTY_IMPORT`` case (see ``commands._shared``).

Pad-record framing follows the same empirically-validated block structure as
``altium_pcb_bin.parse_pads`` (KiCad's pcbnew Altium plugin and altium2kicad
document the same offsets); this module re-walks it because a ``.PcbLib`` data
stream is heterogeneous (pads interleaved with graphics), unlike ``Pads6``.
Track and arc payloads are decoded by the shared
``altium_pcb_bin.decode_track`` / ``decode_arc``.
"""

from __future__ import annotations

import math
import os
import struct
from collections.abc import Mapping
from pathlib import Path

from .. import model
from ..errors import AkcliError, fail
from ..kicad_escape import unescape_string
from . import altium_pcb_bin as _bin
from .altium_pcb_bin import UNIT_MIL
from .. import sexpr

__all__ = ["read_kicad_mod", "read_pretty", "read_pcblib", "read_pcblib_report",
           "parse_footprint_node", "DEFAULT_LAYER_MAP", "KICAD_FOOTPRINT_LAYERS",
           "canonical_altium_layer"]

_MIL_TO_MM = 0.0254

# .PcbLib record-type bytes (same ids as the .PcbDoc sections).
_T_ARC = 0x01
_T_PAD = 0x02
_T_VIA = 0x03
_T_TRACK = 0x04
_T_TEXT = 0x05
_T_FILL = 0x06
_T_REGION = 0x0B
_T_BODY = 0x0C

# One length-prefixed block after the type byte; TEXT carries a second
# (string) block. Pads are block-structured and handled separately.
_ONE_BLOCK_TYPES = {_T_ARC, _T_VIA, _T_TRACK, _T_FILL, _T_REGION, _T_BODY}
_TYPE_LABELS = {
    _T_ARC: "arc", _T_VIA: "via", _T_TRACK: "track", _T_TEXT: "text",
    _T_FILL: "fill", _T_REGION: "region", _T_BODY: "component-body",
}

_PAD_SHAPES = {1: "circle", 2: "rect", 3: "octagon", 9: "roundrect"}

# Layer names a footprint graphic may sit on, as KiCad writes them. User.10
# and up exist from KiCad 9; KiCad 8 stops at User.9.
KICAD_FOOTPRINT_LAYERS: frozenset[str] = frozenset(
    ["F.Cu", "B.Cu", "F.Adhes", "B.Adhes", "F.Paste", "B.Paste", "F.SilkS",
     "B.SilkS", "F.Mask", "B.Mask", "Dwgs.User", "Cmts.User", "Eco1.User",
     "Eco2.User", "Edge.Cuts", "Margin", "F.CrtYd", "B.CrtYd", "F.Fab", "B.Fab"]
    + [f"In{i}.Cu" for i in range(1, 31)]
    + [f"User.{i}" for i in range(1, 46)])

# Altium layer name -> KiCad layer for track/arc graphics. Every row is what
# KiCad's own importer does (kicad-cli 10.0.4 `fp upgrade` on synthetic
# one-primitive-per-layer libraries; frozen in tests/fixtures/kicad/altium_import).
# Deliberately absent, so their primitives are skipped with a warning unless
# the caller maps them: KeepOut (KiCad builds keepout rule areas),
# MultiLayer (KiCad copies the graphic onto all 32 copper layers),
# InternalPlaneN (KiCad emits zones on an undefined layer) and mechanical
# layers past 45 (KiCad drops them).
DEFAULT_LAYER_MAP: dict[str, str] = {
    "Top": "F.Cu", "Bottom": "B.Cu",
    "TopOverlay": "F.SilkS", "BottomOverlay": "B.SilkS",
    "TopPaste": "F.Paste", "BottomPaste": "B.Paste",
    "TopSolder": "F.Mask", "BottomSolder": "B.Mask",
    "DrillGuide": "Dwgs.User", "DrillDrawing": "Dwgs.User",
}
DEFAULT_LAYER_MAP.update({f"Mid{i}": f"In{i}.Cu" for i in range(1, 31)})
DEFAULT_LAYER_MAP.update({f"Mechanical{i}": f"User.{i}" for i in range(1, 46)})

_ALTIUM_LAYERS_BY_FOLD = {name.casefold(): name for name in _bin.LAYER_NAMES.values()}
_MECHANICAL_MAX = 0xFFFF     # the long layer id keeps the number in 16 bits


def canonical_altium_layer(name: str) -> str | None:
    """Canonical spelling of an Altium layer name (case-insensitive), or None.

    The vocabulary is ``altium_pcb_bin.LAYER_NAMES`` plus ``MechanicalN`` for
    the mechanical layers only a long layer id can name.
    """
    folded = name.strip().casefold()
    known = _ALTIUM_LAYERS_BY_FOLD.get(folded)
    if known is not None:
        return known
    if folded.startswith("mechanical"):
        digits = folded[len("mechanical"):]
        if digits.isascii() and digits.isdigit() and not digits.startswith("0") \
                and int(digits) <= _MECHANICAL_MAX:
            return f"Mechanical{int(digits)}"
    return None


def _mm(raw: int) -> float:
    """Altium 1/10000-mil integer -> mm."""
    return raw * UNIT_MIL * _MIL_TO_MM


# --------------------------------------------------------------------------- #
# KiCad .kicad_mod / .pretty
# --------------------------------------------------------------------------- #
def parse_footprint_node(node: sexpr.SNode) -> model.FootprintDef:
    """Normalize one parsed ``(footprint ...)`` / legacy ``(module ...)`` node."""
    def _av(n: sexpr.SNode | None, idx: int) -> str | None:
        if n is not None and n.children and 0 <= idx < len(n.children):
            c = n.children[idx]
            if c.is_atom:
                return c.value
        return None

    def _fnum(n: sexpr.SNode | None, idx: int) -> float:
        v = _av(n, idx)
        try:
            return float(v) if v is not None else 0.0
        except ValueError:
            return 0.0

    fp = model.FootprintDef(name=unescape_string(_av(node, 1)) or "")
    fp.format_version = _av(node.find("version"), 1)
    if node.tag == "module" and fp.format_version is None:
        fp.warnings.append(
            "LEGACY_FORMAT: pre-v6 `(module ...)` footprint — parseable via API "
            "but may be invisible to the KiCad footprint browser; re-save with "
            "KiCad 7+ before relying on it")
    fp.layer = _av(node.find("layer"), 1) or "F.Cu"
    for attr in node.find_all("attr"):
        for c in (attr.children or ())[1:]:
            if c.is_atom and c.value:
                fp.attributes.append(c.value)
    for m in node.find_all("model"):
        p = _av(m, 1)
        if p:
            fp.models.append(p)
    def _point(n: sexpr.SNode, tag: str) -> tuple[float, float] | None:
        at = n.find(tag)
        if at is None or _av(at, 1) is None or _av(at, 2) is None:
            return None
        return (_fnum(at, 1), _fnum(at, 2))

    def _graphic(n: sexpr.SNode, layer: str) -> dict | None:
        """``fp_line`` / ``fp_arc`` / ``fp_circle`` -> a ``graphics`` item.
        Other shapes, and the pre-v6 centre+angle arc, are not modelled."""
        stroke = n.find("stroke")
        width = _fnum(stroke.find("width") if stroke is not None
                      else n.find("width"), 1)
        head = {"kind": "", "layer": layer, "source_layer": None}
        if n.tag == "fp_line":
            a, b = _point(n, "start"), _point(n, "end")
            if a is None or b is None:
                return None
            return {**head, "kind": "line", "start": a, "end": b, "width": width}
        if n.tag == "fp_arc":
            a, m, b = _point(n, "start"), _point(n, "mid"), _point(n, "end")
            if a is None or m is None or b is None:
                return None
            return {**head, "kind": "arc", "start": a, "mid": m, "end": b,
                    "width": width}
        if n.tag == "fp_circle":
            c, e = _point(n, "center"), _point(n, "end")
            if c is None or e is None:
                return None
            return {**head, "kind": "circle", "center": c,
                    "radius": math.hypot(e[0] - c[0], e[1] - c[1]),
                    "width": width}
        return None

    for child in node.children or ():
        if not child.is_list or child.tag == "pad":
            continue
        layer_node = child.find("layer") if child.tag else None
        lval = _av(layer_node, 1) if layer_node is not None else None
        if lval and lval.endswith(".CrtYd"):
            fp.courtyard = True
        if lval and child.tag in ("fp_line", "fp_arc", "fp_circle"):
            graphic = _graphic(child, lval)
            if graphic is not None:
                fp.graphics.append(graphic)
    for pad in node.find_all("pad"):
        number = _av(pad, 1) or ""
        pad_type = _av(pad, 2) or "smd"
        shape = _av(pad, 3) or "rect"
        at = pad.find("at")
        size = pad.find("size")
        layers_node = pad.find("layers")
        layers = [c.value for c in (layers_node.children or ())[1:]
                  if c.is_atom and c.value] if layers_node is not None else []
        drill = pad.find("drill")
        drill_mm = None
        if drill is not None:
            v = _av(drill, 1)
            if v == "oval":
                v = _av(drill, 2)
            try:
                drill_mm = float(v) if v is not None else None
            except ValueError:
                drill_mm = None
        fp.pads.append(model.FootprintPad(
            number=number,
            x_mm=_fnum(at, 1), y_mm=_fnum(at, 2),
            size_x_mm=_fnum(size, 1), size_y_mm=_fnum(size, 2),
            shape=shape, pad_type=pad_type, layers=layers,
            drill_mm=drill_mm, rotation=_fnum(at, 3),
        ))
    return fp


def read_kicad_mod(path: os.PathLike | str) -> model.Library:
    """Read one ``.kicad_mod`` file into a one-footprint :class:`model.Library`."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    root = sexpr.parse(text)
    if root.tag not in ("footprint", "module"):
        fail("ALTIUM_MALFORMED",
             f"{p.name}: not a KiCad footprint (root tag {root.tag!r})")
    fp = parse_footprint_node(root)
    if not fp.name:
        fp.name = p.stem
    lib = model.Library(source_path=str(p), source_format="kicad",
                        symbols=[], footprints=[fp])
    lib.warnings.extend(fp.warnings)
    return lib


def read_pretty(path: os.PathLike | str) -> model.Library:
    """Read a KiCad ``.pretty`` directory into a :class:`model.Library`."""
    p = Path(path)
    lib = model.Library(source_path=str(p), source_format="kicad", symbols=[])
    for mod in sorted(p.glob("*.kicad_mod")):
        try:
            one = read_kicad_mod(mod)
        except Exception as exc:  # keep auditing the rest of the library
            lib.warnings.append(f"{mod.name}: unreadable ({exc})")
            continue
        lib.footprints.extend(one.footprints)
        lib.warnings.extend(f"{mod.name}: {w}" for w in one.warnings)
    return lib


# --------------------------------------------------------------------------- #
# Altium .PcbLib
# --------------------------------------------------------------------------- #
def _read_block(buf: bytes, pos: int, what: str) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        fail("ALTIUM_MALFORMED", f"{what}: truncated block length at {pos}")
    (length,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    if pos + length > len(buf):
        fail("ALTIUM_MALFORMED", f"{what}: block overruns stream at {pos}")
    return buf[pos:pos + length], pos + length


def _pascal(block: bytes) -> str:
    if not block:
        return ""
    n = block[0]
    return block[1:1 + n].decode("latin-1", "replace")


def _pad_layers(layer: int, hole_mm: float | None, plated: bool) -> tuple[str, list[str]]:
    """Map an Altium pad layer id onto (pad_type, KiCad-style layer set)."""
    if hole_mm and hole_mm > 0:
        if not plated:
            return "np_thru_hole", ["*.Cu", "*.Mask"]
        return "thru_hole", ["*.Cu", "*.Mask"]
    if layer == 32:
        return "smd", ["B.Cu", "B.Paste", "B.Mask"]
    return "smd", ["F.Cu", "F.Paste", "F.Mask"]


def _parse_pad_at(buf: bytes, pos: int, name: str) -> tuple[model.FootprintPad, int]:
    """Decode one pad record starting AFTER its type byte; return (pad, new pos)."""
    name_block, pos = _read_block(buf, pos, name)
    pad_name = _pascal(name_block)
    for _ in range(3):                       # reserved blocks
        _, pos = _read_block(buf, pos, name)
    geo, pos = _read_block(buf, pos, name)
    if len(geo) < 61:
        fail("ALTIUM_MALFORMED", f"{name}: pad geometry block too short")
    layer = geo[0]
    x = struct.unpack_from("<i", geo, 13)[0]
    y = struct.unpack_from("<i", geo, 17)[0]
    sx = struct.unpack_from("<i", geo, 21)[0]
    sy = struct.unpack_from("<i", geo, 25)[0]
    hole = struct.unpack_from("<i", geo, 45)[0]
    shape = _PAD_SHAPES.get(geo[49], str(geo[49]))
    (rotation,) = struct.unpack_from("<d", geo, 52)
    plated = bool(geo[60])
    hole_mm = _mm(hole) if hole else None
    pad_type, layers = _pad_layers(layer, hole_mm, plated)
    pad = model.FootprintPad(
        number=pad_name,
        x_mm=round(_mm(x), 6), y_mm=round(-_mm(y), 6),   # Altium +Y up -> KiCad +Y down
        size_x_mm=round(_mm(sx), 6), size_y_mm=round(_mm(sy), 6),
        shape=shape, pad_type=pad_type, layers=layers,
        drill_mm=round(hole_mm, 6) if hole_mm else None,
        rotation=rotation,
    )
    # Optional extended block (newer AD): present when the next byte cannot
    # start a known record. A zero-length block reads as 4x 0x00.
    if pos < len(buf):
        nxt = buf[pos]
        if nxt not in _ONE_BLOCK_TYPES and nxt not in (_T_PAD, _T_TEXT):
            _, pos = _read_block(buf, pos, name)
    return pad, pos


def _xy(x: int, y: int) -> tuple[float, float]:
    """Altium (x, y) -> footprint frame: mm, +Y down (never ``-0.0``)."""
    return (round(_mm(x), 6) + 0.0, round(-_mm(y), 6) + 0.0)


def _track_graphic(t: _bin.TrackPayload, layer: str, source: str) -> dict:
    return {"kind": "line", "layer": layer, "source_layer": source,
            "start": _xy(t.x1, t.y1), "end": _xy(t.x2, t.y2),
            "width": round(_mm(t.width), 6)}


def _arc_graphic(a: _bin.ArcPayload, layer: str, source: str) -> dict:
    """An Altium arc runs counter-clockwise from its start angle to its end
    angle, in a +Y-up frame. A full turn is a circle; anything else becomes
    the KiCad ``start``/``mid``/``end`` triple, which KiCad's importer anchors
    at the Altium END angle (so ``end`` is the Altium start)."""
    head = {"layer": layer, "source_layer": source}
    width = round(_mm(a.width), 6)
    cx, cy, r = _mm(a.cx), _mm(a.cy), _mm(a.radius)
    sweep = (a.angle_end - a.angle_start) % 360.0
    if sweep == 0.0:
        return {"kind": "circle", **head, "center": _xy(a.cx, a.cy),
                "radius": round(r, 6), "width": width}

    def at(deg: float) -> tuple[float, float]:
        rad = math.radians(deg)
        return (round(cx + r * math.cos(rad), 6) + 0.0,
                round(-(cy + r * math.sin(rad)), 6) + 0.0)

    return {"kind": "arc", **head, "start": at(a.angle_end),
            "mid": at(a.angle_start + sweep / 2.0), "end": at(a.angle_start),
            "width": width}


def _target_layer(source: str, keepout: bool,
                  layer_map: Mapping[str, str]) -> tuple[str | None, str]:
    """``(KiCad layer or None, why)`` for a track/arc on Altium layer ``source``."""
    if keepout and source != "KeepOut":
        return None, "keepout"
    if source in layer_map:
        return layer_map[source], "mapped"
    target = DEFAULT_LAYER_MAP.get(source)
    if target is None:
        return None, "keepout" if source == "KeepOut" else "no-layer"
    return target, "mechanical" if source.startswith("Mechanical") else "default"


def _skip_reason(why: str, source: str) -> str:
    if why == "keepout":
        if source == "KeepOut":
            return ("keepout regions are not imported; `library import-altium "
                    "--layer-map KeepOut=<KiCad layer>` draws their outline "
                    "instead")
        return "keepout regions are not imported"
    if canonical_altium_layer(source) is None:
        return "unknown Altium layer id"
    return (f"no default KiCad layer; choose one with `library import-altium "
            f"--layer-map {source}=<KiCad layer>`")


def _parse_footprint_stream(
        data: bytes, storage: str,
        layer_map: Mapping[str, str] | None = None,
) -> tuple[model.FootprintDef, dict, dict[str, str | None]]:
    """Decode one footprint storage's ``Data`` stream.

    Returns ``(footprint, counts, layers)``: ``counts`` is
    ``{"imported": {type: n}, "skipped": {type: n}}`` over the Altium
    primitive types, ``layers`` the Altium layer -> KiCad layer (or None when
    skipped) of every track/arc layer the footprint uses.
    """
    user_map = layer_map or {}
    name_block, pos = _read_block(data, 0, storage)
    fp = model.FootprintDef(name=_pascal(name_block) or storage)
    fp.attributes.append("smd")   # refined below when a plated hole appears
    imported: dict[str, int] = {}
    skipped: dict[str, int] = {}
    layers: dict[str, str | None] = {}
    not_imported: dict[int, int] = {}                 # whole record types
    dropped: dict[tuple[str, str, str], int] = {}     # (type, layer, reason)
    mechanical: dict[tuple[str, str], int] = {}       # (Altium, KiCad) layer

    def _bump(table: dict[str, int], label: str) -> None:
        table[label] = table.get(label, 0) + 1

    def _drop(label: str, source: str, reason: str) -> None:
        key = (label, source, reason)
        dropped[key] = dropped.get(key, 0) + 1
        _bump(skipped, label)

    n = len(data)
    while pos < n:
        rtype = data[pos]
        pos += 1
        if rtype == _T_PAD:
            pad, pos = _parse_pad_at(data, pos, storage)
            fp.pads.append(pad)
            _bump(imported, "pad")
            continue
        if rtype == _T_TEXT:
            _, pos = _read_block(data, pos, storage)
            _, pos = _read_block(data, pos, storage)   # trailing string block
            not_imported[rtype] = not_imported.get(rtype, 0) + 1
            _bump(skipped, _TYPE_LABELS[rtype])
            continue
        if rtype in (_T_TRACK, _T_ARC):
            label = _TYPE_LABELS[rtype]
            payload, pos = _read_block(data, pos, storage)
            rec: _bin.TrackPayload | _bin.ArcPayload
            try:
                rec = (_bin.decode_track(payload, storage) if rtype == _T_TRACK
                       else _bin.decode_arc(payload, storage))
            except AkcliError:
                # the framing is intact, so one bad payload costs one
                # primitive, not the footprint
                _drop(label, "", f"payload too short: {len(payload)} byte(s)")
                continue
            if isinstance(rec, _bin.ArcPayload) and not (
                    math.isfinite(rec.angle_start) and math.isfinite(rec.angle_end)):
                _drop(label, "", "non-finite arc angle")
                continue
            source = _bin.layer_name(rec.layer, rec.layer_v7)
            target, why = _target_layer(source, rec.keepout, user_map)
            if why != "keepout" or source == "KeepOut":
                layers[source] = target
            if target is None:
                _drop(label, source, _skip_reason(why, source))
                continue
            fp.graphics.append(
                _track_graphic(rec, target, source)
                if isinstance(rec, _bin.TrackPayload)
                else _arc_graphic(rec, target, source))
            _bump(imported, label)
            if why == "mechanical":
                mechanical[(source, target)] = mechanical.get((source, target), 0) + 1
            if target.endswith(".CrtYd"):
                fp.courtyard = True
            continue
        if rtype in _ONE_BLOCK_TYPES:
            _, pos = _read_block(data, pos, storage)
            not_imported[rtype] = not_imported.get(rtype, 0) + 1
            _bump(skipped, _TYPE_LABELS[rtype])
            continue
        fp.warnings.append(
            f"UNSUPPORTED_PRIMITIVE: unknown record type 0x{rtype:02x} at "
            f"offset {pos - 1}; {n - pos} byte(s) not decoded")
        break
    for rtype, count in sorted(not_imported.items()):
        fp.warnings.append(
            f"UNSUPPORTED_PRIMITIVE: {count} {_TYPE_LABELS[rtype]} record(s) "
            "skipped (only pads, tracks and arcs are imported)")
    for (label, source, reason), count in sorted(dropped.items()):
        where = f" on {source}" if source else ""
        fp.warnings.append(
            f"UNSUPPORTED_PRIMITIVE: {count} {label} record(s){where} skipped "
            f"({reason})")
    for (source, target), count in sorted(mechanical.items()):
        fp.warnings.append(
            f"MECHANICAL_LAYER: {count} graphic(s) on {source} placed on "
            f"{target} (a mechanical layer has no fixed meaning; choose its "
            f"KiCad layer with `library import-altium --layer-map "
            f"{source}=<KiCad layer>`)")
    if any(p.pad_type == "thru_hole" for p in fp.pads):
        fp.attributes = ["through_hole"]
    counts = {"imported": dict(sorted(imported.items())),
              "skipped": dict(sorted(skipped.items()))}
    return fp, counts, layers


# Storages that are container bookkeeping, not footprints.
_PCBLIB_META_STORAGES = {
    "Library", "FileVersionInfo", "Textures", "UniqueIdPrimitiveInformation",
}


def read_pcblib(path: os.PathLike | str, *,
                layer_map: Mapping[str, str] | None = None) -> model.Library:
    """Read an Altium ``.PcbLib`` into a :class:`model.Library` (footprints).

    ``layer_map`` (Altium layer name -> KiCad layer) overrides
    :data:`DEFAULT_LAYER_MAP` per layer; names are taken as given, so validate
    them first (:func:`canonical_altium_layer`, :data:`KICAD_FOOTPRINT_LAYERS`).
    ``metadata["layer_map"]`` records the mapping that was applied to every
    track/arc layer in the library and ``metadata["primitives"]`` the
    imported/skipped totals per Altium primitive type.
    """
    return read_pcblib_report(path, layer_map=layer_map)[0]


def read_pcblib_report(
        path: os.PathLike | str, *,
        layer_map: Mapping[str, str] | None = None,
) -> tuple[model.Library, list[dict]]:
    """:func:`read_pcblib` plus one ``{"imported": .., "skipped": ..}`` count
    row per footprint, aligned with ``Library.footprints``."""
    from . import _cfbf  # lazy: OLE container walk

    p = Path(path)
    streams = _cfbf.read_streams_qualified(p)
    lib = model.Library(source_path=str(p), source_format="altium", symbols=[])
    lib.metadata["container_streams"] = len(streams)

    storages: dict[str, bytes] = {}
    for key, blob in streams.items():
        if "/" not in key:
            continue
        top, rest = key.split("/", 1)
        if top in _PCBLIB_META_STORAGES:
            continue
        if rest == "Data":
            storages[top] = blob

    if not storages:
        fail("ALTIUM_UNSUPPORTED",
             f"{p.name}: no footprint storages found — not a PcbLib, or an "
             "unsupported container layout (nothing was silently imported)")

    rows: list[dict] = []
    applied: dict[str, str | None] = {}
    totals: dict[str, dict[str, int]] = {"imported": {}, "skipped": {}}
    for storage in sorted(storages):
        try:
            fp, counts, layers = _parse_footprint_stream(
                storages[storage], storage, layer_map)
        except Exception as exc:
            lib.warnings.append(f"{storage}: undecodable footprint ({exc})")
            continue
        lib.footprints.append(fp)
        lib.warnings.extend(f"{fp.name}: {w}" for w in fp.warnings)
        rows.append(counts)
        applied.update(layers)   # a layer's target never varies by footprint
        for bucket, table in counts.items():
            for label, count in table.items():
                totals[bucket][label] = totals[bucket].get(label, 0) + count
    lib.metadata["layer_map"] = dict(sorted(applied.items()))
    lib.metadata["primitives"] = {k: dict(sorted(v.items()))
                                  for k, v in totals.items()}
    return lib, rows
