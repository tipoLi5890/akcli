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
    # Reader warnings (skipped Altium records, ignored project tops, undecoded
    # library primitives) are LOGS: they ride in the JSON payload's `warnings`
    # array AND go to stderr, so a text-mode read cannot look clean while the
    # model is knowingly incomplete. (`read` bypasses `_load_schematic`, which
    # is where every other command does this.)
    for w in list(getattr(obj, "warnings", None) or []):
        sys.stderr.write(f"warning: {w}\n")
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
        # Document parameters (Title/Revision/...) are cheap and identifying —
        # exactly what --summary is for. Absent when the file carries none.
        title_block = getattr(obj, "title_block", None) or {}
        if title_block:
            payload["title_block"] = dict(title_block)
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


def _net_json(n, rich: bool = False) -> dict:
    """The ONE net JSON shape shared by ``net`` and ``nets`` (members "D.P").

    ``rich`` adds the single-net diagnostics (aliases always ride along when
    present — additive optional keys, same base shape for every consumer).
    """
    d: dict = {"name": n.name, "stable_id": n.stable_id,
               "members": sorted(f"{des}.{p}" for des, p in n.members)}
    if n.aliases:
        d["aliases"] = sorted(n.aliases)
    if rich:
        d["is_named"] = n.is_named
        if n.source_names:
            d["source_names"] = list(n.source_names)
        if n.confidence != 1.0:
            d["confidence"] = n.confidence
        if n.merge_reasons:
            d["merge_reasons"] = list(n.merge_reasons)
    return d


def _pin_spec(spec: str) -> tuple[str, str]:
    """Split ``"REF.PIN"`` on the FIRST dot (the `netdiff`/`nets` convention)."""
    ref, sep, pin = spec.partition(".")
    if not sep or not ref or not pin:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: expected REF.PIN (e.g. U3.7), got {spec!r}")
    return ref, pin


def _emit_miss(query: str, kind: str, source: str) -> None:
    """Emit the standard ``--json`` "not found" envelope (net/component/pin
    lookups all share this shape: ``found: false`` + what was queried)."""
    _emit(_dumps(_stamp({"found": False, "query": query, "kind": kind,
                         "source": source})))


def _pin_or_miss(args: argparse.Namespace, path: Path, sch,
                 spec: str) -> tuple[str, str] | None:
    """Resolve ``REF.PIN`` against the schematic; on a miss, emit the standard
    ``found: false`` envelope + did-you-mean and return ``None``."""
    ref, pin = _pin_spec(spec)

    def miss(stderr_line: str) -> None:
        if args.json:
            _emit_miss(spec, "pin", path.as_posix())
        sys.stderr.write(stderr_line + "\n")

    comp = next((c for c in sch.components if c.designator == ref), None)
    if comp is None:
        hint = _did_you_mean(ref, (c.designator for c in sch.components))
        miss(f"no component {ref!r}{hint}")
        return None
    if all(p.number != pin for p in comp.pins):
        hint = _did_you_mean(pin, (p.number for p in comp.pins))
        valid = ", ".join(sorted({p.number for p in comp.pins}))
        miss(f"{ref} has no pin {pin!r}{hint} (valid pins: {valid})")
        return None
    return ref, pin


def _net_at(args: argparse.Namespace, path: Path, sch, spec: str) -> int:
    """``net --at REF.PIN`` — reverse lookup: which net is this pin on."""
    resolved = _pin_or_miss(args, path, sch, spec)
    if resolved is None:
        return EXIT["QUERY_MISS"]
    net = _pin_net_index(sch).get(resolved)
    if args.json:
        _emit(_dumps(_stamp({
            "source": path.as_posix(),
            "pin": f"{resolved[0]}.{resolved[1]}",
            "net": _net_json(net, rich=True) if net else None,
        })))
    else:
        label = _net_display(net) if net else "(no net)"
        _emit(f"{resolved[0]}.{resolved[1]} -> {label}")
    return EXIT["OK"]


