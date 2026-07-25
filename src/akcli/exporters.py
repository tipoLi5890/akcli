"""Netlist emitters: Protel, KiCad eeschema, flat CSV (SPEC §3.1 ``export``).

Each emitter takes a normalized :class:`~akcli.model.Schematic` (the
shared model produced by any reader) and returns a ``str`` netlist:

* ``protel`` — classic Protel/Tango ``.NET`` (Altium-importable): a ``[...]``
  component section followed by ``(...)`` net section.
* ``kicad`` — KiCad legacy eeschema netlist (``(export (version "E") ...)``
  S-expression with ``design`` + ``components`` + ``libparts`` + ``nets``).
* ``csv`` — flat ``net,ref,pin`` table (one row per net membership).

Unnamed nets (``Net.name is None``) are emitted under their coordinate-free
``stable_id`` so the output is deterministic and never coordinate-derived.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import PurePath

from . import __version__
from .errors import fail
from .model import Component, Net, Pin, Schematic

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
# Fields already carried by a dedicated `(comp ...)` sub-node, so they are not
# repeated inside `(fields)`.
_COMP_OWN_FIELDS = frozenset({"Reference", "Value", "Footprint"})

# `~` is KiCad's "no pin name" placeholder; its own netlist emits `(name "")`.
_NO_PIN_NAME = "~"


def _split_lib_id(lib_id: str) -> tuple[str, str]:
    """Split a KiCad ``lib_id`` into ``(lib, part)`` (``lib`` is ``""`` if absent)."""
    lib, sep, part = lib_id.partition(":")
    return (lib, part) if sep else ("", lib_id)


def _sheetpath(comp: Component) -> tuple[str, str]:
    """``(names, tstamps)`` sheet path for ``comp``, KiCad-style (leading+trailing ``/``).

    The KiCad reader stores the sheet-INSTANCE UUID path (``""`` for the root
    file, ``/<sheet-uuid>[/<sheet-uuid>...]`` below it), which is exactly the
    ``tstamps`` path KiCad's own netlist emits — and, together with the
    per-component ``(tstamps <symbol-uuid>)``, the UUID path KiCad 6+ keys the
    footprint<->symbol association by. When the model instead carries a human
    sheet *name* (the Altium reader's provenance string), it goes into ``names``
    and the path degrades to the root ``"/"``.
    """
    s = (comp.sheet or "").strip()
    if s.startswith("/"):
        path = s if s.endswith("/") else s + "/"
        return path, path
    if s:
        return "/" + s.strip("/") + "/", "/"
    return "/", "/"


def _design_sheets(sch: Schematic) -> list[tuple[str, str]]:
    """Distinct ``(names, tstamps)`` sheet paths, root first, deterministic order."""
    by_tstamps: dict[str, str] = {}
    for comp in _sorted_components(sch):
        names, tstamps = _sheetpath(comp)
        by_tstamps.setdefault(tstamps, names)
    by_tstamps.setdefault("/", "/")
    return [(by_tstamps[t], t) for t in sorted(by_tstamps)]


def _pin_sort_key(pin: Pin) -> tuple:
    """Natural sort for pin numbers (``2`` before ``10``, ``A1`` after ``9``)."""
    parts = re.findall(r"\d+|\D+", pin.number)
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


def _libparts(sch: Schematic) -> list[tuple[str, str, dict[str, str], list[Pin]]]:
    """One ``(lib, part, fields, pins)`` entry per distinct ``lib_id``, sorted.

    ``Footprint``/``Datasheet`` are library-level truths here, so they are only
    emitted when EVERY instance of that ``lib_id`` agrees on the same non-empty
    value (one instance's override is not a property of the part).
    """
    by_lib_id: dict[str, list[Component]] = {}
    for comp in _sorted_components(sch):
        if comp.library_ref:
            by_lib_id.setdefault(comp.library_ref, []).append(comp)

    out: list[tuple[str, str, dict[str, str], list[Pin]]] = []
    for lib_id in sorted(by_lib_id):
        comps = by_lib_id[lib_id]
        lib, part = _split_lib_id(lib_id)
        fields = {"Value": part}
        for name, values in (
            ("Footprint", [c.footprint for c in comps]),
            ("Datasheet", [c.parameters.get("Datasheet") for c in comps]),
        ):
            unique = {v for v in values}
            if len(unique) == 1 and all(values):
                fields[name] = str(values[0])
        pins = next((c.pins for c in comps if c.pins), [])
        out.append((lib, part, fields, sorted(pins, key=_pin_sort_key)))
    return out


def to_kicad_netlist(sch: Schematic) -> str:
    """Emit a KiCad legacy eeschema netlist (``(export (version "E") ...)``).

    Faithful to the sections KiCad's own ``sch export netlist`` writes, with the
    UUID identity that matters on re-import: ``(design)`` (source/tool/sheets),
    ``(components)`` with ``(sheetpath (names ..) (tstamps ..))`` +
    ``(tstamps <symbol-uuid>)``, ``(libparts)`` (fields + pins per distinct
    ``lib_id``) and ``(nets)``.

    ``(date "")`` is emitted **empty on purpose**: the field is part of the
    grammar KiCad writes, but a wall-clock date would make the output
    non-deterministic (same input bytes must give the same output bytes).
    """
    lines: list[str] = []
    lines.append('(export (version "E")')
    # design: provenance + the sheet table (no wall-clock date, see docstring)
    lines.append("  (design")
    lines.append(f"    (source {_kicad_quote(PurePath(sch.source_path).as_posix())})")
    lines.append('    (date "")')
    lines.append(f"    (tool {_kicad_quote('akcli ' + __version__)})")
    for number, (names, tstamps) in enumerate(_design_sheets(sch), start=1):
        lines.append(
            f"    (sheet (number {_kicad_quote(number)}) (name {_kicad_quote(names)})"
            f" (tstamps {_kicad_quote(tstamps)}))"
        )
    lines.append("  )")
    # components
    lines.append("  (components")
    for comp in _sorted_components(sch):
        names, tstamps = _sheetpath(comp)
        lines.append(f"    (comp (ref {_kicad_quote(comp.designator)})")
        lines.append(f"      (value {_kicad_quote(comp.value or comp.library_ref or '~')})")
        if comp.footprint:
            lines.append(f"      (footprint {_kicad_quote(comp.footprint)})")
        extra = sorted(
            (k, v) for k, v in comp.parameters.items()
            if v and k not in _COMP_OWN_FIELDS
        )
        if extra:
            lines.append("      (fields")
            for key, value in extra:
                lines.append(f"        (field (name {_kicad_quote(key)}) {_kicad_quote(value)})")
            lines.append("        )")
        if comp.library_ref:
            lib, part = _split_lib_id(comp.library_ref)
            lines.append(
                f"      (libsource (lib {_kicad_quote(lib)}) (part {_kicad_quote(part)}))"
            )
        lines.append(
            f"      (sheetpath (names {_kicad_quote(names)}) (tstamps {_kicad_quote(tstamps)}))"
        )
        if comp.unique_id:
            lines.append(f"      (tstamps {_kicad_quote(comp.unique_id)})")
        lines.append("      )")
    lines.append("  )")
    # libparts: one entry per distinct lib_id
    libparts = _libparts(sch)
    if libparts:
        lines.append("  (libparts")
        for lib, part, fields, pins in libparts:
            lines.append(f"    (libpart (lib {_kicad_quote(lib)}) (part {_kicad_quote(part)})")
            lines.append("      (fields")
            for key in sorted(fields):
                lines.append(
                    f"        (field (name {_kicad_quote(key)}) {_kicad_quote(fields[key])})"
                )
            lines.append("        )")
            if pins:
                lines.append("      (pins")
                for pin in pins:
                    name = "" if pin.name in (None, _NO_PIN_NAME) else pin.name
                    lines.append(
                        f"        (pin (num {_kicad_quote(pin.number)})"
                        f" (name {_kicad_quote(name)})"
                        f" (type {_kicad_quote(pin.electrical_type.value)}))"
                    )
                lines.append("        )")
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
