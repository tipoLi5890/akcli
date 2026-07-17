"""BOM eligibility policy — refdes classes shared by every BOM surface.

A schematic's components are not one population: test points, fiducials,
mounting holes and logos are STRUCTURAL — they exist on the board but no
part is ever bought for them. Treating them like orderable lines produced
both noise (on a real board, most id-less "problems" were test points
and bare pads) and a
real incident (the suggestion engine offering an SMPS IC for a test point).

This module owns the classification; ``checks/bom.py`` (offline) and
``parts/bom_jlc.py`` (catalog) both consume it, and the prefix set is
project-configurable via ``akcli.toml`` ``[bom].no_part_classes``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Component

__all__ = ["DEFAULT_NO_PART_PREFIXES", "CLASSES", "refdes_prefix",
           "is_no_part_ref", "sourcing_of", "classify", "order_id_of"]

# The four BOM populations (SPEC BOM P1-2). Every surface — check --bom,
# jlc bom, the order CSV, totals — speaks this vocabulary.
CLASSES: tuple[str, ...] = ("fitted", "dnp", "external", "no-part")

# Refdes prefixes that never carry an orderable part (full alpha-prefix
# equality, so "H3" matches but a heatsink "HS1" does not).
DEFAULT_NO_PART_PREFIXES: tuple[str, ...] = ("TP", "FID", "MH", "H", "LOGO")

_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def refdes_prefix(ref: str | None) -> str:
    """The alpha prefix of a refdes, uppercased (``TP12`` -> ``TP``)."""
    m = _PREFIX_RE.match((ref or "").strip())
    return m.group(1).upper() if m else ""


def is_no_part_ref(ref: str | None,
                   prefixes: tuple[str, ...] = DEFAULT_NO_PART_PREFIXES) -> bool:
    """True when ``ref``'s class is structural (no part expected)."""
    p = refdes_prefix(ref)
    return bool(p) and p in {x.upper() for x in prefixes}


# Recognized sourcing-channel parameter keys (case-insensitive).
_SOURCING_KEYS: tuple[str, ...] = ("Sourcing", "BOM_Sourcing")

# Explicit order-id parameter keys: an LCSC C-number or a manufacturer part
# number that an assembler can actually order by. (The offline coverage
# heuristic additionally accepts digit-bearing library refs — see
# checks/bom._part_identity — but a MISSING order id is judged on these.)
_ORDER_LCSC_KEYS: tuple[str, ...] = (
    "LCSC Part", "LCSC Part Name", "LCSC", "LCSC#",
    "JLCPCB Part", "JLCPCB", "JLC", "JLC#",
)
_ORDER_MPN_KEYS: tuple[str, ...] = (
    "MPN", "Manufacturer Part", "Manufacturer Part Number", "Mfr. Part",
    "Mfr Part", "Part Number", "Supplier Part",
)


def sourcing_of(comp: "Component") -> tuple[str, str]:
    """``(channel, note)`` from a ``Sourcing``/``BOM_Sourcing`` parameter.

    Channels: ``lcsc`` (default), ``external`` (bought elsewhere; the value
    may carry a note — ``external:vendor official store``), ``consigned``
    (customer-supplied to the fab) and ``no-part``. Unknown values fall back
    to ``lcsc`` so a typo can never silently drop a part from the order.
    """
    params = comp.parameters or {}
    raw = ""
    for k, v in params.items():
        if k.casefold() in {x.casefold() for x in _SOURCING_KEYS}:
            raw = str(v or "").strip()
            break
    if not raw:
        return "lcsc", ""
    channel, _, note = raw.partition(":")
    channel = channel.strip().casefold()
    note = note.strip()
    if channel in ("external", "consigned"):
        return channel, note
    if channel in ("no-part", "nopart", "none"):
        return "no-part", note
    return "lcsc", ""


def classify(comp: "Component",
             prefixes: tuple[str, ...] = DEFAULT_NO_PART_PREFIXES,
             ) -> tuple[str, str]:
    """``(bom_class, note)`` — one of :data:`CLASSES` per component.

    Precedence: structural facts first (``in_bom=no``, a structural refdes
    class, or an explicit ``no-part`` sourcing), then ``dnp``, then the
    sourcing channel, else ``fitted``. A DNP'd test point is still a test
    point — no-part wins over dnp.
    """
    channel, note = sourcing_of(comp)
    if not comp.in_bom or is_no_part_ref(comp.designator, prefixes) \
            or channel == "no-part":
        return "no-part", note
    if comp.dnp:
        return "dnp", note
    if channel in ("external", "consigned"):
        return "external", (note or channel)
    return "fitted", ""


_CNUM_RE = re.compile(r"^[Cc]?\d{2,}$")


def order_id_of(comp: "Component") -> str | None:
    """The explicit order id a fitted line needs (LCSC C-number or MPN)."""
    params = comp.parameters or {}
    lowered = {k.casefold(): str(v or "").strip() for k, v in params.items()}
    for key in _ORDER_LCSC_KEYS:
        v = lowered.get(key.casefold())
        if v and _CNUM_RE.match(v):
            return v if v.upper().startswith("C") else "C" + v
    for key in _ORDER_MPN_KEYS:
        v = lowered.get(key.casefold())
        if v:
            return v
    return None
