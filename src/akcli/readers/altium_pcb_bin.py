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

Prior art gratefully acknowledged: thesourcerer8/altium2kicad and KiCad's
``pcbnew`` Altium plugin document the same field offsets.
"""

from __future__ import annotations

import struct

from ..errors import fail

__all__ = ["parse_tracks", "parse_vias", "parse_arcs", "parse_pads", "UNIT_MIL"]

# One Altium PCB unit = 1/10000 mil.
UNIT_MIL = 1.0 / 10000.0

# Record-type bytes per section (empirical, consistent across samples).
_TYPE_ARC = 0x01
_TYPE_PAD = 0x02
_TYPE_VIA = 0x03
_TYPE_TRACK = 0x04

_NO_NET = 0xFFFF

# Common Altium layer ids -> names (best effort; raw id always included).
LAYER_NAMES = {
    1: "Top", 32: "Bottom", 33: "TopOverlay", 34: "BottomOverlay",
    35: "TopPaste", 36: "BottomPaste", 37: "TopSolder", 38: "BottomSolder",
    39: "InternalPlane1", 56: "DrillGuide", 57: "KeepOut", 74: "MultiLayer",
}
for _i in range(2, 32):
    LAYER_NAMES.setdefault(_i, f"Mid{_i - 1}")
for _i in range(58, 74):
    LAYER_NAMES.setdefault(_i, f"Mechanical{_i - 57}")


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


def parse_tracks(buf: bytes, nets: list[str]) -> list[dict]:
    """Tracks6: straight copper/graphic segments.

    Payload: layer u8@0, net u16@3, component u16@7, x1/y1/x2/y2 s32@13..28,
    width s32@29.
    """
    out = []
    for rtype, p in _records(buf, "Tracks6"):
        if rtype != _TYPE_TRACK:
            fail("ALTIUM_UNSUPPORTED", f"Tracks6: unknown record type 0x{rtype:02x}")
        if len(p) < 33:
            fail("ALTIUM_MALFORMED", "Tracks6: record too short")
        layer = p[0]
        comp = _u16(p, 7)
        out.append({
            "layer": layer,
            "layer_name": LAYER_NAMES.get(layer, str(layer)),
            "net": _net_name(_u16(p, 3), nets),
            "component": None if comp == _NO_NET else comp,
            "start": (_mil(_s32(p, 13)), _mil(_s32(p, 17))),
            "end": (_mil(_s32(p, 21)), _mil(_s32(p, 25))),
            "width": _mil(_s32(p, 29)),
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
    """Arcs6: circular arcs.

    Payload: layer u8@0, net u16@3, component u16@7, cx/cy s32@13/17,
    radius s32@21, start/end angle f64@25/33 (degrees, CCW), width s32@41.
    """
    out = []
    for rtype, p in _records(buf, "Arcs6"):
        if rtype != _TYPE_ARC:
            fail("ALTIUM_UNSUPPORTED", f"Arcs6: unknown record type 0x{rtype:02x}")
        if len(p) < 45:
            fail("ALTIUM_MALFORMED", "Arcs6: record too short")
        layer = p[0]
        comp = _u16(p, 7)
        sa, ea = struct.unpack_from("<dd", p, 25)
        out.append({
            "layer": layer,
            "layer_name": LAYER_NAMES.get(layer, str(layer)),
            "net": _net_name(_u16(p, 3), nets),
            "component": None if comp == _NO_NET else comp,
            "center": (_mil(_s32(p, 13)), _mil(_s32(p, 17))),
            "radius": _mil(_s32(p, 21)),
            "angle_start": sa,
            "angle_end": ea,
            "width": _mil(_s32(p, 41)),
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
