"""Binary ``.PcbDoc`` section decoders: Tracks6 / Vias6 / Arcs6 / Pads6.

Altium stores board copper as packed little-endian records inside per-section
OLE storages (``Tracks6/Data`` etc.). Framing: ``u8 record-type + u32 length +
payload``. Coordinates are **1/10000 mil** integers in the Altium frame
(+Y up); this module emits **mils** (floats) without flipping the axis — the
PCB model keeps the native frame (documented in SPEC).

Layouts were derived empirically from real boards (KiCad's Altium QA corpus)
and cross-validated item-by-item against KiCad's own Altium importer
(``pcbnew``) — see ``tests/test_altium_pcb_bin.py``. An unknown record type
inside a known section fails loudly (``ALTIUM_UNSUPPORTED``) instead of
guessing; a truncated record fails ``ALTIUM_MALFORMED``.

The track and arc PAYLOADS are the same in a ``.PcbLib`` footprint stream, so
their decoders (:func:`decode_track`, :func:`decode_arc`) are shared with
``readers/footprint_lib.py``; only the framing around them differs.

Prior art gratefully acknowledged: thesourcerer8/altium2kicad and KiCad's
``pcbnew`` Altium plugin document the same field offsets.
"""

from __future__ import annotations

import struct
from typing import NamedTuple

from ..errors import fail

__all__ = ["parse_tracks", "parse_vias", "parse_arcs", "parse_pads", "UNIT_MIL",
           "LAYER_NAMES", "TrackPayload", "ArcPayload", "decode_track",
           "decode_arc", "layer_name"]

# One Altium PCB unit = 1/10000 mil.
UNIT_MIL = 1.0 / 10000.0

# Record-type bytes per section (empirical, consistent across samples).
_TYPE_ARC = 0x01
_TYPE_PAD = 0x02
_TYPE_VIA = 0x03
_TYPE_TRACK = 0x04

_NO_NET = 0xFFFF

# Altium layer ids (the u8 at payload offset 0) -> names; the raw id is always
# emitted next to the name. Ids 55..74 are pinned by KiCad's own importer
# (kicad-cli 10.0.4 `fp upgrade` on one-primitive-per-id libraries: 56 becomes
# a keepout rule area, 57..72 land on User.1..User.16, 55 and 73 on Dwgs.User)
# and by the record's own long layer id (see `layer_name`).
LAYER_NAMES = {
    1: "Top", 32: "Bottom", 33: "TopOverlay", 34: "BottomOverlay",
    35: "TopPaste", 36: "BottomPaste", 37: "TopSolder", 38: "BottomSolder",
    55: "DrillGuide", 56: "KeepOut", 73: "DrillDrawing", 74: "MultiLayer",
}
for _i in range(2, 32):
    LAYER_NAMES.setdefault(_i, f"Mid{_i - 1}")
for _i in range(39, 55):
    LAYER_NAMES.setdefault(_i, f"InternalPlane{_i - 38}")
for _i in range(57, 73):
    LAYER_NAMES.setdefault(_i, f"Mechanical{_i - 56}")

# Newer files append a 32-bit layer id to each track/arc payload; a mechanical
# layer is 0x0102_0000 + N. The u8 only reaches Mechanical16, so layers past
# that are named from the long id (as KiCad's importer does: N >= 17 only).
_V7_MECHANICAL = 0x0102
_KEEPOUT_FLAG = 2            # payload byte 2: the primitive is a keepout object


def _records(buf: bytes, section: str):
    """Yield ``(rtype, payload)`` for every framed record in ``buf``."""
    pos, n = 0, len(buf)
    while pos < n:
        if pos + 5 > n:
            fail("ALTIUM_MALFORMED", f"{section}: truncated record header at {pos}")
        rtype = buf[pos]
        (length,) = struct.unpack_from("<I", buf, pos + 1)
        pos += 5
        if pos + length > n:
            fail("ALTIUM_MALFORMED", f"{section}: record overruns stream at {pos}")
        yield rtype, buf[pos:pos + length]
        pos += length


def _mil(v: int) -> float:
    return v * UNIT_MIL


def _net_name(idx: int, nets: list[str]) -> str | None:
    if idx == _NO_NET:
        return None
    return nets[idx] if 0 <= idx < len(nets) else None


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _s32(b: bytes, off: int) -> int:
    return struct.unpack_from("<i", b, off)[0]