def _net_same(args: argparse.Namespace, path: Path, sch,
              specs: list[str]) -> int:
    """``net --same A.PIN B.PIN`` — binary connectivity: same net or not."""
    ra = _pin_or_miss(args, path, sch, specs[0])
    rb = _pin_or_miss(args, path, sch, specs[1]) if ra is not None else None
    if ra is None or rb is None:
        return EXIT["QUERY_MISS"]
    index = _pin_net_index(sch)
    na, nb = index.get(ra), index.get(rb)
    same = na is not None and nb is not None and na.stable_id == nb.stable_id
    if args.json:
        _emit(_dumps(_stamp({
            "source": path.as_posix(),
            "pins": [f"{r}.{p}" for r, p in (ra, rb)],
            "same": same,
            "net": _net_json(na, rich=True) if same and na else None,
            "nets": {f"{r}.{p}": (index.get((r, p)).name
                                  if index.get((r, p)) else None)
                     for r, p in (ra, rb)},
        })))
    else:
        if same and na is not None:
            _emit(f"same net: yes — {_net_display(na)}")
        else:
            da = _net_display(na) if na else "(no net)"
            db = _net_display(nb) if nb else "(no net)"
            _emit(f"same net: no — {specs[0]} on {da}, {specs[1]} on {db}")
    return EXIT["OK"]


def _off_grid_50(v: float) -> bool:
    r = v % 50.0
    return min(r, 50.0 - r) > 0.01


def _net_why(args: argparse.Namespace, path: Path, sch, spec: str) -> int:
    """``net --why REF.PIN`` — pin-centric attachment diagnosis.

    Answers the question `component` cannot: WHY a pin shows ``(no net)`` —
    junction/wire-end attachment status, the nearest wire end / label / other
    pin with mil distances, and the off-grid trap. Same quantized helpers as
    ``netbuild``, so the verdict never disagrees with the net engine.
    """
    import math

    resolved = _pin_or_miss(args, path, sch, spec)
    if resolved is None:
        return EXIT["QUERY_MISS"]
    ref, pin = resolved
    net = _pin_net_index(sch).get(resolved)
    payload: dict = {
        "source": path.as_posix(),
        "pin": f"{ref}.{pin}",
        "net": _net_json(net, rich=True) if net else None,
    }
    hints: list[str] = []

    if path.suffix.lower() != ".kicad_sch":
        hints.append("geometry diagnosis needs a .kicad_sch (this input only "
                     "answers net membership)")
        payload.update({"at": None, "attachment": None, "nearest": None,
                        "off_grid_50mil": None, "hints": hints})
        return _emit_why(args, payload, net)

    from .. import netbuild
    from ..readers import kicad as _krd
    prims = _krd.read_primitives(path)
    ph = next((p for p in prims.pins if p.ref == resolved), None)
    if ph is None:
        hints.append("no placed pin handle found (is this unit placed on the "
                     "root sheet?)")
        payload.update({"at": None, "attachment": None, "nearest": None,
                        "off_grid_50mil": None, "hints": hints})
        return _emit_why(args, payload, net)

    q, on_seg = netbuild.quantize, netbuild.on_segment
    qp = q(ph.at)
    segs = [(w.a, w.b) for w in prims.wires if w.sheet == ph.sheet]
    qsegs = [(q(a), q(b)) for a, b in segs]
    at_junction = any(q(j.at) == qp for j in prims.junctions
                      if j.sheet == ph.sheet)
    at_wire_end = any(qp in (qa, qb) for qa, qb in qsegs)
    midspan = (not at_wire_end and not at_junction
               and any(on_seg(qp, qa, qb) for qa, qb in qsegs))
    label_on_tip = any(
        q(lb.at) == qp for lb in prims.labels
        if lb.sheet == ph.sheet and lb.scope != "hier" and lb.text)

    def _dist(pt: tuple[float, float]) -> float:
        return math.hypot(pt[0] - ph.at[0], pt[1] - ph.at[1])

    def _nearest(candidates):
        best, best_d = None, None
        for tag, pt in candidates:
            d = _dist(pt)
            if best_d is None or d < best_d:
                best, best_d = (tag, pt), d
        if best is None or best_d is None:
            return None
        (tag, pt) = best
        entry = {"at": [round(pt[0], 1), round(pt[1], 1)],
                 "dist_mil": round(best_d, 1)}
        if tag:
            entry.update(tag)
        return entry

    near_wire = _nearest(
        (None, e) for seg in segs for e in seg)
    near_label = _nearest(
        ({"text": lb.text}, lb.at) for lb in prims.labels
        if lb.sheet == ph.sheet and lb.scope != "hier" and lb.text)
    near_pin = _nearest(
        ({"pin": f"{p.ref[0]}.{p.ref[1]}"}, p.at) for p in prims.pins
        if p.sheet == ph.sheet and p.ref != resolved)

    off_grid = _off_grid_50(ph.at[0]) or _off_grid_50(ph.at[1])

    if midspan:
        hints.append(f"pin tip at ({round(ph.at[0])},{round(ph.at[1])}) "
                     "touches a wire MID-SPAN with no junction — not "
                     "connected; add an add_junction there or end the wire "
                     "on the pin tip")
    # a single-member net is electrically the same as "not connected" —
    # the pin only nets with itself, so the near-miss hints still apply.
    unconnected = net is None or len(net.members) <= 1
    if unconnected:
        if near_wire and 0 < near_wire["dist_mil"] <= 100:
            hints.append(f"nearest wire end is {near_wire['dist_mil']} mil "
                         f"away at ({near_wire['at'][0]},{near_wire['at'][1]})"
                         " — extend it to the pin tip")
        if near_label and 0 < near_label["dist_mil"] <= 100:
            hints.append(f"label '{near_label['text']}' floats "
                         f"{near_label['dist_mil']} mil away — anchor it "
                         "exactly on the pin tip")
    if off_grid:
        hints.append("pin tip is OFF the 50-mil grid — grid-snapped wires "
                     "cannot land on it; move the component onto the grid")

    payload.update({
        "at": [round(ph.at[0], 1), round(ph.at[1], 1)],
        "sheet": ph.sheet,
        "attachment": {"at_junction": at_junction, "at_wire_end": at_wire_end,
                       "midspan_touch": midspan, "label_on_tip": label_on_tip},
        "nearest": {"wire_end": near_wire, "label": near_label,
                    "pin": near_pin},
        "off_grid_50mil": off_grid,
        "hints": hints,
    })
    return _emit_why(args, payload, net)


