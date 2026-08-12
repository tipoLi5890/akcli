"""`akcli doc` — pinout book: a human-readable design document from a schematic.

Composes, from the normalized model alone (no EDA install, no network):

* per-IC/connector **pin tables** (pin number, name, electrical type, and the
  net each pin actually landed on — the as-drawn pinout, not the datasheet's),
* the **power-rail summary** (the same analysis as ``review tree``),
* the **BOM** (real, in-BOM components grouped by value/footprint/symbol).

Output is deterministic Markdown (same input bytes -> same output bytes): no
timestamps, all sections sorted. ``--json`` emits the same content as a
structured payload for machine consumption.
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

from ..classify import natkey as _natkey
from ..errors import EXIT
from ._shared import _dumps, _emit, _load_schematic, _require_path, _stamp

# ROADMAP v0.10: "per-IC/connector pin tables" — U = IC, J/CN/P = connector.
_DEFAULT_REFS = "U*,J*,CN*,P*"


def _md_escape(s: str) -> str:
    """Make a value safe inside a Markdown table cell."""
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _match_refs(designator: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(designator, pat) for pat in patterns)


def _pin_net_map(sch) -> dict:
    """``(designator, pin_number) -> net name`` for every connected pin."""
    return {
        (ref, pin): net.name
        for net in sch.nets
        for ref, pin in net.members
    }


def _real_components(sch) -> list:
    """BOM-relevant components: designated, non-power, ``in_bom``."""
    return [
        c for c in sch.components
        if c.in_bom and not c.undesignated
        and not c.designator.startswith(("#", "$"))
    ]


def _component_entry(comp, pin_nets: dict) -> dict:
    pins = [
        {
            "number": pin.number,
            "name": pin.name or "",
            "type": pin.electrical_type.value,
            "net": pin_nets.get((comp.designator, pin.number), ""),
        }
        for pin in sorted(comp.pins, key=lambda p: _natkey(p.number))
    ]
    return {
        "ref": comp.designator,
        "lib_id": comp.library_ref or "",
        "value": comp.value or "",
        "footprint": comp.footprint or "",
        "dnp": comp.dnp,
        "pins": pins,
    }


def _bom_rows(sch) -> list:
    """Group real components by (value, lib_id, footprint), refs natural-sorted."""
    groups: dict = {}
    for c in _real_components(sch):
        key = (c.value or "", c.library_ref or "", c.footprint or "", c.dnp)
        groups.setdefault(key, []).append(c.designator)
    rows = [
        {
            "qty": len(refs),
            "refs": sorted(refs, key=_natkey),
            "value": value,
            "lib_id": lib_id,
            "footprint": footprint,
            "dnp": dnp,
        }
        for (value, lib_id, footprint, dnp), refs in groups.items()
    ]
    rows.sort(key=lambda r: _natkey(r["refs"][0]))
    return rows


def _build_doc(sch, patterns: list[str]) -> dict:
    from ..review import tree as treemod
    pin_nets = _pin_net_map(sch)
    components = [
        _component_entry(c, pin_nets)
        for c in sorted(sch.components, key=lambda c: _natkey(c.designator))
        if _match_refs(c.designator, patterns) and not c.undesignated
        and not c.designator.startswith(("#", "$"))
    ]
    return {
        "source": Path(sch.source_path).as_posix(),
        "refs": patterns,
        "component_count": len(sch.components),
        "net_count": len(sch.nets),
        "components": components,
        "rails": treemod.power_tree(sch)["rails"],
        "bom": _bom_rows(sch),
    }


def _render_markdown(doc: dict) -> str:
    lines: list[str] = []
    name = Path(doc["source"]).name
    lines += [
        f"# {name} — pinout book",
        "",
        f"- source: `{doc['source']}`",
        f"- components: {doc['component_count']}, nets: {doc['net_count']}",
        f"- pin tables for refs matching: `{','.join(doc['refs'])}`",
        "",
        "## Pinouts",
        "",
    ]
    if not doc["components"]:
        lines += ["(no components match — widen with `--refs`)", ""]
    for comp in doc["components"]:
        title = comp["ref"]
        if comp["value"]:
            title += f" — {_md_escape(comp['value'])}"
        if comp["lib_id"]:
            title += f" ({_md_escape(comp['lib_id'])})"
        if comp["dnp"]:
            title += " [DNP]"
        lines += [f"### {title}", ""]
        if comp["footprint"]:
            lines += [f"footprint: `{comp['footprint']}`", ""]
        lines += ["| Pin | Name | Type | Net |", "|---|---|---|---|"]
        for pin in comp["pins"]:
            net = _md_escape(pin["net"]) if pin["net"] else "—"
            lines.append(f"| {_md_escape(pin['number'])} "
                         f"| {_md_escape(pin['name'])} "
                         f"| {pin['type']} | {net} |")
        lines.append("")
    lines += ["## Power rails", ""]
    if not doc["rails"]:
        lines += ["(no power-recognised rails)", ""]
    else:
        lines += ["| Rail | Voltage | Regulator | Consumers | Decoupling |",
                  "|---|---|---|---|---|"]
        for r in doc["rails"]:
            volts = f"{r['voltage']:g} V" if r.get("voltage") is not None else "?"
            reg = r.get("regulator")
            reg_s = (f"{reg['ref']} (FB {reg['fb_pin']})" if reg else "—")
            cons = ", ".join(r.get("consumers") or []) or "—"
            lines.append(f"| {_md_escape(r['net'])} | {volts} | {_md_escape(reg_s)} "
                         f"| {_md_escape(cons)} | {r['decoupling_caps']} cap(s) |")
        lines.append("")
    lines += ["## BOM", ""]
    if not doc["bom"]:
        lines += ["(no BOM components)", ""]
    else:
        lines += ["| Qty | Refs | Value | Footprint | Symbol |",
                  "|---|---|---|---|---|"]
        for row in doc["bom"]:
            refs = ", ".join(row["refs"])
            value = _md_escape(row["value"]) or "—"
            if row["dnp"]:
                value += " [DNP]"
            lines.append(f"| {row['qty']} | {_md_escape(refs)} | {value} "
                         f"| {_md_escape(row['footprint']) or '—'} "
                         f"| {_md_escape(row['lib_id']) or '—'} |")
        lines.append("")
    return "\n".join(lines)


def _cmd_doc(args: argparse.Namespace) -> int:
    path = _require_path(getattr(args, "path", None), "schematic")
    sch = _load_schematic(path)
    patterns = [p.strip() for p in
                (getattr(args, "refs", None) or _DEFAULT_REFS).split(",")
                if p.strip()]
    doc = _build_doc(sch, patterns)
    if args.json:
        _emit(_dumps(_stamp(doc)))
        return EXIT["OK"]
    text = _render_markdown(doc)
    out = getattr(args, "out", None)
    if out:
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        sys.stderr.write(f"doc: wrote {out} ({len(text.encode('utf-8'))} bytes)\n")
        return EXIT["OK"]
    _emit(text.rstrip("\n"))
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "doc", parents=[common],
        help="generate a Markdown pinout book (pin tables + power rails + BOM)")
    p.add_argument("path", nargs="?", help="schematic (.kicad_sch or .SchDoc)")
    p.add_argument("-o", "--out", metavar="FILE",
                   help="write the Markdown here instead of stdout")
    p.add_argument("--refs", metavar="GLOBS",
                   help="comma-separated refdes globs to table "
                        f"(default: {_DEFAULT_REFS})")
    p.set_defaults(handler=_cmd_doc)
