"""``.SchLib`` -> :class:`model.Library` (READ-ONLY) (SPEC §3.2).

An Altium schematic *library* is an OLE2/CFBF container where **each symbol lives
in its own storage** with a ``Data`` stream (plus a top-level ``FileHeader`` index
stream). Unlike a ``.SchDoc`` -- whose single ``FileHeader`` carries every record
behind one HEADER -- a SchLib has many ``Data`` streams that would *collide* under
bare-name keying, so this reader uses
:func:`_cfbf.read_streams_qualified` (``"SymbolName/Data"``).

Per-symbol record framing notes:

* the per-stream ``OwnerIndex`` base is the stream's own record list -- there is
  **no blind ``[1:]``**; :func:`altium_records.parse_records` only drops a leading
  record when it is an actual schematic ``HEADER`` (a symbol ``Data`` stream begins
  with its RECORD-1 ``Component``, so nothing is dropped);
* **binary records are refused loudly** -- a text record's 4th framing byte (the
  flag) is ``0``; any non-zero flag means a binary primitive stream we do not yet
  decode, so we raise ``ALTIUM_UNSUPPORTED`` (exit 5, unsupported — not "corrupt")
  rather than mojibake-parse it.
"""

from __future__ import annotations

import os

from ..errors import fail
from ..model import Library, Pin, SymbolDef
from . import _cfbf
from .altium_records import (
    RECORD_COMPONENT,
    RECORD_PIN,
    coord,
    gi,
    parse_records,
    pin_electrical_type,
)

# PinConglomerate low 2 bits -> orientation unit vector (Altium Location units).
_DIRS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}


def _rid(rec: dict) -> int | None:
    return gi(rec, "RECORD")


def _refuse_binary_records(buf: bytes) -> None:
    """Raise loudly if any framed record carries a non-zero (binary) flag byte.

    Frame = ``[3-byte LE length][1 flag byte][payload]``; a text record's flag is
    ``0``. Binary symbol primitives are explicitly deferred, so we refuse rather
    than feed binary bytes through the ``|KEY=VALUE|`` tokenizer.
    """
    pos, n = 0, len(buf)
    while pos + 4 <= n:
        ln = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16)
        flag = buf[pos + 3]
        if flag != 0:
            fail(
                "ALTIUM_UNSUPPORTED",
                f"binary symbol record (flag={flag:#x}) unsupported (deferred)",
            )
        pos += 4 + ln


def _pin_tip(d: dict) -> tuple[float, float]:
    """Electrical-tip (mils, +Y down) of a RECORD-2 pin = Location + PinLength*dir."""
    loc_x = coord(d, "Location.X")
    loc_y = coord(d, "Location.Y")
    length = coord(d, "PinLength")
    dx, dy = _DIRS[gi(d, "PinConglomerate", 0) & 3]
    return (loc_x + length * dx, -(loc_y + length * dy))


def _symbol_from_records(recs: list[dict], fallback_name: str) -> SymbolDef | None:
    """Build a :class:`SymbolDef` from one symbol stream's record list."""
    comp_idx: int | None = None
    name = fallback_name
    part_count = 1
    for idx, r in enumerate(recs):
        if _rid(r) == RECORD_COMPONENT:
            comp_idx = idx
            name = r.get("LibReference") or r.get("DesignItemId") or fallback_name
            part_count = gi(r, "PartCount", 1) or 1
            break
    if comp_idx is None:
        return None

    pins: list[Pin] = []
    for r in recs:
        if _rid(r) != RECORD_PIN:
            continue
        tip_x, tip_y = _pin_tip(r)
        pins.append(
            Pin(
                number=r.get("Designator", ""),
                name=r.get("Name"),
                x_mil=tip_x,
                y_mil=tip_y,
                electrical_type=pin_electrical_type(r),
                owner_part_id=gi(r, "OwnerPartId", 1) or 1,
                unique_id=r.get("UniqueId"),
            )
        )
    return SymbolDef(name=name, lib_id=name, pins=pins, part_count=part_count)


def read(path: os.PathLike | str | bytes | bytearray) -> Library:
    """Read a ``.SchLib`` into a normalized :class:`model.Library`.

    Every symbol is recovered (never collapsed by the bare-name ``Data``
    collision); binary symbol streams are refused loudly.
    """
    streams = _cfbf.read_streams_qualified(path)

    symbols: list[SymbolDef] = []
    for key in sorted(streams):
        # symbol payloads live in a storage's "Data" stream (e.g. "RES/Data").
        if not key.endswith("/Data") and key != "Data":
            continue
        buf = streams[key]
        if not buf:
            continue
        _refuse_binary_records(buf)
        recs = parse_records(buf, drop_header=True)
        storage = key[: -len("/Data")] if key.endswith("/Data") else "Symbol"
        sym = _symbol_from_records(recs, fallback_name=storage)
        if sym is not None:
            symbols.append(sym)

    src = path if isinstance(path, (str, os.PathLike)) else "<bytes>"
    return Library(source_path=str(src), source_format="altium", symbols=symbols)


__all__ = ["read"]
