"""Altium record framing + ``|KEY=VALUE|`` tokenizer (SPEC §3.2).

The ``FileHeader`` (and per-symbol ``Data``) stream of a relicensed-MIT Altium
binary doc is a sequence of length-prefixed text records::

    [ 3-byte LE length ][ 1 flag byte ][ payload + NUL ]

where ``length`` counts the ``payload + NUL`` bytes (not the 4-byte header) and a
text record's ``flag`` byte is ``0``. The payload is ``|KEY=VALUE|KEY=VALUE|...|``.

This is a hardened, from-scratch reimplementation of the record framing +
``|KEY=VALUE|`` tokenizer, with:

* a ``%UTF8%KEY`` twin-field decode (Altium stores CJK / Ω / µ values a second
  time UTF-8-encoded; we re-encode the latin1 framing bytes and decode UTF-8 so
  the canonical value is clean, not mojibake);
* ``*_Frac`` companion assembly via :func:`units.altium_to_mil` so sub-unit
  coordinates are not dropped (off-grid misses);
* an ``Electrical`` -> :class:`model.PinType` map;
* RECORD-ID constants (Appendix A);
* **header detection** -- ``drop_header`` removes the leading record only when it
  is the schematic ``HEADER`` (so SchLib/PcbDoc ``OwnerIndex`` bases are not
  shifted by a blind ``[1:]``);
* a :data:`safety.MAX_RECORDS` cap.
"""

from __future__ import annotations

from .. import safety
from ..errors import fail
from ..model import ALTIUM_ELECTRICAL, PinType
from ..units import altium_to_mil

# --- RECORD-ID constants (SPEC Appendix A; ★ = net-bearing) -----------------
RECORD_COMPONENT = 1
RECORD_PIN = 2            # ★
RECORD_POLYLINE = 6
RECORD_SHEET_SYMBOL = 15  # ★
RECORD_SHEET_ENTRY = 16   # ★
RECORD_POWER_PORT = 17    # ★
RECORD_PORT = 18          # ★
RECORD_NO_ERC = 22
RECORD_NET_LABEL = 25     # ★
RECORD_WIRE = 27          # ★
RECORD_JUNCTION = 29      # ★
RECORD_DESIGNATOR = 34
RECORD_PARAMETER = 41
RECORD_IMPL_44 = 44
RECORD_IMPL_MODEL = 45
RECORD_IMPL_FOOTPRINT = 46
RECORD_IMPL_48 = 48

RECORDS: dict[int, str] = {
    RECORD_COMPONENT: "Component",
    RECORD_PIN: "Pin",
    RECORD_POLYLINE: "Polyline",
    RECORD_SHEET_SYMBOL: "SheetSymbol",
    RECORD_SHEET_ENTRY: "SheetEntry",
    RECORD_POWER_PORT: "PowerPort",
    RECORD_PORT: "Port",
    RECORD_NO_ERC: "NoERC",
    RECORD_NET_LABEL: "NetLabel",
    RECORD_WIRE: "Wire",
    RECORD_JUNCTION: "Junction",
    RECORD_DESIGNATOR: "Designator",
    RECORD_PARAMETER: "Parameter",
    RECORD_IMPL_44: "Implementation",
    RECORD_IMPL_MODEL: "ModelImplementation",
    RECORD_IMPL_FOOTPRINT: "FootprintImplementation",
    RECORD_IMPL_48: "Implementation48",
}

_UTF8_PREFIX = "%UTF8%"


def fields(r: str) -> dict[str, str]:
    """Tokenize one ``|KEY=VALUE|...|`` record payload into a dict.

    ``%UTF8%KEY`` twin fields are decoded (latin1 bytes -> UTF-8) and override the
    plain ``KEY`` twin, so CJK / Ω / µ values are canonical, not mojibake.
    """
    d: dict[str, str] = {}
    utf8: dict[str, str] = {}
    for tok in r.split("|"):
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k.startswith(_UTF8_PREFIX):
            base = k[len(_UTF8_PREFIX):]
            # NOTE: this only re-decodes latin1 framing bytes as UTF-8. If an upstream
            # tool already wrote a value's Ω/µ/± as the U+FFFD replacement bytes
            # (``EF BF BD``) on a non-UTF-8 locale, the loss happened at *export* time
            # and cannot be recovered here by any codec — re-export from a tool that
            # preserves it instead.
            try:
                v = v.encode("latin-1", "replace").decode("utf-8")
            except UnicodeDecodeError:
                v = v.encode("latin-1", "replace").decode("utf-8", "replace")
            utf8[base] = v
        else:
            d[k] = v
    d.update(utf8)  # UTF-8 twin wins over the latin1 twin
    return d


def gi(d: dict, k: str, default=None):
    """Get integer field ``k`` from ``d`` (or ``default`` if absent/non-numeric)."""
    v = d.get(k)
    if v is not None and v.lstrip("-").isdigit():
        return int(v)
    return default


def coord(d: dict, key: str) -> float:
    """Assemble an Altium ``key`` (+ ``key + "_Frac"``) coordinate into mils."""
    return altium_to_mil(gi(d, key, 0), gi(d, key + "_Frac", 0))


def pin_electrical_type(d: dict) -> PinType:
    """Map a pin record's ``Electrical`` int onto a canonical :class:`PinType`."""
    return ALTIUM_ELECTRICAL.get(gi(d, "Electrical", 4), PinType.PASSIVE)


def _is_header(rec: dict) -> bool:
    """A schematic HEADER record carries the ``HEADER`` key and no ``RECORD`` id."""
    return "HEADER" in rec and "RECORD" not in rec


def parse_records(buf: bytes, drop_header: bool = True) -> list[dict]:
    """Frame ``buf`` into a list of field-dicts.

    Each record is ``[3-byte LE length][1 flag byte][payload + NUL]``; the trailing
    NUL padding is stripped before tokenizing (this is how the miniFAT and
    fat-chain fixtures, whose last record is NUL-padded to change container layout,
    parse to *identical* records). When ``drop_header`` is true the leading record
    is removed **only** if it is the schematic ``HEADER`` (never a blind ``[1:]``).
    """
    recs: list[dict] = []
    pos = 0
    n = len(buf)
    while pos + 4 <= n:
        ln = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16)
        # buf[pos + 3] is the record flag (0 = text); binary records are handled
        # by the per-format readers, not here.
        pos += 4
        end = pos + ln
        payload = buf[pos:end]  # safe slice even when the final record is truncated
        pos = end
        rec_str = payload.rstrip(b"\x00").decode("latin-1", "replace")
        recs.append(fields(rec_str))
        if len(recs) > safety.MAX_RECORDS:
            fail("ALTIUM_ALLOC_GUARD", "record count exceeds cap")
    if drop_header and recs and _is_header(recs[0]):
        recs = recs[1:]
    return recs


__all__ = [
    "parse_records",
    "fields",
    "gi",
    "coord",
    "pin_electrical_type",
    "RECORDS",
    "RECORD_COMPONENT",
    "RECORD_PIN",
    "RECORD_POLYLINE",
    "RECORD_SHEET_SYMBOL",
    "RECORD_SHEET_ENTRY",
    "RECORD_POWER_PORT",
    "RECORD_PORT",
    "RECORD_NO_ERC",
    "RECORD_NET_LABEL",
    "RECORD_WIRE",
    "RECORD_JUNCTION",
    "RECORD_DESIGNATOR",
    "RECORD_PARAMETER",
    "RECORD_IMPL_44",
    "RECORD_IMPL_MODEL",
    "RECORD_IMPL_FOOTPRINT",
    "RECORD_IMPL_48",
]