class TrackPayload(NamedTuple):
    """One track payload, undecorated: Altium units (1/10000 mil), +Y up."""

    layer: int
    keepout: bool
    net: int
    component: int
    x1: int
    y1: int
    x2: int
    y2: int
    width: int
    layer_v7: int | None       # trailing 32-bit layer id, None when absent


class ArcPayload(NamedTuple):
    """One arc payload, undecorated: Altium units, +Y up, degrees CCW."""

    layer: int
    keepout: bool
    net: int
    component: int
    cx: int
    cy: int
    radius: int
    angle_start: float
    angle_end: float
    width: int
    layer_v7: int | None


def decode_track(p: bytes, what: str = "Tracks6") -> TrackPayload:
    """Decode one track PAYLOAD (the bytes after ``type + u32 length``).

    Shared by the ``.PcbDoc`` ``Tracks6`` section and the per-footprint
    ``.PcbLib`` stream, which frame the same payload differently. Layout:
    layer u8@0, keepout u8@2 (==2), net u16@3, component u16@7,
    x1/y1/x2/y2 s32@13..28, width s32@29, long layer id u32@41 (payloads of
    45 bytes or more).
    """
    if len(p) < 33:
        fail("ALTIUM_MALFORMED", f"{what}: record too short")
    return TrackPayload(
        layer=p[0], keepout=p[2] == _KEEPOUT_FLAG,
        net=_u16(p, 3), component=_u16(p, 7),
        x1=_s32(p, 13), y1=_s32(p, 17), x2=_s32(p, 21), y2=_s32(p, 25),
        width=_s32(p, 29),
        layer_v7=struct.unpack_from("<I", p, 41)[0] if len(p) >= 45 else None,
    )


def decode_arc(p: bytes, what: str = "Arcs6") -> ArcPayload:
    """Decode one arc PAYLOAD (see :func:`decode_track` for the sharing).

    Layout: layer u8@0, keepout u8@2 (==2), net u16@3, component u16@7,
    cx/cy s32@13/17, radius s32@21, start/end angle f64@25/33 (degrees, CCW),
    width s32@41, long layer id u32@52 (payloads of 56 bytes or more).
    """
    if len(p) < 45:
        fail("ALTIUM_MALFORMED", f"{what}: record too short")
    sa, ea = struct.unpack_from("<dd", p, 25)
    return ArcPayload(
        layer=p[0], keepout=p[2] == _KEEPOUT_FLAG,
        net=_u16(p, 3), component=_u16(p, 7),
        cx=_s32(p, 13), cy=_s32(p, 17), radius=_s32(p, 21),
        angle_start=sa, angle_end=ea, width=_s32(p, 41),
        layer_v7=struct.unpack_from("<I", p, 52)[0] if len(p) >= 56 else None,
    )


def layer_name(layer: int, layer_v7: int | None = None) -> str:
    """Altium layer name for a primitive: the u8 id, unless the long id names
    a mechanical layer the u8 cannot express (``Mechanical17`` and up)."""
    if layer_v7 is not None and layer_v7 >> 16 == _V7_MECHANICAL:
        number = layer_v7 & 0xFFFF
        if number > 16:
            return f"Mechanical{number}"
    return LAYER_NAMES.get(layer, str(layer))


def parse_tracks(buf: bytes, nets: list[str]) -> list[dict]:
    """Tracks6: straight copper/graphic segments (payload: :func:`decode_track`)."""
    out = []
    for rtype, p in _records(buf, "Tracks6"):
        if rtype != _TYPE_TRACK:
            fail("ALTIUM_UNSUPPORTED", f"Tracks6: unknown record type 0x{rtype:02x}")
        t = decode_track(p, "Tracks6")
        out.append({
            "layer": t.layer,
            "layer_name": LAYER_NAMES.get(t.layer, str(t.layer)),
            "net": _net_name(t.net, nets),
            "component": None if t.component == _NO_NET else t.component,
            "start": (_mil(t.x1), _mil(t.y1)),
            "end": (_mil(t.x2), _mil(t.y2)),
            "width": _mil(t.width),
        })
    return out


