"""Footprint-library readers: KiCad ``.kicad_mod`` / ``.pretty`` and Altium ``.PcbLib``.

All three entry points normalize into :class:`model.Library` carrying
:class:`model.FootprintDef` items (schema 1.2). Coordinates are **mm** in the
footprint's own frame (KiCad convention, +Y down); Altium's 1/10000-mil integers
are converted, and its +Y-up frame is flipped so both sources agree.

Fail-loudly rules (the whole point of this module):

* an unrecognized record type inside a ``.PcbLib`` footprint stops that
  footprint's decode with an ``UNSUPPORTED_PRIMITIVE`` warning — primitives are
  never silently dropped;
* decoded-but-not-modelled primitives (silkscreen tracks/arcs, text, fills,
  regions, 3D bodies) are counted per type and surfaced as warnings;
* a container that yields zero footprints from a non-empty source is the
  caller's ``EMPTY_IMPORT`` case (see ``commands._shared``).

Pad-record framing follows the same empirically-validated block structure as
``altium_pcb_bin.parse_pads`` (KiCad's pcbnew Altium plugin and altium2kicad
document the same offsets); this module re-walks it because a ``.PcbLib`` data
stream is heterogeneous (pads interleaved with graphics), unlike ``Pads6``.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from .. import model
from ..errors import fail
from .altium_pcb_bin import UNIT_MIL
from . import sexpr

__all__ = ["read_kicad_mod", "read_pretty", "read_pcblib", "parse_footprint_node"]

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

    fp = model.FootprintDef(name=_av(node, 1) or "")
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
    for child in node.children or ():
        if not child.is_list or child.tag == "pad":
            continue
        layer_node = child.find("layer") if child.tag else None
        lval = _av(layer_node, 1) if layer_node is not None else None
        if lval and lval.endswith(".CrtYd"):
            fp.courtyard = True
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


def _parse_footprint_stream(data: bytes, storage: str) -> model.FootprintDef:
    """Decode one footprint storage's ``Data`` stream."""
    name_block, pos = _read_block(data, 0, storage)
    fp = model.FootprintDef(name=_pascal(name_block) or storage)
    fp.attributes.append("smd")   # refined below when a plated hole appears
    skipped: dict[int, int] = {}
    n = len(data)
    while pos < n:
        rtype = data[pos]
        pos += 1
        if rtype == _T_PAD:
            pad, pos = _parse_pad_at(data, pos, storage)
            fp.pads.append(pad)
            continue
        if rtype == _T_TEXT:
            _, pos = _read_block(data, pos, storage)
            _, pos = _read_block(data, pos, storage)   # trailing string block
            skipped[rtype] = skipped.get(rtype, 0) + 1
            continue
        if rtype in _ONE_BLOCK_TYPES:
            _, pos = _read_block(data, pos, storage)
            skipped[rtype] = skipped.get(rtype, 0) + 1
            continue
        fp.warnings.append(
            f"UNSUPPORTED_PRIMITIVE: unknown record type 0x{rtype:02x} at "
            f"offset {pos - 1}; {n - pos} byte(s) not decoded")
        break
    for rtype, count in sorted(skipped.items()):
        fp.warnings.append(
            f"UNSUPPORTED_PRIMITIVE: {count} {_TYPE_LABELS[rtype]} record(s) "
            "skipped (graphics/3D are not modelled)")
    if any(p.pad_type == "thru_hole" for p in fp.pads):
        fp.attributes = ["through_hole"]
    return fp


# Storages that are container bookkeeping, not footprints.
_PCBLIB_META_STORAGES = {
    "Library", "FileVersionInfo", "Textures", "UniqueIdPrimitiveInformation",
}


def read_pcblib(path: os.PathLike | str) -> model.Library:
    """Read an Altium ``.PcbLib`` into a :class:`model.Library` (footprints)."""
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

    for storage in sorted(storages):
        try:
            fp = _parse_footprint_stream(storages[storage], storage)
        except Exception as exc:
            lib.warnings.append(f"{storage}: undecodable footprint ({exc})")
            continue
        lib.footprints.append(fp)
        lib.warnings.extend(f"{fp.name}: {w}" for w in fp.warnings)
    return lib
