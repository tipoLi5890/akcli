"""`akcli` read-side commands: ``read net nets component pins export``.

Pure queries over a normalized schematic/library/PCB (plus ``pins``, the
op-list authoring helper that prints a symbol's world pin coordinates). No
command here writes; every heavy import is LAZY inside its handler.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..errors import EXIT
from ._shared import (
    _add_throttle_flags,
    _detect_format_ex,
    _did_you_mean,
    _draw_symbol_sources,
    _empty_import_warning,
    _dumps,
    _emit,
    _ExitWith,
    _load_cfg,
    _load_schematic,
    _match_limit,
    _net_display,
    _pin_net_index,
    _require_path,
    _schematic_md,
    _schematic_text,
    _stamp,
    _throttle_note,
)


def _read_detect_meta(obj, path: Path, fmt: str, method: str,
                      counts: dict[str, int], strict: bool) -> int:
    """Stamp detection metadata on ``obj`` + surface EMPTY_IMPORT; return exit."""
    meta = getattr(obj, "metadata", None)
    if isinstance(meta, dict):
        meta["detected_format"] = fmt
        meta["detection_method"] = method
        meta["object_counts"] = dict(counts)
    warn = _empty_import_warning(path, fmt, counts)
    if warn:
        warnings = getattr(obj, "warnings", None)
        if isinstance(warnings, list):
            warnings.append(warn)
        sys.stderr.write(f"warning: {warn}\n")
        if strict:
            return EXIT["FINDINGS"]
    return EXIT["OK"]


def _read_throttle_obj(args: argparse.Namespace, obj, attr: str,
                       key) -> dict | None:
    """Apply ``--match``/``--limit`` to one object list of a normalized model.

    Filters the model's list attribute IN PLACE (before any renderer runs), so
    text, ``--md`` and ``--json`` all honor the flags identically. Only active
    when a throttle flag was actually given, so the default full export stays
    byte-identical (golden snapshots). Returns the truncation meta (merged
    under ``"listing"`` in the JSON payload) or None when inactive — the
    graduated sibling of the all-or-nothing ``--summary``.
    """
    if getattr(args, "match", None) is None and getattr(args, "limit", None) is None:
        return None
    items, meta = _match_limit(getattr(obj, attr), args, key=key)
    setattr(obj, attr, items)
    _throttle_note(args, meta, attr)
    return meta


def _cmd_read(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    fmt, method = _detect_format_ex(path)
    strict = bool(getattr(args, "strict", False))

    def _summary_payload(obj, counts: dict[str, int]) -> dict:
        # --summary: the context-budget escape hatch — counts + metadata only,
        # never the full object arrays (a big board's full export can be MBs).
        from ..model import SCHEMA_VERSION
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source": str(path),
            "format": fmt,
            "counts": dict(counts),
            "metadata": dict(getattr(obj, "metadata", None) or {}),
        }
        warnings = getattr(obj, "warnings", None) or []
        if warnings:
            payload["warnings"] = list(warnings)
        return payload

    def _emit_schematic(obj) -> int:
        counts = {"components": len(obj.components), "nets": len(obj.nets)}
        counts["pins"] = sum(len(c.pins) for c in obj.components)
        code = _read_detect_meta(obj, path, fmt, method, counts, strict)
        if getattr(args, "summary", False):
            if args.json:
                _emit(_dumps(_summary_payload(obj, counts)))
            else:
                _emit("\n".join([f"schematic: {obj.source_path}",
                                 f"format:    {obj.source_format}"]
                                + [f"{k}: {v}" for k, v in counts.items()]))
            return code
        meta = _read_throttle_obj(args, obj, "components",
                                  key=lambda c: c.designator or "")
        if args.json:
            doc = obj.export()
            if meta is not None:
                doc["listing"] = {"filtered": "components", **meta}
            _emit(_dumps(doc))
        elif getattr(args, "md", False):
            _emit(_schematic_md(obj))
        else:
            _emit(_schematic_text(obj))
        return code

    def _emit_library(lib) -> int:
        counts = {"symbols": len(lib.symbols)}
        fps = getattr(lib, "footprints", None) or []
        if fps or fmt in ("altium_pcblib", "kicad_mod"):
            counts["footprints"] = len(fps)
        code = _read_detect_meta(lib, path, fmt, method, counts, strict)
        if getattr(args, "summary", False):
            if args.json:
                _emit(_dumps(_summary_payload(lib, counts)))
            else:
                _emit("\n".join([f"library: {lib.source_path}"]
                                + [f"{k}: {v}" for k, v in counts.items()]))
            return code
        meta = _read_throttle_obj(args, lib, "symbols",
                                  key=lambda s: s.name or "")
        if args.json:
            doc = lib.export()
            if meta is not None:
                doc["listing"] = {"filtered": "symbols", **meta}
            _emit(_dumps(doc))
        else:
            out = [f"library: {lib.source_path}", f"symbols: {len(lib.symbols)}"]
            for s in lib.symbols:
                out.append(f"  {s.name} (pins={len(s.pins)})")
            if fps:
                out.append(f"footprints: {len(fps)}")
                for f in fps:
                    out.append(f"  {f.name} (pads={len(f.pads)})")
            _emit("\n".join(out))
        return code

    def _emit_pcb(pcb) -> int:
        counts = {"footprints": len(pcb.footprints), "nets": len(pcb.nets)}
        code = _read_detect_meta(pcb, path, fmt, method, counts, strict)
        if getattr(args, "summary", False):
            if args.json:
                _emit(_dumps(_summary_payload(pcb, counts)))
            else:
                _emit("\n".join([f"pcb: {pcb.source_path}"]
                                + [f"{k}: {v}" for k, v in counts.items()]))
            return code
        meta = _read_throttle_obj(args, pcb, "footprints",
                                  key=lambda f: f.designator or "")
        if args.json:
            doc = pcb.export()
            if meta is not None:
                doc["listing"] = {"filtered": "footprints", **meta}
            _emit(_dumps(doc))
        else:
            out = [
                f"pcb: {pcb.source_path}",
                f"footprints: {len(pcb.footprints)}",
                f"nets: {len(pcb.nets)}",
            ]
            for f in pcb.footprints:
                out.append(f"  {f.designator}  {f.footprint_name or '-'}")
            _emit("\n".join(out))
        return code

    if fmt == "kicad_sch":
        from ..readers import kicad
        return _emit_schematic(kicad.read_sch(str(path)))

    if fmt == "altium_sch":
        from ..readers import altium_sch
        return _emit_schematic(altium_sch.read(str(path)))

    if fmt == "altium_schlib":
        from ..readers import altium_schlib
        return _emit_library(altium_schlib.read(str(path)))

    if fmt == "kicad_sym":
        from ..readers import kicad_lib
        return _emit_library(kicad_lib.read(str(path)))

    if fmt == "kicad_mod":
        from ..readers import footprint_lib
        return _emit_library(footprint_lib.read_kicad_mod(str(path)))

    if fmt == "altium_pcblib":
        from ..readers import footprint_lib
        return _emit_library(footprint_lib.read_pcblib(str(path)))

    if fmt == "altium_pcb":
        from ..readers import altium_pcb
        return _emit_pcb(altium_pcb.read(str(path)))

    if fmt == "kicad_pcb":
        from ..readers import kicad
        return _emit_pcb(kicad.read_pcb(str(path)))

    raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"], f"ERROR: unsupported/unknown format: {path}")


def _cmd_net(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    name = getattr(args, "name", None)

    if name:
        matches = [
            n for n in sch.nets
            if n.name == name or name in n.aliases or n.stable_id == name
        ]
        if not matches:
            if args.json:
                _emit(_dumps(_stamp({"found": False, "query": name,
                                     "kind": "net", "source": str(path)})))
            hint = _did_you_mean(name, (n.name for n in sch.nets if n.name))
            sys.stderr.write(f"no net named {name!r}{hint}\n")
            return EXIT["QUERY_MISS"]
        if args.json:
            from ..model import to_json
            _emit(_dumps([to_json(n) for n in matches]))
        else:
            out = []
            for n in matches:
                members = " ".join(f"{d}.{p}" for d, p in n.members)
                out.append(f"{_net_display(n)}: {members}")
                if n.aliases:
                    out.append(f"  aliases: {', '.join(n.aliases)}")
            _emit("\n".join(out))
        return EXIT["OK"]

    # no name: list all nets
    if args.json:
        from ..model import to_json
        _emit(_dumps([to_json(n) for n in sch.nets]))
    else:
        out = []
        for n in sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n))):
            members = " ".join(f"{d}.{p}" for d, p in n.members)
            out.append(f"{_net_display(n)}: {members}")
        _emit("\n".join(out))
    return EXIT["OK"]


def _cmd_nets(args: argparse.Namespace) -> int:
    """`nets <sch>` — one line per net: name -> sorted members.

    ``--intent-snapshot OUT.json`` also writes the current netlist as a
    ``checks.intent`` document (``protocol_version``/``mode``/``nets``), the
    input to ``akcli check --intent`` — the snapshot -> edit -> assert loop.
    """
    path = _require_path(args.path)
    sch = _load_schematic(path)

    snap_out = getattr(args, "intent_snapshot", None)
    if snap_out:
        from ..checks import intent as intent_mod
        doc = intent_mod.snapshot(
            sch, include_unnamed=getattr(args, "include_unnamed", False))
        rendered = _dumps(doc) + "\n"
        if snap_out == "-":
            _emit(rendered)
            return EXIT["OK"]
        Path(snap_out).write_text(rendered, encoding="utf-8")
        sys.stderr.write(f"wrote intent snapshot: {snap_out} "
                         f"({len(doc['nets'])} net(s) — assert with "
                         f"`akcli check <sch> --intent {snap_out}`)\n")

    ordered = sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n)))
    ordered, meta = _match_limit(ordered, args, key=lambda n: n.name or "")
    if args.json:
        _emit(_dumps(_stamp({
            "source": str(path),
            **meta,
            "nets": [{"name": n.name, "stable_id": n.stable_id,
                      "members": sorted(f"{d}.{p}" for d, p in n.members)}
                     for n in ordered],
        })))
    else:
        out = [f"{_net_display(n)}: "
               + ", ".join(sorted(f"{d}.{p}" for d, p in n.members))
               for n in ordered]
        _emit("\n".join(out) if out else "(no nets)")
        _throttle_note(args, meta, "nets")
    return EXIT["OK"]


def _cmd_component(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    ref = getattr(args, "ref", None)
    if not ref:
        # no REF: list every component (compact rows, throttleable)
        ordered = sorted(sch.components, key=lambda c: c.designator)
        ordered, meta = _match_limit(ordered, args, key=lambda c: c.designator)
        if args.json:
            from ..model import SCHEMA_VERSION
            _emit(_dumps({
                "schema_version": SCHEMA_VERSION,
                "source": str(path),
                **meta,
                "components": [
                    {"designator": c.designator, "library_ref": c.library_ref,
                     "value": c.value, "footprint": c.footprint,
                     "pins": len(c.pins)}
                    for c in ordered
                ],
            }))
        else:
            out = [f"{c.designator:<8} {c.library_ref or '-':<20} "
                   f"value={c.value or '-'} pins={len(c.pins)}"
                   for c in ordered]
            _emit("\n".join(out) if out else "(no components)")
            _throttle_note(args, meta, "components")
        return EXIT["OK"]

    comp = next((c for c in sch.components if c.designator == ref), None)
    if comp is None:
        if args.json:
            _emit(_dumps(_stamp({"found": False, "query": ref,
                                 "kind": "component", "source": str(path)})))
        hint = _did_you_mean(ref, (c.designator for c in sch.components))
        sys.stderr.write(f"no component {ref!r}{hint}\n")
        return EXIT["QUERY_MISS"]

    index = _pin_net_index(sch)
    if args.json:
        from ..model import SCHEMA_VERSION, to_json
        payload = to_json(comp)
        payload["schema_version"] = SCHEMA_VERSION
        payload["pin_nets"] = {
            p.number: (index.get((comp.designator, p.number)).name
                       if index.get((comp.designator, p.number)) else None)
            for p in comp.pins
        }
        _emit(_dumps(payload))
    else:
        out = [
            f"component: {comp.designator}",
            f"library:   {comp.library_ref or '-'}",
            f"value:     {comp.value or '-'}",
            f"footprint: {comp.footprint or '-'}",
            "pins:",
        ]
        for p in comp.pins:
            net = index.get((comp.designator, p.number))
            net_name = _net_display(net) if net else "(no net)"
            label = f" ({p.name})" if p.name else ""
            out.append(f"  {p.number}{label} -> {net_name}")
        _emit("\n".join(out))
    return EXIT["OK"]


def _cmd_pins(args: argparse.Namespace) -> int:
    """Print a symbol's pin world coordinates for a (hypothetical) placement.

    Op-list authoring helper: resolves ``lib_id`` from the same symbol sources
    the writer uses (``--symbols`` / config ``[paths]`` ``.kicad_sym`` entries)
    and reports every pin's number / name / electrical type and its **world**
    ``(x_mil, y_mil)`` for the given ``--at`` / ``--rotation`` / ``--mirror`` —
    the exact points wires, labels and power ports must target. Mirrors the
    writer's ``geometry.pin_world``, so a coordinate printed here is byte-for-byte
    where ``draw`` will place that pin.
    """
    lib_id = getattr(args, "lib_id", None)
    if not lib_id:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing lib_id (e.g. Device:R)")

    from .. import model as _model
    from .. import units as _units
    from ..readers import kicad_lib
    from ..writers import geometry
    from ..writers.lib_cache import _coerce_sources

    cfg = _load_cfg(args, None)
    libs = _coerce_sources(_draw_symbol_sources(args, cfg))
    sym = kicad_lib.resolve(lib_id, libs)          # raises SYMBOL_NOT_FOUND

    at = getattr(args, "at", None) or [0.0, 0.0]
    rot = int(getattr(args, "rotation", 0) or 0)
    mirror = getattr(args, "mirror", None) or "none"
    inst = _model.Component(
        designator="?", library_ref=lib_id,
        x_mil=at[0], y_mil=at[1], rotation=rot, mirror=mirror,
    )

    def _r(v: float):
        iv = round(v)
        return iv if abs(v - iv) < 1e-6 else round(v, 3)

    part_count = max(1, sym.part_count or 1)
    rows: list[dict] = []
    for unit in range(1, part_count + 1):
        for p in kicad_lib.unit_pins(sym, unit):
            wx, wy = geometry.pin_world(sym, inst, p)   # nm
            etype = getattr(p.electrical_type, "value", str(p.electrical_type))
            rows.append({
                "number": p.number, "name": p.name, "type": etype, "unit": unit,
                "x_mil": _r(_units.nm_to_mil(wx)), "y_mil": _r(_units.nm_to_mil(wy)),
            })

    if args.json:
        _emit(_dumps(_stamp({
            "lib_id": lib_id, "at": [at[0], at[1]], "rotation": rot,
            "mirror": mirror, "unit_count": part_count, "pins": rows,
        })))
    else:
        head = f"{lib_id}  @({_r(at[0])},{_r(at[1])}) rot={rot} mirror={mirror}"
        if part_count > 1:
            head += f"  [{part_count} units]"
        out = [head, f"  {'pin':>4}  {'name':<10} {'type':<10} "
                     f"{'unit':>4}  {'x_mil':>9} {'y_mil':>9}"]
        for r in rows:
            out.append(f"  {r['number']:>4}  {(r['name'] or ''):<10} {r['type']:<10} "
                       f"{r['unit']:>4}  {str(r['x_mil']):>9} {str(r['y_mil']):>9}")
        _emit("\n".join(out))
    return EXIT["OK"]


def _cmd_export(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    from .. import exporters
    text = exporters.export_netlist(sch, args.format)
    if args.json:
        # --json wraps the rendered netlist in an envelope instead of refusing:
        # the netlist text itself stays exactly what --format produces.
        from ..model import SCHEMA_VERSION
        payload = _dumps({
            "schema_version": SCHEMA_VERSION,
            "source": str(path),
            "format": args.format,
            "content": text,
        })
        if getattr(args, "output", None):
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
            sys.stderr.write(f"wrote {args.output}\n")
        else:
            _emit(payload)
        return EXIT["OK"]
    if getattr(args, "output", None):
        Path(args.output).write_text(text, encoding="utf-8")
        sys.stderr.write(f"wrote {args.output}\n")
    else:
        _emit(text)
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser("read", parents=[common], help="read + normalize a file")
    p.add_argument("path", nargs="?",
                   help="input file (.SchDoc/.SchLib/.PcbDoc/.PcbLib/.kicad_*)")
    p.add_argument("--md", action="store_true", help="render a Markdown summary")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 when a non-empty source normalizes to nothing "
                        "(EMPTY_IMPORT)")
    p.add_argument("--summary", action="store_true",
                   help="counts + metadata only, never the full object arrays "
                        "(the context-budget escape hatch for big boards)")
    _add_throttle_flags(p, "objects (components/symbols/footprints)")
    p.set_defaults(handler=_cmd_read)

    p = sub.add_parser("net", parents=[common], help="query nets")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("name", nargs="?", help="net name to query (omit to list all)")
    p.set_defaults(handler=_cmd_net)

    p = sub.add_parser("nets", parents=[common],
                       help="print every net -> sorted members "
                            "(+ --intent-snapshot for `check --intent`)")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--intent-snapshot", metavar="OUT.json",
                   help="also write the netlist as a design-intent JSON file "
                        "('-' = stdout) for `akcli check --intent`")
    p.add_argument("--include-unnamed", action="store_true",
                   help="intent snapshot: include unnamed nets "
                        "(keyed by stable id)")
    _add_throttle_flags(p, "nets")
    p.set_defaults(handler=_cmd_nets)

    p = sub.add_parser("component", parents=[common],
                       help="list components, or query one component's pin->net")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("ref", nargs="?",
                   help="component designator (e.g. U3); omit to list all")
    _add_throttle_flags(p, "components")
    p.set_defaults(handler=_cmd_component)

    p = sub.add_parser("pins", parents=[common],
                       help="print a symbol's pin world coords for a placement (op-list authoring)")
    p.add_argument("lib_id", nargs="?", help="symbol lib_id, e.g. Device:R or Timer:NE555P")
    p.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"),
                   help="placement origin in mils (default: 0 0)")
    p.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=0,
                   help="placement rotation (default: 0)")
    p.add_argument("--mirror", choices=["none", "x", "y"], default="none",
                   help="placement mirror (default: none)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.set_defaults(handler=_cmd_pins)

    p = sub.add_parser("export", parents=[common], help="emit a netlist")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--format", choices=["protel", "kicad", "csv"], default="protel",
                   help="netlist format (default: protel)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write to FILE instead of stdout")
    p.set_defaults(handler=_cmd_export)