def _emit_why(args: argparse.Namespace, payload: dict, net) -> int:
    if args.json:
        _emit(_dumps(_stamp(payload)))
        return EXIT["OK"]
    lines = [f"{payload['pin']} -> "
             + (_net_display(net) if net else "(no net)")]
    att = payload.get("attachment")
    if att:
        state = [k for k, v in att.items() if v]
        lines.append("attachment: " + (", ".join(state) if state else "none"))
    near = payload.get("nearest") or {}
    for kind in ("wire_end", "label", "pin"):
        e = near.get(kind)
        if e:
            what = e.get("text") or e.get("pin") or ""
            what = f" {what!r}" if what else ""
            lines.append(f"nearest {kind}:{what} {e['dist_mil']} mil away "
                         f"at ({e['at'][0]},{e['at'][1]})")
    for h in payload.get("hints") or []:
        lines.append(f"hint: {h}")
    _emit("\n".join(lines))
    return EXIT["OK"]


def _cmd_net(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    name = getattr(args, "name", None)
    at = getattr(args, "at", None)
    same = getattr(args, "same", None)
    why = getattr(args, "why", None)
    if sum(1 for x in (name, at, same, why) if x) > 1:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: give at most one of NAME, --at, --same, --why")
    if at:
        return _net_at(args, path, sch, at)
    if same:
        return _net_same(args, path, sch, same)
    if why:
        return _net_why(args, path, sch, why)

    if name:
        matches = [
            n for n in sch.nets
            if n.name == name or name in n.aliases or n.stable_id == name
        ]
        if not matches:
            if args.json:
                _emit_miss(name, "net", str(path))
            hint = _did_you_mean(name, (n.name for n in sch.nets if n.name))
            sys.stderr.write(f"no net named {name!r}{hint}\n")
            return EXIT["QUERY_MISS"]
        if args.json:
            _emit(_dumps(_stamp({
                "source": path.as_posix(),
                "query": name,
                "nets": [_net_json(n, rich=True) for n in matches],
            })))
        else:
            out = []
            for n in matches:
                members = " ".join(f"{d}.{p}" for d, p in n.members)
                out.append(f"{_net_display(n)}: {members}")
                if n.aliases:
                    out.append(f"  aliases: {', '.join(n.aliases)}")
            _emit("\n".join(out))
        return EXIT["OK"]

    # no name: list all nets — SAME envelope + net shape as `nets`, so an
    # agent needs exactly one parser for both commands.
    ordered = sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n)))
    ordered, meta = _match_limit(ordered, args, key=lambda n: n.name or "")
    if args.json:
        _emit(_dumps(_stamp({
            "source": path.as_posix(),
            **meta,
            "nets": [_net_json(n) for n in ordered],
        })))
    else:
        out = []
        for n in ordered:
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
        Path(snap_out).write_text(rendered, encoding="utf-8", newline="\n")
        sys.stderr.write(f"wrote intent snapshot: {snap_out} "
                         f"({len(doc['nets'])} net(s) — assert with "
                         f"`akcli check <sch> --intent {snap_out}`)\n")

    ordered = sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n)))
    ordered, meta = _match_limit(ordered, args, key=lambda n: n.name or "")
    if args.json:
        _emit(_dumps(_stamp({
            # as_posix(): forward slashes on every platform, so the golden
            # nets snapshots (invoked with repo-relative paths) are stable
            # across OSes — Windows str(Path) would emit backslashes here.
            "source": path.as_posix(),
            **meta,
            "nets": [_net_json(n) for n in ordered],
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
            _emit_miss(ref, "component", str(path))
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
    libs = _coerce_sources(_draw_symbol_sources(args, cfg, lib_ids=[lib_id]))
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


def _cmd_bbox(args: argparse.Namespace) -> int:
    """Print a symbol's world bounding boxes for a (hypothetical) placement.

    Placement-planning helper (the sibling of ``akcli pins``): reports, per
    unit, the drawn-body box and the full box (body UNION pin tips) in world
    mils for the given ``--at``/``--rotation``/``--mirror`` — so an agent can
    reserve space and pick spacings BEFORE placing. Uses the same transform
    chain as the writer (``geometry.world_box_from_extent`` / ``pin_world``),
    so the boxes can never disagree with where ``draw`` puts the part.
    Pin-only symbols (no drawn body, e.g. power stubs) fall back to the pin
    bounding box and say so via ``pin_only``.
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
    libs = _coerce_sources(_draw_symbol_sources(args, cfg, lib_ids=[lib_id]))
    sym = kicad_lib.resolve(lib_id, libs)          # raises SYMBOL_NOT_FOUND

    at = getattr(args, "at", None) or [0.0, 0.0]
    rot = int(getattr(args, "rotation", 0) or 0)
    mirror = getattr(args, "mirror", None) or "none"
    origin_nm = (_units.mil_to_nm(at[0]), _units.mil_to_nm(at[1]))
    inst = _model.Component(
        designator="?", library_ref=lib_id,
        x_mil=at[0], y_mil=at[1], rotation=rot, mirror=mirror,
    )

    def _r(v: float):
        iv = round(v)
        return iv if abs(v - iv) < 1e-6 else round(v, 3)

    def _box_mil(box_nm) -> list:
        return [_r(_units.nm_to_mil(v)) for v in box_nm]

    part_count = max(1, sym.part_count or 1)
    units_out: list[dict] = []
    for unit in range(1, part_count + 1):
        ext = kicad_lib.body_extent_mil(sym, unit)
        body_nm = (None if ext is None
                   else geometry.world_box_from_extent(ext, rot, mirror, origin_nm))
        pts = [geometry.pin_world(sym, inst, p)
               for p in kicad_lib.unit_pins(sym, unit)]
        boxes = ([body_nm] if body_nm is not None else []) + [
            (x, y, x, y) for (x, y) in pts]
        if not boxes:                       # neither body nor pins: origin point
            boxes = [(origin_nm[0], origin_nm[1], origin_nm[0], origin_nm[1])]
        full_nm = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes))
        full = _box_mil(full_nm)
        units_out.append({
            "unit": unit,
            "body_box": None if body_nm is None else _box_mil(body_nm),
            "full_box": full,
            "width_mil": _r(full[2] - full[0]),
            "height_mil": _r(full[3] - full[1]),
            "pin_only": body_nm is None,
        })

    if args.json:
        _emit(_dumps(_stamp({
            "lib_id": lib_id, "at": [at[0], at[1]], "rotation": rot,
            "mirror": mirror, "unit_count": part_count,
            "recommended_min_spacing_mil": 400,
            "units": units_out,
        })))
    else:
        head = f"{lib_id}  @({_r(at[0])},{_r(at[1])}) rot={rot} mirror={mirror}"
        if part_count > 1:
            head += f"  [{part_count} units]"
        out = [head,
               f"  {'unit':>4}  {'body_box (x0,y0,x1,y1) mil':<30} "
               f"{'full_box (body+pins)':<30} {'w x h':<14}"]
        for u in units_out:
            body = ("(pin-only)" if u["body_box"] is None
                    else str(tuple(u["body_box"])))
            out.append(f"  {u['unit']:>4}  {body:<30} "
                       f"{str(tuple(u['full_box'])):<30} "
                       f"{u['width_mil']}x{u['height_mil']}")
        out.append("  keep neighboring parts >= 400 mil apart "
                   "(see `akcli pins` for exact pin coordinates)")
        _emit("\n".join(out))
    return EXIT["OK"]


def _cmd_groups(args: argparse.Namespace) -> int:
    """`groups <sch>` — functional groups on a sheet; ``--frame`` draws borders.

    List mode reports every group recovered from the hidden ``Group`` symbol
    property: members, world bounding box, and whether its visual frame is
    present. ``--frame`` (dry-run unless ``--apply``) emits the border
    rectangle + title ops through the standard draw pipeline — keyed uuids
    make a re-run replace stale frames in place after parts move.
    """
    path = _require_path(args.path, "schematic .kicad_sch")
    if not str(path).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: groups works on .kicad_sch")
    from ..groupframe import group_report
    if getattr(args, "frame", False):
        return _cmd_groups_frame(args, path)
    rows = group_report(path)
    if args.json:
        _emit(_dumps(_stamp({"source": str(path), "groups": rows})))
        return EXIT["OK"]
    if not rows:
        _emit("no functional groups (no 'Group' symbol properties on this "
              "sheet; place with a group tag or pass --groups to arrange)")
        return EXIT["OK"]
    out = [f"  {'group':<16} {'members':>7}  {'box (x0,y0,x1,y1) mil':<28} "
           f"{'w x h':<12} frame"]
    for r in rows:
        box = ",".join(f"{v:g}" for v in r["box_mil"])
        out.append(f"  {r['name']:<16} {len(r['members']):>7}  ({box})"
                   f"{'':<4} {r['width_mil']:g}x{r['height_mil']:g}"
                   f"{'':<4} {'yes' if r['has_frame'] else 'no'}")
        out.append(f"    {', '.join(r['members'])}")
    _emit("\n".join(out))
    return EXIT["OK"]


def _cmd_groups_frame(args: argparse.Namespace, path) -> int:
    from ..groupframe import FRAME_MARGIN_MIL, plan_frames
    margin = getattr(args, "margin", None) or FRAME_MARGIN_MIL
    ops_list = plan_frames(path, margin_mil=margin)
    do_apply = bool(getattr(args, "apply", False))
    if not ops_list:
        _emit("no functional groups to frame")
        return EXIT["OK"]
    from .. import journal as _journal_mod
    from ..writers import kicad as kwriter
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": path.name, "ops": ops_list}
    cfg = _load_cfg(args, path)
    findings: list = []
    results = kwriter.apply(
        oplist, str(path), apply=do_apply,
        sources=_draw_symbol_sources(args, cfg), verify_out=findings,
        backup_dir=(_journal_mod.backups_dir(path) if do_apply else None),
        allow_open=bool(getattr(args, "allow_open", False)))
    from ..errors import EXIT as _EXIT
    ok = (all(r.status == "ok" for r in results)
          and not any(f.severity.value in ("error", "critical") for f in findings))
    if do_apply and ok:
        _journal_mod.record(
            path, "groups-frame", "applied", op_count=len(ops_list),
            backup=f"{_journal_mod.DIR_NAME}/{_journal_mod.BACKUP_DIR_NAME}/"
                   f"{path.name}.bak")
    frames = len(ops_list) // 2
    if args.json:
        _emit(_dumps(_stamp({
            "source": str(path), "applied": bool(do_apply and ok),
            "frames": frames,
            "ops": [r.to_dict() for r in results],
        })))
    elif not ok:
        for r in results:
            if r.status != "ok":
                _emit(f"ERROR [{r.op_index}] {r.op}: {r.error_code}: {r.message}")
    elif do_apply:
        _emit(f"groups --frame: drew {frames} frame(s) on {path.name} "
              f"(backup {path.name}.bak; `akcli undo` reverts)")
    else:
        _emit(f"dry-run: {frames} frame(s) planned — re-run with --apply")
    return EXIT["OK"] if ok else _EXIT["OPLIST"]


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
            Path(args.output).write_text(payload + "\n", encoding="utf-8", newline="\n")
            sys.stderr.write(f"wrote {args.output}\n")
        else:
            _emit(payload)
        return EXIT["OK"]
    if getattr(args, "output", None):
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
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

    p = sub.add_parser("net", parents=[common],
                       help="query nets: by name, all, or pin-level "
                            "(--at / --same / --why REF.PIN)")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("name", nargs="?", help="net name to query (omit to list all)")
    p.add_argument("--at", metavar="REF.PIN",
                   help="reverse lookup: which net is this pin on")
    p.add_argument("--same", nargs=2, metavar=("A.PIN", "B.PIN"),
                   help="binary connectivity: are these two pins on one net")
    p.add_argument("--why", metavar="REF.PIN",
                   help="attachment diagnosis: why is this pin (not) "
                        "connected — junction/wire-end status, nearest wire "
                        "end/label/pin with mil distances, off-grid flag")
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

    p = sub.add_parser("bbox", parents=[common],
                       help="print a symbol's world bounding boxes for a "
                            "placement (spacing planning; sibling of `pins`)")
    p.add_argument("lib_id", nargs="?", help="symbol lib_id, e.g. Device:R or Timer:NE555P")
    p.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"),
                   help="placement origin in mils (default: 0 0)")
    p.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=0,
                   help="placement rotation (default: 0)")
    p.add_argument("--mirror", choices=["none", "x", "y"], default="none",
                   help="placement mirror (default: none)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.set_defaults(handler=_cmd_bbox)

    p = sub.add_parser("groups", parents=[common],
                       help="list a sheet's functional groups (from the Group "
                            "property); --frame draws border+title per group")
    p.add_argument("path", nargs="?", help="input .kicad_sch")
    p.add_argument("--frame", action="store_true",
                   help="draw/refresh a border rectangle + title per group "
                        "(dry-run unless --apply; keyed uuids replace stale "
                        "frames in place)")
    p.add_argument("--apply", action="store_true",
                   help="with --frame: actually write (default is a dry-run)")
    p.add_argument("--margin", type=float, metavar="MIL",
                   help="frame padding around the group box (default 200)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra symbol source for the write pipeline")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="write even when a KiCad GUI lock file is present")
    p.set_defaults(handler=_cmd_groups)

    p = sub.add_parser("export", parents=[common], help="emit a netlist")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--format", choices=["protel", "kicad", "csv"], default="protel",
                   help="netlist format (default: protel)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write to FILE instead of stdout")
    p.set_defaults(handler=_cmd_export)
