"""Tests for the binary ``.PcbDoc`` section decoders.

Synthetic records are encoded here in the same layout the decoder documents
(offsets empirically verified against real boards from KiCad's Altium QA
corpus and cross-validated item-by-item against KiCad's own importer —
778/778 board-level copper tracks, 20/20 vias, 236/236 arcs, 48/48 pads with
exact drills/sizes and pure-translation coordinates). Set
``AKCLI_PCBDOC_SAMPLE=/path/to/real.PcbDoc`` to additionally run the
real-board smoke test.
"""

from __future__ import annotations

import os
import struct

import pytest

from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.readers import altium_pcb_bin as B

NETS = ["GND", "V3V3"]


def _rec(rtype: int, payload: bytes) -> bytes:
    return bytes([rtype]) + struct.pack("<I", len(payload)) + payload


def _track(layer=1, net=0, comp=0xFFFF, x1=10000, y1=20000, x2=30000, y2=20000, w=100):
    p = bytearray(33)
    p[0] = layer
    struct.pack_into("<H", p, 3, net)
    struct.pack_into("<H", p, 7, comp)
    struct.pack_into("<iiii", p, 13, x1, y1, x2, y2)
    struct.pack_into("<i", p, 29, w)
    return _rec(0x04, bytes(p))


def _via(net=0, x=5000, y=5000, dia=500, hole=280):
    p = bytearray(31)
    struct.pack_into("<H", p, 3, net)
    struct.pack_into("<H", p, 7, 0xFFFF)
    struct.pack_into("<ii", p, 13, x, y)
    struct.pack_into("<ii", p, 21, dia, hole)
    p[29], p[30] = 1, 32
    return _rec(0x03, bytes(p))


def _arc(layer=1, cx=0, cy=0, r=1000, a0=0.0, a1=90.0, w=60):
    p = bytearray(45)
    p[0] = layer
    struct.pack_into("<H", p, 3, 0xFFFF)
    struct.pack_into("<H", p, 7, 0xFFFF)
    struct.pack_into("<ii", p, 13, cx, cy)
    struct.pack_into("<i", p, 21, r)
    struct.pack_into("<dd", p, 25, a0, a1)
    struct.pack_into("<i", p, 41, w)
    return _rec(0x01, bytes(p))


def test_tracks_decode_units_and_nets():
    (t,) = B.parse_tracks(_track(net=1, w=100), NETS)
    assert t["net"] == "V3V3"
    assert t["component"] is None
    assert t["start"] == (1.0, 2.0) and t["end"] == (3.0, 2.0)  # units/10000 -> mils
    assert t["width"] == 0.01
    assert t["layer_name"] == "Top"


def test_via_decode():
    (v,) = B.parse_vias(_via(), NETS)
    assert v["net"] == "GND"
    assert v["diameter"] == 0.05 and v["hole"] == 0.028
    assert (v["layer_start"], v["layer_end"]) == (1, 32)


def test_arc_angles_are_degrees():
    (a,) = B.parse_arcs(_arc(a0=45.0, a1=180.0), NETS)
    assert (a["angle_start"], a["angle_end"]) == (45.0, 180.0)
    assert a["radius"] == 0.1


def test_multiple_records_stream():
    buf = _track() + _track(x1=1, y1=1, x2=2, y2=1) + _track(layer=32)
    out = B.parse_tracks(buf, NETS)
    assert len(out) == 3 and out[2]["layer_name"] == "Bottom"


def test_unknown_record_type_refused_loudly():
    with pytest.raises(AkcliError) as ei:
        B.parse_tracks(_rec(0x7E, bytes(33)), NETS)
    assert ei.value.code == "ALTIUM_UNSUPPORTED"


def test_truncated_record_refused():
    good = _track()
    with pytest.raises(AkcliError) as ei:
        B.parse_tracks(good[:-5], NETS)
    assert ei.value.code == "ALTIUM_MALFORMED"


def test_out_of_range_net_index_is_none():
    (t,) = B.parse_tracks(_track(net=99), NETS)
    assert t["net"] is None


# --------------------------------------------------------------------------- #
# real-board smoke (gated: needs a genuine binary .PcbDoc on disk)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not os.environ.get("AKCLI_PCBDOC_SAMPLE"),
                    reason="set AKCLI_PCBDOC_SAMPLE to a real binary .PcbDoc")
def test_real_board_decodes():
    from altium_kicad_cli.readers import altium_pcb

    pcb = altium_pcb.read(os.environ["AKCLI_PCBDOC_SAMPLE"])
    assert pcb.pads, "no pads decoded from the real board"
    assert pcb.tracks, "no tracks decoded from the real board"
    for pad in pcb.pads:
        assert pad["name"] is not None
        assert abs(pad["at"][0]) < 1e6 and abs(pad["at"][1]) < 1e6  # sane mils
    named = [v for v in pcb.vias if v["net"]]
    assert named, "no via resolved to a net name"