def parse_vias(buf: bytes, nets: list[str]) -> list[dict]:
    """Vias6: plated barrels.

    Payload: net u16@3, component u16@7, x/y s32@13/17, diameter s32@21,
    hole s32@25, start/end layer u8@29/30.
    """
    out = []
    for rtype, p in _records(buf, "Vias6"):
        if rtype != _TYPE_VIA:
            fail("ALTIUM_UNSUPPORTED", f"Vias6: unknown record type 0x{rtype:02x}")
        if len(p) < 31:
            fail("ALTIUM_MALFORMED", "Vias6: record too short")
        comp = _u16(p, 7)
        out.append({
            "net": _net_name(_u16(p, 3), nets),
            "component": None if comp == _NO_NET else comp,
            "at": (_mil(_s32(p, 13)), _mil(_s32(p, 17))),
            "diameter": _mil(_s32(p, 21)),
            "hole": _mil(_s32(p, 25)),
            "layer_start": p[29],
            "layer_end": p[30],
        })
    return out


def parse_arcs(buf: bytes, nets: list[str]) -> list[dict]:
    """Arcs6: circular arcs (payload: :func:`decode_arc`)."""
    out = []
    for rtype, p in _records(buf, "Arcs6"):
        if rtype != _TYPE_ARC:
            fail("ALTIUM_UNSUPPORTED", f"Arcs6: unknown record type 0x{rtype:02x}")
        a = decode_arc(p, "Arcs6")
        out.append({
            "layer": a.layer,
            "layer_name": LAYER_NAMES.get(a.layer, str(a.layer)),
            "net": _net_name(a.net, nets),
            "component": None if a.component == _NO_NET else a.component,
            "center": (_mil(a.cx), _mil(a.cy)),
            "radius": _mil(a.radius),
            "angle_start": a.angle_start,
            "angle_end": a.angle_end,
            "width": _mil(a.width),
        })
    return out


# Pad shape ids (geometry block).
_PAD_SHAPES = {1: "round", 2: "rect", 3: "octagon", 9: "roundrect"}


def parse_pads(buf: bytes, nets: list[str]) -> list[dict]:
    """Pads6: component pads (block-structured records).

    Each pad is a type-0x02 record holding the pad NAME (pascal string),
    followed by four bare length-prefixed blocks; the fifth block is the
    geometry: layer u8@0, net u16@3, component u16@7, x/y s32@13/17,
    top size-x/y s32@21/25, mid size s32@29/33, bottom size s32@37/41,
    hole s32@45, shape u8@49, rotation f64@52, plated? u8@60.
    """
    out = []
    pos, n = 0, len(buf)

    def take_block(expect_type: bool):
        nonlocal pos
        if expect_type:
            if pos >= n:
                return None
            rtype = buf[pos]
            if rtype != _TYPE_PAD:
                fail("ALTIUM_UNSUPPORTED", f"Pads6: unknown record type 0x{rtype:02x}")
            pos += 1
        if pos + 4 > n:
            fail("ALTIUM_MALFORMED", "Pads6: truncated block length")
        (length,) = struct.unpack_from("<I", buf, pos)
        pos += 4
        if pos + length > n:
            fail("ALTIUM_MALFORMED", "Pads6: block overruns stream")
        block = buf[pos:pos + length]
        pos += length
        return block

    while pos < n:
        name_block = take_block(expect_type=True)
        if name_block is None:
            break
        name = ""
        if name_block:
            slen = name_block[0]
            name = name_block[1:1 + slen].decode("latin-1", "replace")
        take_block(False)   # reserved
        take_block(False)   # reserved
        take_block(False)   # reserved
        geo = take_block(False)
        if geo is None or len(geo) < 61:
            fail("ALTIUM_MALFORMED", "Pads6: geometry block too short")
        # optional trailing extended block (newer AD versions)
        if pos < n and buf[pos] != _TYPE_PAD:
            take_block(False)
        layer = geo[0]
        comp = _u16(geo, 7)
        (rotation,) = struct.unpack_from("<d", geo, 52)
        out.append({
            "name": name,
            "layer": layer,
            "layer_name": LAYER_NAMES.get(layer, str(layer)),
            "net": _net_name(_u16(geo, 3), nets),
            "component": None if comp == _NO_NET else comp,
            "at": (_mil(_s32(geo, 13)), _mil(_s32(geo, 17))),
            "size": (_mil(_s32(geo, 21)), _mil(_s32(geo, 25))),
            "hole": _mil(_s32(geo, 45)),
            "shape": _PAD_SHAPES.get(geo[49], str(geo[49])),
            "rotation": rotation,
        })
    return out
