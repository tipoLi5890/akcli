"""Netlist emitters: Protel, KiCad eeschema, flat CSV (SPEC §3.1 ``export``).

Each emitter takes a normalized :class:`~akcli.model.Schematic` (the
shared model produced by any reader) and returns a ``str`` netlist:

* ``protel`` — classic Protel/Tango ``.NET`` (Altium-importable): a ``[...]``
  component section followed by ``(...)`` net section.
* ``kicad`` — KiCad legacy eeschema netlist (``(export (version "E") ...)``
  S-expression with ``components`` + ``nets``).
* ``csv`` — flat ``net,ref,pin`` table (one row per net membership).

Unnamed nets (``Net.name is None``) are emitted under their coordinate-free
``stable_id`` so the output is deterministic and never coordinate-derived.
"""

from __future__ import annotations

import csv
import io

from .errors import fail
from .model import Component, Net, Schematic

__all__ = [
    "FORMATS",
    "export_netlist",
    "to_protel",
    "to_kicad_netlist",
    "to_csv",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _net_name(net: Net) -> str:
    """Display name for a net, falling back to its membership-stable id."""
    return net.name if net.name else net.stable_id


def _sorted_nets(sch: Schematic) -> list[Net]:
    """Nets in a stable order: named alphabetically, then unnamed by stable id."""
    return sorted(sch.nets, key=lambda n: (n.name is None, _net_name(n)))


def _sorted_components(sch: Schematic) -> list[Component]:
    return sorted(sch.components, key=lambda c: c.designator)


def _kicad_quote(s: object) -> str:
    """Quote a token KiCad-style (escape backslash + double-quote)."""
    text = "" if s is None else str(s)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# --------------------------------------------------------------------------- #
# Protel / Tango netlist (Altium-importable)
# --------------------------------------------------------------------------- #
def to_protel(sch: Schematic) -> str:
    """Emit a classic Protel ``.NET`` netlist (importable by Altium Designer)."""
    out: list[str] = []
    # Component section: one [ ... ] block per component.
    for comp in _sorted_components(sch):
        out.append("[")
        out.append(comp.designator)
        out.append(comp.footprint or "")
        out.append(comp.value or comp.library_ref or "")
        out.append("")  # part field 1 (reserved / blank)
        out.append("")  # part field 2
        out.append("]")
    # Net section: one ( ... ) block per net.
    for net in _sorted_nets(sch):
        out.append("(")
        out.append(_net_name(net))
        for desig, pin in net.members:
            out.append(f"{desig}-{pin}")
        out.append(")")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# KiCad legacy eeschema netlist
# --------------------------------------------------------------------------- #
def to_kicad_netlist(sch: Schematic) -> str:
    """Emit a KiCad legacy eeschema netlist (``(export (version "E") ...)``)."""
    lines: list[str] = []
    lines.append('(export (version "E")')
    # components
    lines.append("  (components")
    for comp in _sorted_components(sch):
        lines.append(f"    (comp (ref {_kicad_quote(comp.designator)})")
        lines.append(f"      (value {_kicad_quote(comp.value or comp.library_ref or '~')})")
        if comp.footprint:
            lines.append(f"      (footprint {_kicad_quote(comp.footprint)})")
        if comp.library_ref:
            lines.append(f"      (libsource (part {_kicad_quote(comp.library_ref)}))")
        lines.append("      )")
    lines.append("  )")
    # nets
    lines.append("  (nets")
    for code, net in enumerate(_sorted_nets(sch), start=1):
        lines.append(f"    (net (code {_kicad_quote(code)}) (name {_kicad_quote(_net_name(net))})")
        for desig, pin in net.members:
            lines.append(f"      (node (ref {_kicad_quote(desig)}) (pin {_kicad_quote(pin)}))")
        lines.append("      )")
    lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# flat CSV
# --------------------------------------------------------------------------- #
def to_csv(sch: Schematic) -> str:
    """Emit a flat ``net,ref,pin`` CSV (one row per net membership)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["net", "ref", "pin"])
    for net in _sorted_nets(sch):
        name = _net_name(net)
        for desig, pin in net.members:
            writer.writerow([name, desig, pin])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
FORMATS: dict[str, "callable[[Schematic], str]"] = {
    "protel": to_protel,
    "kicad": to_kicad_netlist,
    "csv": to_csv,
}


def export_netlist(sch: Schematic, fmt: str) -> str:
    """Render ``sch`` as a netlist in ``fmt`` (``protel`` | ``kicad`` | ``csv``)."""
    emitter = FORMATS.get(fmt)
    if emitter is None:
        fail("BAD_CONFIG", f"unknown export format {fmt!r}; choose from {sorted(FORMATS)}")
    return emitter(sch)
