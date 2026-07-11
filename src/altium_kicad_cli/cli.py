"""argparse dispatch + exit codes for the ``akcli`` CLI (SPEC §3.1).

Subcommands ``read net nets component check diff pinmap export plan draw
relink-symbols`` (and more) are live. ``plan``/``draw`` drive the KiCad op-list
executor (``draw`` writes only on ``--apply``) and report a before/after net
connectivity diff (``--no-net-diff`` opts out; ``draw --apply --strict-nets``
refuses splits/merges of named nets). Every handler does its heavy imports
LAZILY (inside the handler) so ``akcli --help`` / ``--version`` run from a
clean checkout with only the Foundation modules present.

Conventions
-----------
* **stdout = data, stderr = logs.** Machine-readable output goes to stdout;
  diagnostics/verbose logs go to stderr.
* Global flags (``--json -C/--config -v/-q --no-color --debug``) are accepted by
  every subcommand.
* ``check``/``diff``/``pinmap`` are lint-style: exit ``1`` when actionable findings
  (severity ≥ WARNING) are present, ``0`` when clean; ``--exit-zero`` forces ``0``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as _config
from . import report as _report
from .errors import EXIT, AkcliError, as_error, to_exit
from .ops import PROTOCOL_VERSION

# OLE2/CFBF magic (all Altium binary docs).
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Extension -> internal format token.
_EXT_FORMAT = {
    ".schdoc": "altium_sch",
    ".schlib": "altium_schlib",
    ".pcbdoc": "altium_pcb",
    ".kicad_sch": "kicad_sch",
    ".kicad_pcb": "kicad_pcb",
    ".kicad_sym": "kicad_sym",
    ".prjpcb": "altium_prj",
}


class _ExitWith(Exception):
    """Internal control-flow signal: stop the handler with ``code`` + stderr ``msg``."""

    def __init__(self, code: int, msg: str = "") -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


# --------------------------------------------------------------------------- #
# logging / small helpers
# --------------------------------------------------------------------------- #
def _log(args: argparse.Namespace, level: int, msg: str) -> None:
    """Emit a verbosity-gated log line to stderr (never stdout)."""
    if getattr(args, "quiet", False):
        return
    if getattr(args, "verbose", 0) >= level:
        sys.stderr.write(msg + "\n")


def _emit(text: str) -> None:
    """Write a data payload to stdout with exactly one trailing newline."""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def _require_path(value: str | None, what: str = "input file") -> Path:
    if not value:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: missing {what}")
    return Path(value)


def _detect_format(path: Path) -> str:
    """Detect the file format by extension, falling back to a magic-byte sniff."""
    ext = path.suffix.lower()
    if ext in _EXT_FORMAT:
        return _EXT_FORMAT[ext]
    try:
        head = path.open("rb").read(64)
    except OSError:
        return "unknown"
    if head.startswith(_OLE_MAGIC):
        return "altium_sch"  # bare OLE2: assume schematic doc
    stripped = head.lstrip()
    if stripped.startswith(b"(kicad_symbol_lib"):
        return "kicad_sym"
    if stripped.startswith(b"(kicad_sch"):
        return "kicad_sch"
    if stripped.startswith(b"(kicad_pcb"):
        return "kicad_pcb"
    return "unknown"


def _load_schematic(path: Path):
    """Read ``path`` into a normalized ``Schematic`` or raise ``_ExitWith``.

    KiCad schematics and non-schematic Altium docs are not yet schematics here,
    so they surface as exit ``5`` (unsupported format) with a clear notice.
    """
    def _warned(sch):
        # reader warnings (e.g. duplicate designators) are logs, not data
        for w in getattr(sch, "warnings", None) or []:
            sys.stderr.write(f"warning: {w}\n")
        return sch

    fmt = _detect_format(path)
    if fmt == "altium_sch":
        from .readers import altium_sch  # lazy
        return _warned(altium_sch.read(str(path)))
    if fmt == "altium_prj":
        from .readers import altium_prj  # lazy
        return _warned(altium_prj.read(str(path)))
    if fmt == "kicad_sch":
        from .readers import kicad  # lazy
        return _warned(kicad.read_sch(str(path)))
    if fmt == "kicad_pcb":
        raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"],
                        "ERROR: .kicad_pcb is a PCB, not a schematic (use `read`)")
    if fmt == "altium_schlib":
        raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"],
                        "ERROR: .SchLib is a symbol library, not a schematic (use `read`)")
    if fmt == "altium_pcb":
        raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"],
                        "ERROR: .PcbDoc is a PCB, not a schematic (use `read`)")
    raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"], f"ERROR: unsupported/unknown format: {path}")


def _load_cfg(args: argparse.Namespace, near: Path | None):
    """Load config from ``-C/--config`` or walk-up discovery; default empty Config."""
    if getattr(args, "config", None):
        return _config.load_config(Path(args.config))
    start = near.parent if near is not None else None
    found = _config.find_config(start)
    if found is None:
        return _config.Config()
    _log(args, 1, f"using config {found}")
    return _config.load_config(found)


def _pin_net_index(sch) -> dict:
    """Map every ``(designator, pin_number)`` -> the ``Net`` it belongs to."""
    index: dict = {}
    for net in sch.nets:
        for ref in net.members:
            index[ref] = net
    return index


def _schematic_meta(sch) -> dict:
    """Build the report metadata header (passive ratio, No-ERC, unnamed nets, frac)."""
    from .model import PinType  # lazy
    meta = dict(getattr(sch, "metadata", None) or {})
    total = sum(len(c.pins) for c in sch.components)
    if total:
        passive = sum(
            1 for c in sch.components for p in c.pins
            if p.electrical_type == PinType.PASSIVE
        )
        meta.setdefault("passive_pin_ratio", round(passive / total, 3))
    meta.setdefault("no_erc_suppressed", len(getattr(sch, "no_erc_points", []) or []))
    meta.setdefault("unnamed_net_count", sum(1 for n in sch.nets if not n.name))
    return meta


def _findings_exit(findings: list, args: argparse.Namespace) -> int:
    """Lint-style exit: 1 if any actionable (≥WARNING) finding, else 0."""
    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    actionable = {
        _report.Severity.WARNING,
        _report.Severity.ERROR,
        _report.Severity.CRITICAL,
    }
    if any(f.severity in actionable for f in findings):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)


def _did_you_mean(name: str, candidates) -> str:
    """`" (did you mean: x, y?)"` for a typo'd name, or `""` when nothing is close."""
    import difflib
    close = difflib.get_close_matches(str(name), sorted(candidates), n=2)
    return f" (did you mean: {', '.join(close)}?)" if close else ""


# --------------------------------------------------------------------------- #
# render helpers
# --------------------------------------------------------------------------- #
def _net_display(net) -> str:
    return net.name if net.name else f"<unnamed {net.stable_id}>"


def _schematic_text(sch) -> str:
    lines = [
        f"schematic: {sch.source_path}",
        f"format:    {sch.source_format}",
        f"components: {len(sch.components)}",
        f"nets:       {len(sch.nets)}",
        "",
        "components:",
    ]
    for c in sorted(sch.components, key=lambda c: c.designator):
        lines.append(
            f"  {c.designator:<8} {c.library_ref or '-':<14} "
            f"value={c.value or '-'} pins={len(c.pins)}"
        )
    lines.append("")
    lines.append("nets:")
    for n in sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n))):
        members = " ".join(f"{d}.{p}" for d, p in n.members)
        lines.append(f"  {_net_display(n)}: {members}")
    return "\n".join(lines)


def _schematic_md(sch) -> str:
    lines = [
        f"# Schematic `{Path(sch.source_path).name}`",
        "",
        f"- **format**: {sch.source_format}",
        f"- **components**: {len(sch.components)}",
        f"- **nets**: {len(sch.nets)}",
        "",
        "## Components",
        "",
        "| Designator | Library | Value | Pins |",
        "| --- | --- | --- | --- |",
    ]
    for c in sorted(sch.components, key=lambda c: c.designator):
        lines.append(
            f"| {c.designator} | {c.library_ref or ''} | {c.value or ''} | {len(c.pins)} |"
        )
    lines += ["", "## Nets", "", "| Net | Members |", "| --- | --- |"]
    for n in sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n))):
        members = ", ".join(f"{d}.{p}" for d, p in n.members)
        lines.append(f"| {_net_display(n)} | {members} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #
def _cmd_read(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    fmt = _detect_format(path)
    if fmt == "kicad_sch":
        from .readers import kicad
        obj = kicad.read_sch(str(path))
        if args.json:
            _emit(_dumps(obj.export()))
        elif getattr(args, "md", False):
            _emit(_schematic_md(obj))
        else:
            _emit(_schematic_text(obj))
        return EXIT["OK"]

    if fmt == "altium_sch":
        from .readers import altium_sch
        obj = altium_sch.read(str(path))
        if args.json:
            _emit(_dumps(obj.export()))
        elif getattr(args, "md", False):
            _emit(_schematic_md(obj))
        else:
            _emit(_schematic_text(obj))
        return EXIT["OK"]

    if fmt == "altium_schlib":
        from .readers import altium_schlib
        lib = altium_schlib.read(str(path))
        if args.json:
            _emit(_dumps(lib.export()))
        else:
            out = [f"library: {lib.source_path}", f"symbols: {len(lib.symbols)}"]
            for s in lib.symbols:
                out.append(f"  {s.name} (pins={len(s.pins)})")
            _emit("\n".join(out))
        return EXIT["OK"]

    if fmt == "kicad_sym":
        from .readers import kicad_lib
        lib = kicad_lib.read(str(path))
        if args.json:
            _emit(_dumps(lib.export()))
        else:
            out = [f"library: {lib.source_path}", f"symbols: {len(lib.symbols)}"]
            for s in lib.symbols:
                out.append(f"  {s.name} (pins={len(s.pins)})")
            _emit("\n".join(out))
        return EXIT["OK"]

    if fmt == "altium_pcb":
        from .readers import altium_pcb
        pcb = altium_pcb.read(str(path))
        if args.json:
            _emit(_dumps(pcb.export()))
        else:
            out = [
                f"pcb: {pcb.source_path}",
                f"footprints: {len(pcb.footprints)}",
                f"nets: {len(pcb.nets)}",
            ]
            for f in pcb.footprints:
                out.append(f"  {f.designator}  {f.footprint_name or '-'}")
            _emit("\n".join(out))
        return EXIT["OK"]

    if fmt == "kicad_pcb":
        from .readers import kicad
        pcb = kicad.read_pcb(str(path))
        if args.json:
            _emit(_dumps(pcb.export()))
        else:
            out = [
                f"pcb: {pcb.source_path}",
                f"footprints: {len(pcb.footprints)}",
                f"nets: {len(pcb.nets)}",
            ]
            for f in pcb.footprints:
                out.append(f"  {f.designator}  {f.footprint_name or '-'}")
            _emit("\n".join(out))
        return EXIT["OK"]

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
            sys.stderr.write(f"no net named {name!r}\n")
            return EXIT["OK"]
        if args.json:
            from .model import to_json
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
        from .model import to_json
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
        from .checks import intent as intent_mod
        doc = intent_mod.snapshot(
            sch, include_unnamed=getattr(args, "include_unnamed", False))
        rendered = _dumps(doc) + "\n"
        if snap_out == "-":
            sys.stdout.write(rendered)
            return EXIT["OK"]
        Path(snap_out).write_text(rendered, encoding="utf-8")
        sys.stderr.write(f"wrote intent snapshot: {snap_out} "
                         f"({len(doc['nets'])} net(s) — assert with "
                         f"`akcli check <sch> --intent {snap_out}`)\n")

    ordered = sorted(sch.nets, key=lambda n: (n.name is None, _net_display(n)))
    if args.json:
        _emit(_dumps({
            "source": str(path),
            "nets": [{"name": n.name, "stable_id": n.stable_id,
                      "members": sorted(f"{d}.{p}" for d, p in n.members)}
                     for n in ordered],
        }))
    else:
        out = [f"{_net_display(n)}: "
               + ", ".join(sorted(f"{d}.{p}" for d, p in n.members))
               for n in ordered]
        _emit("\n".join(out) if out else "(no nets)")
    return EXIT["OK"]


def _cmd_component(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    ref = getattr(args, "ref", None)
    if not ref:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing component designator")

    comp = next((c for c in sch.components if c.designator == ref), None)
    if comp is None:
        sys.stderr.write(f"no component {ref!r}\n")
        return EXIT["OK"]

    index = _pin_net_index(sch)
    if args.json:
        from .model import SCHEMA_VERSION, to_json
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


def _run_check(name: str, sch, cfg, args: argparse.Namespace) -> list:
    """Run one check by name, importing lazily; missing ERC degrades gracefully."""
    if name == "erc":
        try:
            from .checks import erc  # lazy; may not exist yet
        except ImportError:
            sys.stderr.write("note: ERC check unavailable in this build; skipped\n")
            return []
        return erc.run(sch, cfg)
    if name == "power":
        from .checks import power
        return power.run(sch, cfg)
    if name == "bom":
        from .checks import bom
        return bom.run(sch)
    if name == "layout":
        from .checks import layout
        return layout.run(args.path)
    if name == "nets":
        from .checks import nets
        out = nets.run(sch, cfg)
        # geom near-miss lint reads the raw s-expression primitives, so it is
        # KiCad-only; the suffix gate keeps default Altium runs quiet.
        if str(args.path).lower().endswith(".kicad_sch"):
            from .checks import geom
            out.extend(geom.run(args.path))
        return out
    if name == "libsync":
        from .checks import libsync
        return libsync.run(args.path, lib_dirs=getattr(args, "symbols", None))
    return []


def _cmd_check(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    cfg = _load_cfg(args, path)

    which: list[str] = []
    if args.erc:
        which.append("erc")
    if args.power:
        which.append("power")
    if args.bom:
        which.append("bom")
    if getattr(args, "layout", False):
        which.append("layout")
    if getattr(args, "nets", False):
        which.append("nets")
    if getattr(args, "libsync", False):
        which.append("libsync")
    intent_file = getattr(args, "intent", None)
    if not which and not intent_file:
        # --intent alone is a pure intent assertion, like any other selector.
        which = ["erc", "power", "bom", "nets"]
        if str(path).lower().endswith(".kicad_sch"):
            which.append("layout")

    findings: list = []
    for name in which:
        findings.extend(_run_check(name, sch, cfg, args))
    if intent_file:
        from .checks import intent as intent_mod
        spec = intent_mod.load(intent_file)   # BAD_CONFIG/PROTOCOL_MISMATCH via main
        findings.extend(intent_mod.run(sch, spec))

    meta = _schematic_meta(sch)
    fmt = getattr(args, "format", None) or ("json" if args.json else "text")
    _emit(_report.render(findings, fmt, meta, source=str(path)))
    return _findings_exit(findings, args)


def _cmd_diff(args: argparse.Namespace) -> int:
    a = _load_schematic(_require_path(args.path, "first schematic"))
    b = _load_schematic(_require_path(args.other, "second schematic"))
    from .checks import diff as diffmod
    rep = diffmod.run(a, b)
    findings = rep.findings()
    if args.json:
        _emit(_dumps(rep.export()))
    else:
        _emit(_report.render(findings, "text", {}))
    return _findings_exit(findings, args)


def _cmd_arrange(args: argparse.Namespace) -> int:
    """`arrange <sch>` — nudge FREE components until nothing overlaps.

    Dry-run by default (prints the planned moves); --apply executes them
    through the standard draw pipeline (.bak + connectivity re-verify), so
    `akcli undo` reverts an arrange like any other write.
    """
    target = _require_path(args.path)
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: arrange works on .kicad_sch")
    from . import arrange as arrmod
    result = arrmod.plan(target,
                         grid=getattr(args, "grid", None) or arrmod.GRID_MIL,
                         margin=getattr(args, "margin", None) or arrmod.MARGIN_MIL)
    moves, stuck = result["moves"], result["anchored_overlaps"]
    do_apply = bool(getattr(args, "apply", False))
    base = {
        "symbols": result["symbols"], "clean": result["clean"],
        "moves": [{"designator": m.ref, "from": list(m.frm),
                   "to": list(m.to)} for m in moves],
        "anchored_overlaps": stuck,
    }
    if not args.json:
        if result["clean"]:
            _emit(f"arrange: {result['symbols']} symbols, no overlaps — clean")
        for m in moves:
            _emit(f"  move {m.ref}: ({m.frm[0]:g},{m.frm[1]:g}) -> "
                  f"({m.to[0]:g},{m.to[1]:g})")
        if stuck:
            _emit("  cannot auto-fix (wired/labeled or no free slot): "
                  + ", ".join(stuck))
    if not moves or not do_apply:
        if args.json:
            _emit(_dumps({**base, "applied": False}))
        elif moves:
            _emit(f"dry-run: {len(moves)} move(s) planned — re-run with --apply")
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    from .writers import kicad as kwriter
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": target.name,
              "ops": [m.to_op() for m in moves]}
    cfg = _load_cfg(args, target)
    findings: list = []
    results = kwriter.apply(oplist, str(target), apply=True,
                            sources=_draw_symbol_sources(args, cfg),
                            verify_out=findings, backup_dir=target.parent)
    code = _draw_exit(results, findings)
    if args.json:
        _emit(_dumps({
            **base, "applied": code == EXIT["OK"],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
            ],
        }))
        return (EXIT["FINDINGS"] if stuck else EXIT["OK"]) \
            if code == EXIT["OK"] else code
    if code == EXIT["OK"]:
        _emit(f"arrange: applied {len(moves)} move(s) to {target.name}"
              + (f" — {len(stuck)} overlap(s) left for manual fixing"
                 if stuck else ""))
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    return code


def _cmd_verify(args: argparse.Namespace) -> int:
    """`verify <a> <b>` — net-equivalence proof between two schematics.

    PASS means: same component set, and every net's pin membership is
    identical (net *names* may differ — conversions rename unnamed nets).
    With --strict, changed component values/footprints also fail.
    """
    a = _load_schematic(_require_path(args.path, "first schematic"))
    b = _load_schematic(_require_path(args.other, "second schematic"))
    from .checks import diff as diffmod
    rep = diffmod.run(a, b)

    comp_ok = not rep.added_components and not rep.removed_components
    nets_ok = (not rep.added_nets and not rep.removed_nets
               and not rep.member_changed_nets)
    strict_ok = not (getattr(args, "strict", False) and rep.changed_components)
    equivalent = comp_ok and nets_ok and strict_ok

    if args.json:
        _emit(_dumps({
            "equivalent": equivalent,
            "strict": bool(getattr(args, "strict", False)),
            "components": {"a": len(a.components), "b": len(b.components)},
            "nets": {"a": len(a.nets), "b": len(b.nets)},
            "renamed_nets": [[n.name_a, n.name_b] for n in rep.renamed_nets],
            "summary": rep.summary(),
            "detail": rep.export(),
        }))
    else:
        lines = [f"CONVERSION PROOF: {'PASS' if equivalent else 'FAIL'}",
                 f"  components: {len(a.components)} vs {len(b.components)}"
                 f"  (+{len(rep.added_components)} −{len(rep.removed_components)}"
                 f" ~{len(rep.changed_components)})",
                 f"  nets:       {len(a.nets)} vs {len(b.nets)}"
                 f"  (+{len(rep.added_nets)} −{len(rep.removed_nets)}"
                 f" membership-changed {len(rep.member_changed_nets)})"]
        if rep.renamed_nets:
            names = ", ".join(f"{n.name_a}→{n.name_b}"
                              for n in rep.renamed_nets[:8])
            lines.append(f"  renamed (connectivity identical): "
                         f"{len(rep.renamed_nets)}  [{names}]")
        if not equivalent:
            for c in rep.added_components[:10]:
                lines.append(f"  + component only in B: {c.designator_b}")
            for c in rep.removed_components[:10]:
                lines.append(f"  − component only in A: {c.designator_a}")
            for n in rep.added_nets[:10]:
                lines.append(f"  + net only in B: {n.name_b}")
            for n in rep.removed_nets[:10]:
                lines.append(f"  − net only in A: {n.name_a}")
            for n in rep.member_changed_nets[:10]:
                lines.append(
                    f"  ~ net {n.name_a or n.name_b}: "
                    f"+{[f'{d}.{p}' for d, p in n.added_members]} "
                    f"−{[f'{d}.{p}' for d, p in n.removed_members]}")
        if rep.changed_components and not getattr(args, "strict", False):
            lines.append(f"  note: {len(rep.changed_components)} component(s) "
                         "differ in value/footprint (connectivity unaffected; "
                         "--strict makes this fail)")
        if rep.low_confidence:
            lines.append("  note: low-confidence net matching — inspect "
                         "`akcli diff` output")
        _emit("\n".join(lines))
    if equivalent or getattr(args, "exit_zero", False):
        return EXIT["OK"]
    return EXIT["FINDINGS"]


def _cmd_undo(args: argparse.Namespace) -> int:
    """`undo <target>` — swap the target with its draw backup (<name>.bak).

    `akcli draw --apply` leaves `<target>.bak` beside the file; undo swaps the
    two, so running undo twice is a redo. Dry-run by default (like draw).
    """
    target = _require_path(args.path, "target .kicad_sch")
    bak = target.parent / (target.name + ".bak")
    if not bak.exists():
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no backup at {bak} (created by `akcli draw --apply`)")
    from .readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(bak))
    from .checks import diff as diffmod
    rep = diffmod.run(cur, old)
    summary = (f"{len(cur.components)} parts/{len(cur.nets)} nets -> "
               f"{len(old.components)} parts/{len(old.nets)} nets "
               f"(+{len(rep.added_components)} −{len(rep.removed_components)} "
               f"components, {len(rep.member_changed_nets)} nets change membership)")
    if not getattr(args, "apply", False):
        if args.json:
            _emit(_dumps({"applied": False, "target": str(target),
                          "backup": str(bak), "summary": summary}))
        else:
            _emit(f"undo (dry-run): would restore {bak.name}\n  {summary}\n"
                  "re-run with --apply to swap (undo again = redo)")
        return EXIT["OK"]
    import shutil as _shutil
    tmp = target.parent / (target.name + ".undo-tmp")
    _shutil.copy2(bak, tmp)
    _shutil.copy2(target, bak)
    tmp.replace(target)
    if args.json:
        _emit(_dumps({"applied": True, "target": str(target),
                      "backup": str(bak), "summary": summary}))
    else:
        _emit(f"undo: restored {target.name} from backup — {summary}\n"
              f"previous content kept at {bak.name} (undo again = redo)")
    return EXIT["OK"]


def _cmd_relink(args: argparse.Namespace) -> int:
    """`relink-symbols <sch>` — re-embed stale lib_symbols cache entries.

    Dry-run by default; ``--apply`` splices the fresh blocks in behind the
    net-membership equivalence gate (``VERIFY_FAILED`` -> exit 6, file
    untouched) and leaves ``<name>.bak``. ``missing-lib`` entries exit 6 like
    a failed op — scope with ``--only`` to silence intentionally-unavailable
    nicknames.
    """
    target = _require_path(args.target, "target .kicad_sch")
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: relink-symbols works on a .kicad_sch")
    from . import relink
    actions = relink.plan(target, lib_dirs=getattr(args, "libs", None),
                          only=getattr(args, "only", None))
    do_apply = bool(getattr(args, "apply", False))
    res = None
    if do_apply:
        # VERIFY_FAILED (gate refusal) / SYMBOL_NOT_FOUND -> exit 6 via main
        res = relink.apply(str(target), actions)

    replaces = [a for a in actions if a["status"] == "replace"]
    missing = [a for a in actions if a["status"] == "missing-lib"]
    if args.json:
        # new_sexpr is a full symbol block; strip it for readability
        slim = [{k: v for k, v in a.items() if k != "new_sexpr"} for a in actions]
        _emit(_dumps({
            "actions": slim,
            "applied": bool(res and res["written"]),
            "replaced": (res or {}).get("replaced", []),
            "backup": (res or {}).get("backup"),
        }))
    else:
        lines = []
        for a in actions:
            detail = f" — {a['detail']}" if a.get("detail") else ""
            lines.append(f"  {a['status']:<11} {a['lib_id']}  "
                         f"[{a['source'] or 'no source'}]{detail}")
        if not lines:
            lines.append("  (no embedded lib_symbols entries matched)")
        if res is not None and res["written"]:
            bak = Path(res["backup"]).name if res.get("backup") else None
            lines.append(f"status: APPLIED — re-embedded {len(res['replaced'])} "
                         f"symbol(s) into {target.name}"
                         + (f" (backup {bak}; `akcli undo` reverts)" if bak else ""))
        elif do_apply:
            lines.append("status: nothing to replace — file untouched")
        elif replaces:
            lines.append(f"status: dry-run — {len(replaces)} replacement(s) "
                         "pending; re-run with --apply")
        else:
            lines.append("status: dry-run — nothing to replace")
        _emit("\n".join(lines))
    return EXIT["OPLIST"] if missing else EXIT["OK"]


def _load_expected(path_str: str) -> dict:
    """Load an external pin->signal table from CSV or JSON."""
    p = Path(path_str)
    data = p.read_text(encoding="utf-8")  # FileNotFound -> exit 4 via main
    if p.suffix.lower() == ".json":
        obj = json.loads(data)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
        if isinstance(obj, list):
            out: dict = {}
            for row in obj:
                if not isinstance(row, dict):
                    continue
                key = row.get("pin") or row.get("Pin") or row.get("number")
                val = row.get("signal") or row.get("net") or row.get("name")
                if key is not None and val is not None:
                    out[str(key)] = str(val)
            return out
        raise _ExitWith(EXIT["USAGE"], "ERROR: expected JSON must be an object or array")
    # CSV
    import csv as _csv
    rows = list(_csv.reader(data.splitlines()))
    out = {}
    start = 0
    if rows and rows[0] and rows[0][0].strip().lower() in ("pin", "number", "#"):
        start = 1
    for r in rows[start:]:
        if len(r) >= 2 and r[0].strip():
            out[r[0].strip()] = r[1].strip()
    return out


def _cmd_pinmap(args: argparse.Namespace) -> int:
    path = _require_path(args.path)
    sch = _load_schematic(path)
    cfg = _load_cfg(args, path)

    if getattr(args, "mcu", None):
        cfg = _config.Config(
            mcu_designator=args.mcu,
            rails=cfg.rails,
            paths=cfg.paths,
            erc_waivers=cfg.erc_waivers,
            source_path=cfg.source_path,
        )

    expected = _load_expected(args.expected) if getattr(args, "expected", None) else None

    from .checks import pinmap
    findings = pinmap.run(sch, cfg, expected)
    fmt = "json" if args.json else "text"
    _emit(_report.render(findings, fmt, _schematic_meta(sch)))
    return _findings_exit(findings, args)


def _cmd_export(args: argparse.Namespace) -> int:
    if args.json:
        sys.stderr.write(
            "ERROR: `export` emits a netlist format — use --format {protel,kicad,csv}; "
            "for structured netlist JSON use `akcli net --json`\n"
        )
        return EXIT["USAGE"]
    path = _require_path(args.path)
    sch = _load_schematic(path)
    from . import exporters
    text = exporters.export_netlist(sch, args.format)
    if getattr(args, "output", None):
        Path(args.output).write_text(text, encoding="utf-8")
        sys.stderr.write(f"wrote {args.output}\n")
    else:
        _emit(text)
    return EXIT["OK"]


# --------------------------------------------------------------------------- #
# jlc — JLCPCB/LCSC part search via jlcsearch (the ONLY networked subcommand)
# --------------------------------------------------------------------------- #
def _jlc_price(p) -> str:
    return f"${p:.4f}" if isinstance(p, (int, float)) else "-"


def _jlc_table(parts: list) -> str:
    """Render search results as a fixed-width table (one part per row)."""
    header = ("LCSC", "MPN", "PACKAGE", "STOCK", "PRICE", "B", "DESCRIPTION")
    rows = [header]
    for p in parts:
        desc = p.description or p.category or ""
        rows.append((
            p.lcsc or "-",
            (p.mpn or "-")[:28],
            p.package or "-",
            str(p.stock),
            _jlc_price(p.price),
            "B" if p.basic else ("P" if p.preferred else "-"),
            desc[:40],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    out = []
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return "\n".join(out)


def _jlc_detail(p) -> str:
    """Render a single part as a key/value block."""
    lines = [
        f"LCSC:        {p.lcsc or '-'}",
        f"MPN:         {p.mpn or '-'}",
        f"description: {p.description or '-'}",
        f"package:     {p.package or '-'}",
        f"category:    {p.category or '-'}",
        f"stock:       {p.stock}",
        f"unit price:  {_jlc_price(p.price)}",
        f"basic:       {'yes' if p.basic else 'no'}",
        f"preferred:   {'yes' if p.preferred else 'no'}",
        f"datasheet:   {p.datasheet or '-'}",
    ]
    sub = p.attributes.get("subcategory")
    if sub:
        lines.append(f"subcategory: {sub}")
    return "\n".join(lines)


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

    from . import model as _model
    from . import units as _units
    from .readers import kicad_lib
    from .writers import geometry
    from .writers.lib_cache import _coerce_sources

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
        _emit(_dumps({
            "lib_id": lib_id, "at": [at[0], at[1]], "rotation": rot,
            "mirror": mirror, "unit_count": part_count, "pins": rows,
        }))
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


def _cmd_view(args: argparse.Namespace) -> int:
    """`view calc` / `view live <sch>` / `view <sch>` — the unified dashboard.

    One server hosts both pages: /calc always, /live when a schematic is
    watched. `view <sch.kicad_sch>` is shorthand for `view live <sch>`.
    """
    from .webui import server

    what, path = args.what, args.path
    if what.lower().endswith(".kicad_sch") and not path:
        what, path = "live", what
    if what not in ("calc", "live"):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: view expects `calc`, `live <sch>`, or a .kicad_sch path")
    port = args.port if args.port is not None else server.DEFAULT_PORT
    if what == "calc":
        return server.serve(port=port, open_browser=not args.no_browser,
                            max_steps=args.max_steps)
    path = _require_path(path, "schematic to watch")
    if not str(path).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: view live watches a .kicad_sch")
    return server.serve(port=port, open_browser=not args.no_browser,
                        target=path, state_dir=args.state_dir,
                        max_steps=args.max_steps)


def _cmd_ops(args: argparse.Namespace) -> int:
    """`ops list` / `ops template <op>` — the op-list authoring kit."""
    from . import ops as opsmod

    action = getattr(args, "action", None)
    if action == "list":
        try:
            caps = opsmod.load_capabilities()["ops"]  # packaged mirror + repo fallback
        except Exception:
            caps = {}
        if args.json:
            _emit(_dumps({
                "protocol_version": opsmod.PROTOCOL_VERSION,
                "ops": [{"name": name,
                         "required": list(opsmod._OP_REQUIRED.get(name, [])),
                         "kicad": (caps.get(name) or {}).get("kicad"),
                         "altium_live": (caps.get(name) or {}).get("altium")}
                        for name in sorted(opsmod.OP_NAMES)],
                "macros": [{"name": name,
                            "required": list(opsmod.MACRO_REQUIRED.get(name, []))}
                           for name in sorted(opsmod.MACRO_OPS)],
            }))
            return EXIT["OK"]
        lines = []
        for name in sorted(opsmod.OP_NAMES):
            required = ", ".join(opsmod._OP_REQUIRED.get(name, []))
            support = ""
            entry = caps.get(name)
            if entry:
                support = "  [kicad:" + ("yes" if entry.get("kicad") else "no")                           + " altium-live:" + ("yes" if entry.get("altium") else "no") + "]"
            lines.append(f"{name:26} required: {required or '-'}{support}")
        lines.append("-- macros (expanded to core ops before plan/draw; "
                     "label-on-pin connectivity) --")
        for name in sorted(opsmod.MACRO_OPS):
            required = ", ".join(opsmod.MACRO_REQUIRED.get(name, []))
            lines.append(f"{name:26} required: {required or '-'}")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if action == "template":
        name = getattr(args, "opname", None)
        if not name:
            raise _ExitWith(EXIT["USAGE"], "ERROR: ops template needs an op name")
        try:
            op = opsmod.op_template(name, include_optional=not getattr(args, "required_only", False))
        except KeyError:
            raise _ExitWith(
                EXIT["USAGE"],
                f"ERROR: unknown op {name!r}"
                f"{_did_you_mean(name, opsmod.OP_NAMES | opsmod.MACRO_OPS)} "
                "(see `akcli ops list`)",
            )
        doc = {
            "protocol_version": opsmod.PROTOCOL_VERSION,
            "target_format": "kicad",
            "target_file": "<board.kicad_sch>",
            "ops": [op],
        }
        _emit(_dumps(doc))
        return EXIT["OK"]
    raise _ExitWith(EXIT["USAGE"], "ERROR: use `akcli ops list` or `akcli ops template <op>`")


def _calc_md(doc: dict) -> str:
    """Render one compute() envelope as a markdown table."""
    from .calc.si import fmt_eng

    lines = [f"### {doc['title']}", "",
             "| result | value | note |", "|---|---|---|"]
    for k, cell in doc["results"].items():
        v, unit = cell["value"], cell.get("unit", "")
        if isinstance(v, float):
            plain = unit not in ("Ω", "V", "A", "W", "F", "H", "Hz", "s", "m")
            shown = f"{v:.6g} {unit}".strip() if plain else fmt_eng(v, unit)
        elif isinstance(v, list):
            shown = "; ".join(str(x) for x in v) if v and not isinstance(v[0], dict) \
                else f"{len(v)} entries (use --json)"
        else:
            shown = f"{v} {unit}".strip()
        lines.append(f"| {k} | {shown} | {cell.get('note', '')} |")
    lines += ["", f"*Reference: {doc['reference']}*"]
    return "\n".join(lines)


def _calc_batch(args: argparse.Namespace, params: list[str]) -> int:
    """`calc batch <file|->`: run a JSON job list, emit an array of envelopes."""
    import json as _json
    import sys as _sys

    from . import calc as calcmod
    from .calc.registry import CALCS, CalcError

    if not params:
        raise _ExitWith(EXIT["USAGE"], "ERROR: calc batch needs a jobs file ('-' = stdin)")
    src = _sys.stdin.read() if params[0] == "-" else None
    if src is None:
        path = Path(params[0])
        if not path.exists():
            raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: {path} not found")
        src = path.read_text(encoding="utf-8")
    try:
        doc = _json.loads(src)
        jobs = doc["jobs"] if isinstance(doc, dict) else doc
        assert isinstance(jobs, list)
    except Exception:
        raise _ExitWith(EXIT["USAGE"],
                        'ERROR: batch input must be {"jobs": [{"calc": ..., "params": {...}}, ...]}')
    out, failed = [], 0
    for i, job in enumerate(jobs):
        name = job.get("calc") if isinstance(job, dict) else None
        raw = {k: str(v) for k, v in (job.get("params") or {}).items()} \
            if isinstance(job, dict) else {}
        if not name or name not in CALCS:
            out.append({"calc": name, "error": f"job {i}: unknown calculator {name!r}"})
            failed += 1
            continue
        try:
            out.append(calcmod.compute(name, raw))
        except CalcError as exc:
            out.append({"calc": name, "error": str(exc)})
            failed += 1
    _emit(_dumps(out))
    if failed:
        print(f"{failed}/{len(jobs)} jobs failed", file=_sys.stderr)
    return EXIT["FINDINGS"] if failed else EXIT["OK"]


def _cmd_calc(args: argparse.Namespace) -> int:
    """`calc list` / `calc info <name>` / `calc <name> key=value ...`."""
    from . import calc as calcmod
    from .calc.registry import CALCS, CalcError
    from .calc.si import fmt_eng

    name = getattr(args, "name", None)
    params = list(getattr(args, "params", []) or [])
    if not name or name == "list":
        if getattr(args, "json", False):
            table = {
                c.name: {
                    "title": c.title, "group": c.group,
                    "params": [{"name": p.name, "unit": p.unit, "help": p.help,
                                "required": p.default is None,
                                **({"choices": list(p.choices)} if p.choices else {})}
                               for p in c.params],
                    "reference": c.reference,
                    **({"notes": c.notes} if c.notes else {}),
                }
                for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name))
            }
            _emit(_dumps(table))
            return EXIT["OK"]
        lines, group = [], None
        for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name)):
            if c.group != group:
                group = c.group
                lines.append(f"[{group}]")
            req = " ".join(p.name for p in c.params if p.default is None)
            lines.append(f"  {c.name:18} {c.title}" + (f"  ({req})" if req else ""))
        lines.append("`akcli calc info <name>` shows params + the reference; "
                     "`akcli calc <name> key=value ...` runs it.")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if name == "info":
        target = params[0] if params else None
        c = CALCS.get(target or "")
        if c is None:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: unknown calculator {target!r}"
                            f"{_did_you_mean(target or '', CALCS)} "
                            "(see `akcli calc list`)")
        lines = [f"{c.name} — {c.title}", ""]
        for p in c.params:
            d = "required" if p.default is None else f"default {p.default}"
            ch = f" one of {list(p.choices)}" if p.choices else ""
            lines.append(f"  {p.name:14} [{p.unit or '-'}] {p.help} ({d}){ch}")
        lines += ["", f"Reference: {c.reference}"]
        if c.notes:
            lines.append(f"Note: {c.notes}")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if name == "batch":
        return _calc_batch(args, params)
    if name not in CALCS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown calculator {name!r}"
                        f"{_did_you_mean(name, CALCS)} "
                        "(see `akcli calc list`)")
    raw: dict[str, str] = {}
    for tok in params:
        if "=" not in tok:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: expected key=value, got {tok!r}")
        k, v = tok.split("=", 1)
        raw[k.strip()] = v.strip()
    try:
        doc = calcmod.compute(name, raw)
    except CalcError as exc:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
    if getattr(args, "ops", None):
        from .calc import opsmap
        try:
            opdoc = opsmap.to_oplist(name, doc)
        except CalcError as exc:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
        rendered = _dumps(opdoc)
        if args.ops == "-":
            _emit(rendered)
            return EXIT["OK"]
        Path(args.ops).write_text(rendered + "\n", encoding="utf-8")
        import sys as _sys
        print(f"op-list written to {args.ops} "
              f"({len(opdoc['ops'])} ops — edit coordinates, then `akcli plan`)",
              file=_sys.stderr)
    if getattr(args, "md", False):
        _emit(_calc_md(doc))
        return EXIT["OK"]
    if getattr(args, "json", False):
        _emit(_dumps(doc))
        return EXIT["OK"]
    lines = [f"{doc['title']}"]
    for key, cell in doc["results"].items():
        val, unit = cell["value"], cell.get("unit", "")
        if isinstance(val, float):
            # SI prefixes scale linearly — only prefix bare base units
            # (never mm, °C/W, m², Ω/km, ...)
            prefixable = unit in ("Ω", "V", "A", "W", "F", "H", "Hz", "s", "m")
            shown = (fmt_eng(val, unit) if prefixable
                     else f"{val:.6g} {unit}".strip())
        elif isinstance(val, list):
            shown = f"{len(val)} entries (use --json for detail)"
        else:
            shown = f"{val} {unit}".strip()
        note = f"   ({cell['note']})" if cell.get("note") else ""
        lines.append(f"  {key:22} {shown}{note}")
    lines.append(f"reference: {doc['reference']}")
    _emit("\n".join(lines))
    return EXIT["OK"]


def _cmd_expected(args: argparse.Namespace) -> int:
    """Extract an expected pin->signal table from a .dts/.overlay or pinout .md.

    Bridges the adapters into the ``pinmap --expected`` pipeline: the emitted
    JSON object ({pin: signal}) is exactly what ``--expected`` consumes. The
    schematic stays authoritative; this table is advisory input.
    """
    src = getattr(args, "input", None)
    if not src:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing input file (.dts/.overlay or .md)")
    path = Path(src)
    if not path.is_file():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: file not found: {src}")

    suffix = path.suffix.lower()
    if suffix in (".dts", ".dtsi", ".overlay"):
        from .adapters import dts as dts_adapter  # lazy
        table = dts_adapter.to_expected_table(dts_adapter.parse_dts(path))
    elif suffix in (".md", ".markdown"):
        from .adapters import pinout_md  # lazy
        table = pinout_md.parse_pinout_md(
            path,
            key_header=getattr(args, "key_header", None),
            value_header=getattr(args, "value_header", None),
        )
    else:
        raise _ExitWith(
            EXIT["USAGE"],
            f"ERROR: unsupported input {suffix!r} (want .dts/.dtsi/.overlay or .md)",
        )

    payload = _dumps({k: table[k] for k in sorted(table)})
    out = getattr(args, "output", None)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        sys.stderr.write(f"wrote {len(table)} pin assignment(s) to {out}\n")
    else:
        _emit(payload)

    if not table:
        # An empty table would make `pinmap --expected` vacuously pass — treat
        # "nothing extracted" as a finding, not a success.
        sys.stderr.write(f"WARNING: no pin assignments found in {src}\n")
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _cmd_jlc(args: argparse.Namespace) -> int:
    """No subcommand given: print usage."""
    raise _ExitWith(
        EXIT["USAGE"],
        "ERROR: use `akcli jlc search <query>`, `akcli jlc show <C-number>`, "
        "`akcli jlc bom <sch>`, or `akcli jlc add <C-number>`",
    )


def _cmd_jlc_search(args: argparse.Namespace) -> int:
    query = getattr(args, "query", None)
    if not query:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing search query")
    from .parts import search as parts_search  # lazy: keeps network out of offline paths
    try:
        results = parts_search.search(query, limit=getattr(args, "limit", None) or 20,
                                      cache_dir=parts_search.default_cache_dir())
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if not results:
        sys.stderr.write(f"no parts found for {query!r}\n")
        if args.json:
            _emit(_dumps([]))
        return EXIT["OK"]
    if args.json:
        _emit(_dumps([p.to_dict() for p in results]))
    else:
        _emit(_jlc_table(results))
    return EXIT["OK"]


def _easyeda_enrich(lcsc: str):
    """Best-effort EasyEDA metadata/3D-availability lookup; never raises.

    Returns an ``EasyEdaInfo`` or ``None`` — a failed/absent EasyEDA lookup must never
    break ``jlc show``; it just omits the EasyEDA-derived fields.
    """
    try:
        from .parts import easyeda  # lazy: keeps network out of offline paths
        return easyeda.lookup(lcsc)
    except Exception:  # EasyEdaError or anything unexpected -> degrade gracefully
        return None


def _easyeda_lines(info) -> list[str]:
    return [
        "-- EasyEDA --",
        f"3D model:     {'available' if info.has_3d else 'none'}",
        f"model uuid:   {info.model_uuid or '-'}",
        f"manufacturer: {info.manufacturer or '-'}",
        f"EasyEDA MPN:  {info.mpn or '-'}",
        f"EasyEDA pkg:  {info.package or '-'}",
        f"source:       {info.source}",
    ]


def _cmd_jlc_bom(args: argparse.Namespace) -> int:
    """`jlc bom <sch>` — BOM lines vs the JLCPCB catalog (stock/price/cost)."""
    path = _require_path(args.path)
    sch = _load_schematic(path)
    from .parts import bom_jlc, search as parts_search  # lazy: networked
    qty = max(1, getattr(args, "qty", 1) or 1)
    fix_all = getattr(args, "fix_all", False)
    do_fix = getattr(args, "fix", False) or fix_all
    do_suggest = do_fix or getattr(args, "suggest", False)
    if do_fix and not str(path).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --fix writes the schematic; it needs a .kicad_sch")
    cache = parts_search.default_cache_dir()
    try:
        lines = bom_jlc.check(
            sch, min_stock=getattr(args, "min_stock", 1) or 1, qty=qty,
            cache_dir=cache)
        if do_suggest:
            bom_jlc.suggest_parts(lines, cache_dir=cache)
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if do_fix:
        # plain --fix writes high-confidence suggestions only; --fix-all also
        # writes low-confidence ones (package matched, value unverified)
        ops = bom_jlc.fix_ops(
            lines, min_confidence=("low" if fix_all else "high"))
        if not fix_all:
            low = sum(1 for ln in lines
                      if ln.suggestion
                      and (ln.suggestion_confidence or "low") == "low")
            if low:
                sys.stderr.write(f"{low} low-confidence suggestion(s) not "
                                 "written (use --fix-all)\n")
        if not ops:
            sys.stderr.write("--fix: nothing to fix (no suggestions)\n")
        else:
            from .writers import kicad as kwriter
            oplist = {"protocol_version": 1, "target_format": "kicad",
                      "target_file": path.name, "ops": ops}
            findings: list = []
            results = kwriter.apply(oplist, str(path), apply=True,
                                    sources=[], verify_out=findings,
                                    backup_dir=path.parent)
            code = _draw_exit(results, findings)
            if code != EXIT["OK"]:
                return code
            fixed = [ln for ln in lines if ln.suggestion]
            for ln in fixed:
                sys.stderr.write(
                    f"fixed {','.join(ln.refs)}: "
                    f"{ln.lcsc_key or 'LCSC'} = {ln.suggestion.lcsc} "
                    f"({ln.suggestion.mpn}) — verify against the datasheet\n")
            # re-check so the report reflects the written ids
            sch = _load_schematic(path)
            try:
                lines = bom_jlc.check(
                    sch, min_stock=getattr(args, "min_stock", 1) or 1,
                    qty=qty, cache_dir=cache)
            except parts_search.JlcNetworkError as exc:
                sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
                return EXIT["TOOL_MISSING"]
    csv_out = getattr(args, "csv", None)
    if csv_out:
        text = bom_jlc.to_jlc_csv(lines)
        if csv_out == "-":
            sys.stdout.write(text)          # CSV replaces the table: stdout = data
        else:
            Path(csv_out).write_text(text, encoding="utf-8")
            sys.stderr.write(f"wrote JLCPCB BOM CSV: {csv_out}\n")
    agg = bom_jlc.totals(lines)
    if csv_out == "-":
        pass
    elif args.json:
        _emit(_dumps({"qty": qty, "lines": [ln.to_dict() for ln in lines],
                      "totals": agg}))
    else:
        rows = [("REFS", "QTY", "NEED", "VALUE", "PART", "STATUS",
                 "STOCK", "UNIT", "EXT", "B", "NOTE")]
        for ln in sorted(lines, key=lambda x: x.refs[0]):
            p = ln.part
            rows.append((
                ",".join(ln.refs[:4]) + ("…" if len(ln.refs) > 4 else ""),
                str(ln.qty),
                str(ln.need),
                (ln.value or "-")[:16],
                ln.lcsc or ln.mpn or "-",
                ln.status,
                str(p.stock) if p else "-",
                f"${ln.unit_price:.4f}" if ln.unit_price is not None else "-",
                f"${ln.ext_price:.2f}" if ln.ext_price is not None else "-",
                ("B" if p.basic else "P" if p.preferred else "-") if p else "-",
                (f"→ {ln.suggestion.lcsc} {ln.suggestion.mpn} "
                 f"(stock {ln.suggestion.stock}"
                 f"{', Basic' if ln.suggestion.basic else ''}) — "
                 "--fix writes it" if ln.suggestion else ln.note),
            ))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
        _emit("\n".join(
            "  ".join(c.ljust(w) for c, w in zip(r[:-1], widths)) + "  " + r[-1]
            for r in rows).rstrip())
        summary = (f"{agg['lines']} line(s): {agg['ok']} ok, "
                   f"{agg['problems']} problem(s), "
                   f"{agg['no_part_id']} without a part id")
        if agg["priced_lines"]:
            summary += (f" · est. parts cost ${agg['est_cost']:.2f} "
                        f"for {qty} board(s)"
                        + (f" ({agg['priced_lines']}/{agg['lines']} lines priced)"
                           if agg["priced_lines"] < agg["lines"] else ""))
        sys.stdout.flush()          # keep the table above the stderr summary
        sys.stderr.write(summary + "\n")
    if agg["problems"] and not getattr(args, "exit_zero", False):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _cmd_jlc_show(args: argparse.Namespace) -> int:
    lcsc = getattr(args, "lcsc", None)
    if not lcsc:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing LCSC C-number")
    from .parts import search as parts_search  # lazy
    try:
        part = parts_search.get(lcsc, cache_dir=parts_search.default_cache_dir())
    except parts_search.JlcNetworkError as exc:
        sys.stderr.write(f"ERROR: NETWORK: {exc.message}\n")
        return EXIT["TOOL_MISSING"]
    if part is None:
        sys.stderr.write(f"no part {lcsc!r} found\n")
        if args.json:
            _emit(_dumps(None))
        return EXIT["OK"]

    info = _easyeda_enrich(part.lcsc or lcsc) if getattr(args, "easyeda", False) else None

    if args.json:
        payload = part.to_dict()
        if getattr(args, "easyeda", False):
            payload["easyeda"] = info.to_dict() if info is not None else None
        _emit(_dumps(payload))
    else:
        out = _jlc_detail(part)
        if info is not None:
            out += "\n" + "\n".join(_easyeda_lines(info))
        elif getattr(args, "easyeda", False):
            out += "\n-- EasyEDA --\n(metadata unavailable)"
        _emit(out)
    return EXIT["OK"]


_VERIFY_CAVEAT = (
    "NOTE: symbol/footprint/3D converted from EasyEDA/LCSC CAD data. Verify pin "
    "mapping, footprint dimensions, and 3D alignment against the datasheet before use."
)

_ADD_EXIT = {
    "NETWORK": EXIT["TOOL_MISSING"],
    "CONVERT_PART_NOT_FOUND": EXIT["NOT_FOUND"],
    "CONVERT_FAILED": EXIT["OPLIST"],
    "CONVERT_NO_ARTIFACTS": EXIT["OPLIST"],
}


def _read_symbol_name(kicad_sym_path: str) -> str | None:
    """Read the first symbol id from a produced ``.kicad_sym`` (don't guess from files)."""
    try:
        from .readers import kicad_lib  # lazy
        lib = kicad_lib.read(kicad_sym_path)
    except Exception:
        return None
    return lib.symbols[0].name if lib.symbols else None


def _build_place_oplist(result, args, value: str | None) -> dict | None:
    """Build a one-op ``place_component`` op-list from a successful conversion.

    ``lib_id``'s symbol name is read from the produced ``.kicad_sym`` (the converter
    names by component name, not the C-number); the footprint id comes from the
    produced ``.kicad_mod`` stem. Returns ``None`` if no symbol artifact was produced.
    """
    from .ops import PROTOCOL_VERSION

    lib_name = getattr(args, "lib_name", None) or "akcli"
    sym_art = next((a for a in result.artifacts if a.endswith(".kicad_sym")), None)
    if sym_art is None:
        return None
    sym_name = _read_symbol_name(sym_art)
    if not sym_name:
        return None
    fp_art = next((a for a in result.artifacts if a.endswith(".kicad_mod")), None)
    fp_name = Path(fp_art).stem if fp_art else sym_name

    x_mil, y_mil = args.at
    op: dict = {
        "op": "place_component",
        "lib_id": f"{lib_name}:{sym_name}",
        "designator": args.designator,
        "x_mil": float(x_mil),
        "y_mil": float(y_mil),
        "footprint": f"footprint:{fp_name}",
    }
    if value:
        op["value"] = value
    return {
        "protocol_version": PROTOCOL_VERSION,
        "target_format": "kicad",
        "ops": [op],
    }


def _cmd_jlc_add(args: argparse.Namespace) -> int:
    from .parts import search as parts_search  # lazy: reuse the C-number normalizer

    digits = parts_search._lcsc_digits(getattr(args, "lcsc", None))
    if not digits:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing/invalid LCSC C-number")
    lcsc = "C" + digits

    place = bool(getattr(args, "place", False))
    if place:
        if not getattr(args, "designator", None):
            raise _ExitWith(EXIT["USAGE"], "ERROR: --place requires --designator REF")
        if not getattr(args, "at", None):
            raise _ExitWith(EXIT["USAGE"], "ERROR: --place requires --at X Y")

    out_dir = getattr(args, "out", None) or str(Path("akcli-parts") / lcsc)
    with_3d = bool(getattr(args, "with_3d", False))

    # Advisory EasyEDA lookup: what is being fetched + whether 3D exists.
    info = _easyeda_enrich(lcsc)
    if with_3d and info is not None and not info.has_3d:
        sys.stderr.write(
            f"warning: no 3D model is published for {lcsc} on EasyEDA; "
            "no STEP will be produced\n"
        )

    from .drivers import jlc2kicad  # lazy: vendored converter (networked)

    result = jlc2kicad.convert(
        lcsc,
        out_dir,
        with_3d=with_3d,
        lib_name=getattr(args, "lib_name", None) or "akcli",
        force=bool(getattr(args, "force", False)),
    )

    if result.error_code is not None:
        sys.stderr.write(f"ERROR: {result.error_code}: {result.message}\n")
        return _ADD_EXIT.get(result.error_code, EXIT["OPLIST"])

    value = (info.mpn or info.title) if info is not None else None
    place_doc = None
    place_path = None
    if place:
        place_doc = _build_place_oplist(result, args, value)
        if place_doc is not None:
            place_path = Path(out_dir) / "place.json"
            try:
                place_path.write_text(_dumps(place_doc) + "\n", encoding="utf-8")
            except OSError:  # pragma: no cover - best-effort file write
                place_path = None
        else:
            sys.stderr.write(
                "warning: --place skipped: no KiCad symbol artifact to place\n"
            )

    if args.json:
        payload = result.to_dict()
        payload["note"] = _VERIFY_CAVEAT
        if place:
            payload["place"] = place_doc
        _emit(_dumps(payload))
    else:
        lines = [
            f"converted {lcsc} -> kicad (in-process, vendored JLC2KiCadLib)",
            f"out: {result.out_dir}",
            "artifacts:",
        ]
        for a in result.artifacts:
            lines.append(f"  {a}")
        if place and place_path is not None:
            lines.append(f"placement op-list: {place_path}")
            lines.append("  apply with: akcli draw <target.kicad_sch> --ops "
                         f"{place_path} --apply")
        lines.append(_VERIFY_CAVEAT)
        lines.append("hint: review with `akcli check`/`kicad-cli erc` before use.")
        _emit("\n".join(lines))
    return EXIT["OK"]


def _read_symbol_name(kicad_sym_path: str) -> str | None:
    """Read the first symbol id from a produced ``.kicad_sym`` (don't guess from files)."""
    try:
        from .readers import kicad_lib  # lazy
        lib = kicad_lib.read(kicad_sym_path)
    except Exception:
        return None
    return lib.symbols[0].name if lib.symbols else None


# --------------------------------------------------------------------------- #
# plan / draw (KiCad op-list executor)
# --------------------------------------------------------------------------- #
def _draw_symbol_sources(args: argparse.Namespace, cfg) -> list:
    """Collect symbol sources for the writer: --symbols paths + config paths."""
    sources: list = []
    for s in getattr(args, "symbols", None) or []:
        sources.append(s)
    # config [paths] entries pointing at .kicad_sym files are usable symbol sources
    for key, val in (getattr(cfg, "paths", None) or {}).items():
        if isinstance(val, str) and val.lower().endswith(".kicad_sym"):
            sources.append(val)
    return sources


def _draw_results_text(results: list, findings: list) -> str:
    """Render per-op results + connectivity findings as a human summary."""
    lines = [f"# ops ({len(results)})"]
    for r in results:
        uuids = f" -> {r.created_uuids}" if r.created_uuids else ""
        if r.status == "ok":
            lines.append(f"  ok    [{r.op_index}] {r.op}{uuids}")
        else:
            lines.append(f"  ERROR [{r.op_index}] {r.op}: {r.error_code}: {r.message}")
    lines.append(f"# connectivity ({len(findings)})")
    if not findings:
        lines.append("  (clean)")
    for f in findings:
        lines.append(f"  {f.severity.value.upper()} [{f.code}] {f.message}")
    return "\n".join(lines)


def _draw_exit(results: list, findings: list) -> int:
    """Exit 6 (OPLIST) when any op errored or connectivity has an error finding."""
    if any(r.status == "error" for r in results):
        return EXIT["OPLIST"]
    actionable = {_report.Severity.ERROR, _report.Severity.CRITICAL}
    if any(f.severity in actionable for f in findings):
        return EXIT["OPLIST"]
    return EXIT["OK"]


def _run_draw(args: argparse.Namespace, do_apply: bool) -> int:
    """Shared plan/draw driver: validate + (dry-)apply an op-list to a .kicad_sch."""
    target = _require_path(args.target, "target .kicad_sch")
    if not getattr(args, "ops", None):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing --ops <oplist.json>")

    from .ops import expand_macros, load_oplist, validate_oplist
    from .writers import kicad as kwriter

    oplist = load_oplist(args.ops)               # FileNotFound -> exit 4 via main
    oplist = expand_macros(oplist)               # macro ops -> core ops (exit 6 on bad args)
    errs = validate_oplist(oplist)
    if errs:
        for e in errs:
            sys.stderr.write(f"ERROR: [{e.op_index}] {e.code}: {e.message}\n")
        return EXIT["OPLIST"]

    cfg = _load_cfg(args, target)
    sources = _draw_symbol_sources(args, cfg)

    strict = do_apply and getattr(args, "strict_nets", False)
    # Connectivity diff (advisory unless --strict-nets): dry-apply the op-list
    # to a temp copy and compare netlists, so both dry-run and apply report the
    # net effect BEFORE the target is touched. When the diff cannot be computed
    # at all, --strict-nets fails CLOSED (a silently skipped gate is no gate).
    net_lines = None
    net_risk = False
    net_equiv = True
    net_diff_err = None
    if not getattr(args, "no_net_diff", False) or strict:
        from .netdiff import diff as net_diff
        from .netdiff import format_summary as net_summary
        from .netdiff import has_risk as net_has_risk
        from .readers import kicad as kreader
        import os
        import shutil
        # the copy lives NEXT TO the target (never a TemporaryDirectory): a
        # hierarchical root must still resolve its child sheets on read-back
        tmp = target.parent / f".{target.name}.netdiff.{os.getpid()}.tmp"
        try:
            before_nets = kreader.read_sch(str(target)).nets
            shutil.copy2(target, tmp)
            tmp_findings: list = []
            tmp_results = kwriter.apply(oplist, str(tmp), apply=True,
                                        sources=sources,
                                        verify_out=tmp_findings,
                                        backup_dir=None)
            if _draw_exit(tmp_results, tmp_findings) == EXIT["OK"]:
                after_nets = kreader.read_sch(str(tmp)).nets
                nd = net_diff(before_nets, after_nets)
                net_lines = net_summary(nd)      # [] iff nd.equivalent
                net_risk = net_has_risk(nd)
                net_equiv = nd.equivalent
            # else: the op-list itself fails — the per-op results below explain
            # it, and the real apply refuses on its own (nothing is written),
            # so a "(none)" net diff here would be misleading
        except Exception as e:                   # noqa: BLE001
            net_diff_err = f"{type(e).__name__}: {e}"
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    if net_diff_err is not None:
        if strict:
            raise _ExitWith(EXIT["OPLIST"],
                            "REFUSED: --strict-nets: net diff unavailable "
                            f"({net_diff_err}); nothing written")
        sys.stderr.write(f"WARNING: net diff unavailable: {net_diff_err}\n")

    if strict and net_risk:
        for ln in net_lines or []:
            sys.stderr.write(f"  {ln}\n")
        raise _ExitWith(EXIT["OPLIST"],
                        "REFUSED: --strict-nets: net split/merge touches a "
                        "named net; nothing written")

    findings: list = []
    results = kwriter.apply(
        oplist, str(target), apply=do_apply, sources=sources, verify_out=findings,
        # write a <name>.bak next to the target on apply (the atomic write already
        # guarantees the original is never corrupted; this is an extra safety copy).
        backup_dir=(target.parent if do_apply else None),
    )

    if do_apply and _draw_exit(results, findings) == EXIT["OK"]:
        _log(args, 1, f"wrote {target}")
        # advisory secondary ERC via kicad-cli, if installed (never fatal)
        try:
            from .drivers import kicad_cli
            if kicad_cli.available():
                rep = kicad_cli.erc(str(target))
                if rep is not None:
                    _log(args, 1, f"kicad-cli erc: exit {rep.get('exit_code')}")
        except Exception:  # pragma: no cover - advisory only
            pass

    code = _draw_exit(results, findings)
    show_diff = not getattr(args, "no_net_diff", False)
    if not do_apply:
        hint = " (re-run with --apply)" if getattr(args, "command", "") == "draw" else ""
        status = "dry-run"
        status_line = f"status: dry-run — nothing written{hint}"
    elif code == EXIT["OK"]:
        status = "applied"
        status_line = (f"status: APPLIED — wrote {target.name} "
                       f"(backup {target.name}.bak; `akcli undo` reverts)")
    else:
        status = "refused"
        status_line = "status: REFUSED — nothing written (fix the errors above)"

    if args.json:
        payload = {
            "applied": bool(do_apply and code == EXIT["OK"]),
            "status": status,
            "ops": [r.to_dict() for r in results],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
            ],
            "net_diff": None if (net_lines is None or not show_diff) else {
                "equivalent": net_equiv, "risk": net_risk, "lines": net_lines,
            },
        }
        _emit(_dumps(payload))
    else:
        _emit(_draw_results_text(results, findings))
        if show_diff and net_lines is not None:
            _emit("Net changes:")
            if not net_lines:
                _emit("  (none)")
            for ln in net_lines:
                _emit(f"  {ln}")
        _emit(status_line)

    return code


def _cmd_plan(args: argparse.Namespace) -> int:
    """Validate + dry-run an op-list (per-op preview + connectivity); never writes."""
    return _run_draw(args, do_apply=False)


def _cmd_draw(args: argparse.Namespace) -> int:
    """Apply an op-list to a .kicad_sch (dry-run unless --apply)."""
    return _run_draw(args, do_apply=bool(getattr(args, "apply", False)))


# --------------------------------------------------------------------------- #
# parser construction
# --------------------------------------------------------------------------- #
# Global flags use ``SUPPRESS`` defaults so they can appear EITHER before or after the
# subcommand: the shared parent is attached to both the top-level parser and every
# subparser, and SUPPRESS stops the subparser's copy from clobbering a value parsed
# before the subcommand. ``main()`` backfills the real defaults after parsing.
_GLOBAL_DEFAULTS = {
    "config": None, "verbose": 0, "quiet": False,
    "json": False, "no_color": False, "debug": False,
}


def _global_flags() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--config", metavar="PATH", default=argparse.SUPPRESS,
                        help="path to altium-kicad-cli.toml (overrides discovery)")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="increase verbosity (-v, -vv)")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="suppress non-error logs")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="disable ANSI color")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="re-raise exceptions with a full traceback")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_flags()
    parser = argparse.ArgumentParser(
        prog="akcli",
        description="Read Altium .SchDoc/.SchLib/.PcbDoc and KiCad .kicad_sch, "
                    "run ERC/design/intent checks, query nets and parts, and "
                    "draw KiCad schematics from JSON op-lists (with net-diff "
                    "safety rails and one-command undo).",
        epilog=(
            "typical workflow:\n"
            "  akcli read board.kicad_sch            # inspect components + nets\n"
            "  akcli nets board.kicad_sch --intent-snapshot intent.json\n"
            "  akcli ops list && akcli ops template add_wire   # author an op-list\n"
            "  akcli plan board.kicad_sch --ops edit.json      # dry-run + net diff\n"
            "  akcli draw board.kicad_sch --ops edit.json --apply\n"
            "  akcli check board.kicad_sch --intent intent.json  # assert intent held\n"
            "  akcli undo board.kicad_sch --apply    # revert the last write\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],   # accept global flags before the subcommand too
    )
    parser.add_argument("--version", action="store_true",
                        help="print package + protocol version and exit")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("read", parents=[common], help="read + normalize a file")
    p.add_argument("path", nargs="?", help="input file (.SchDoc/.SchLib/.PcbDoc)")
    p.add_argument("--md", action="store_true", help="render a Markdown summary")
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
    p.set_defaults(handler=_cmd_nets)

    p = sub.add_parser("component", parents=[common], help="query one component's pin->net")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("ref", nargs="?", help="component designator (e.g. U3)")
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

    p = sub.add_parser("view", parents=[common],
                       help="local web dashboard: /calc (calculators) + /live (watch a .kicad_sch)")
    p.add_argument("what",
                   help="`calc`, `live`, or directly a .kicad_sch to watch")
    p.add_argument("path", nargs="?",
                   help="the .kicad_sch to watch (view live only)")
    p.add_argument("--port", type=int,
                   help="listen port (default 8765; auto-increments if busy; "
                        "localhost only)")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open the browser automatically")
    p.add_argument("--state-dir", metavar="DIR",
                   help="view live: persist the step timeline here "
                            "(default: fresh temp dir per run)")
    p.add_argument("--max-steps", type=int, default=500, metavar="N",
                   help="keep at most N timeline steps, deleting the oldest "
                        "SVGs (default 500; 0 = unlimited)")
    p.set_defaults(handler=_cmd_view)

    p = sub.add_parser("check", parents=[common], help="run ERC/power/BOM/layout checks")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--erc", action="store_true", help="run ERC checks")
    p.add_argument("--power", action="store_true", help="run power-rail checks")
    p.add_argument("--bom", action="store_true", help="run BOM-hygiene checks")
    p.add_argument("--layout", action="store_true",
                   help="run geometric-overlap lint (.kicad_sch only)")
    p.add_argument("--nets", action="store_true",
                   help="run connectivity-hygiene checks (single-pin nets, "
                        "off-grid pins, wire/pin/label attachment near-misses)")
    p.add_argument("--intent", metavar="FILE",
                   help="assert a JSON design-intent file (see `akcli nets "
                        "--intent-snapshot`) against the built netlist")
    p.add_argument("--libsync", action="store_true",
                   help="check embedded lib_symbols freshness (pin-signature "
                        "drift vs --symbols sources; old-format heuristic "
                        "without them)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="symbol source dir or .kicad_sym for --libsync "
                        "(repeatable)")
    p.add_argument("--exit-zero", action="store_true",
                   help="always exit 0 (report mode)")
    p.add_argument("--format", choices=["text", "json", "sarif", "junit"],
                   help="output format (sarif: GitHub code scanning; junit: CI test reporters)")
    p.set_defaults(handler=_cmd_check)

    p = sub.add_parser("diff", parents=[common], help="net-level diff of two schematics")
    p.add_argument("path", nargs="?", help="schematic A")
    p.add_argument("other", nargs="?", help="schematic B")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_diff)

    p = sub.add_parser("arrange", parents=[common],
                       help="nudge free (unwired/unlabeled) components until "
                            "no symbols overlap (dry-run unless --apply)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to arrange")
    p.add_argument("--apply", action="store_true",
                   help="write the moves (default: preview only)")
    p.add_argument("--grid", type=float, metavar="MIL",
                   help="nudge step in mils (default 100)")
    p.add_argument("--margin", type=float, metavar="MIL",
                   help="required clearance between symbols (default 50)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra symbol source for the write pipeline")
    p.set_defaults(handler=_cmd_arrange)

    p = sub.add_parser("verify", parents=[common],
                       help="net-equivalence proof between two schematics "
                            "(e.g. Altium original vs converted KiCad)")
    p.add_argument("path", nargs="?", help="schematic A (the reference)")
    p.add_argument("other", nargs="?", help="schematic B (the candidate)")
    p.add_argument("--strict", action="store_true",
                   help="also fail on component value/footprint differences")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_verify)

    p = sub.add_parser("undo", parents=[common],
                       help="swap a .kicad_sch with its draw backup "
                            "(<name>.bak; undo twice = redo)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to restore")
    p.add_argument("--apply", action="store_true",
                   help="actually swap (default is a dry-run preview)")
    p.set_defaults(handler=_cmd_undo)

    p = sub.add_parser("pinmap", parents=[common], help="MCU pin->net map + cross-check")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--mcu", metavar="REF", help="MCU designator (overrides config)")
    p.add_argument("--expected", metavar="FILE",
                   help="expected pin->signal table (.csv or .json)")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_pinmap)

    p = sub.add_parser("expected", parents=[common],
                       help="extract an expected pin->signal table from .dts/.overlay or pinout .md")
    p.add_argument("input", nargs="?", help="input file (.dts, .dtsi, .overlay, .md)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write JSON here instead of stdout (feed to pinmap --expected)")
    p.add_argument("--key-header", metavar="NAME",
                   help="markdown: explicit pin/GPIO column header")
    p.add_argument("--value-header", metavar="NAME",
                   help="markdown: explicit signal/net column header")
    p.set_defaults(handler=_cmd_expected)

    p = sub.add_parser("ops", parents=[common],
                       help="op-list authoring kit (list ops, emit op templates)")
    ops_sub = p.add_subparsers(dest="action", metavar="<action>")
    pol = ops_sub.add_parser("list", parents=[common],
                             help="list the op vocabulary with required fields")
    pol.set_defaults(handler=_cmd_ops, action="list")
    pot = ops_sub.add_parser("template", parents=[common],
                             help="emit a fill-in JSON op-list skeleton for one op")
    pot.add_argument("opname", nargs="?", help="op name, e.g. place_component")
    pot.add_argument("--required-only", action="store_true",
                     help="omit optional fields from the skeleton")
    pot.set_defaults(handler=_cmd_ops, action="template")
    p.set_defaults(handler=_cmd_ops)

    p = sub.add_parser("calc", parents=[common],
                       help="engineering calculators (E-series, IPC-2221, via, "
                            "SMPS, 555, I2C, ... — every result cites its source)")
    p.add_argument("name", nargs="?",
                   help="calculator name, or `list` / `info <name>` / `batch <file>`")
    p.add_argument("params", nargs="*", metavar="key=value",
                   help="inputs, engineering notation ok (4k7, 100n, 35u)")
    p.add_argument("--md", action="store_true",
                   help="render the result as a markdown table")
    p.add_argument("--ops", metavar="FILE",
                   help="also emit a place_component op-list with the computed "
                        "values ('-' = stdout; design-type calculators only)")
    p.set_defaults(handler=_cmd_calc)

    p = sub.add_parser("export", parents=[common], help="emit a netlist")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--format", choices=["protel", "kicad", "csv"], default="protel",
                   help="netlist format (default: protel)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write to FILE instead of stdout")
    p.set_defaults(handler=_cmd_export)

    p = sub.add_parser("plan", parents=[common],
                       help="validate + dry-run an op-list against a .kicad_sch (never writes)")
    p.add_argument("target", nargs="?", help="target .kicad_sch file")
    p.add_argument("--ops", metavar="FILE", help="op-list JSON file")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.add_argument("--no-net-diff", action="store_true",
                   help="skip the before/after net connectivity diff")
    p.set_defaults(handler=_cmd_plan)

    p = sub.add_parser("draw", parents=[common],
                       help="apply an op-list to a .kicad_sch (dry-run unless --apply)")
    p.add_argument("target", nargs="?", help="target .kicad_sch file")
    p.add_argument("--ops", metavar="FILE", help="op-list JSON file")
    p.add_argument("--apply", action="store_true",
                   help="actually write (default is dry-run: verify only)")
    p.add_argument("--dry-run", action="store_true",
                   help="verify only, do not write (default)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.add_argument("--no-net-diff", action="store_true",
                   help="skip the before/after net connectivity diff")
    p.add_argument("--strict-nets", action="store_true",
                   help="with --apply: refuse to write when the net diff shows "
                        "a split/merge touching a named net")
    p.set_defaults(handler=_cmd_draw)

    p = sub.add_parser("relink-symbols", parents=[common],
                       help="re-embed stale lib_symbols cache entries from "
                            "fresh .kicad_sym libraries (dry-run unless --apply)")
    p.add_argument("target", nargs="?", help="the .kicad_sch to relink")
    p.add_argument("--libs", metavar="DIR", action="append",
                   help="symbol library dir or .kicad_sym file (repeatable); "
                        "default: the KiCad.app SharedSupport symbols dir "
                        "if it exists")
    p.add_argument("--only", metavar="NICKS",
                   help="comma-separated library nicknames (or full lib_ids) "
                        "to consider")
    p.add_argument("--apply", action="store_true",
                   help="write the replacements (default: preview only; "
                        "net-membership equivalence gated, leaves <name>.bak)")
    p.set_defaults(handler=_cmd_relink)

    # jlc — JLCPCB/LCSC part search (needs network; powered by jlcsearch)
    p = sub.add_parser("jlc", parents=[common],
                       help="search JLCPCB/LCSC parts via jlcsearch (needs network)")
    p.set_defaults(handler=_cmd_jlc)
    jlc_sub = p.add_subparsers(dest="jlc_command", metavar="<subcommand>")

    ps = jlc_sub.add_parser("search", parents=[common],
                            help="keyword search for parts (MPN, category, C-number)")
    ps.add_argument("query", nargs="?", help="search keywords")
    ps.add_argument("--limit", type=int, default=20, metavar="N",
                    help="max results (default: 20)")
    ps.set_defaults(handler=_cmd_jlc_search)

    pb = jlc_sub.add_parser(
        "bom", parents=[common],
        help="check a schematic's BOM against the JLCPCB catalog "
             "(stock/price via LCSC/MPN parameters; networked)")
    pb.add_argument("path", nargs="?", help="input schematic (.kicad_sch/.SchDoc)")
    pb.add_argument("--qty", type=int, default=1, metavar="N",
                    help="number of boards: stock and tier pricing are "
                         "evaluated at N x per-line quantity (default: 1)")
    pb.add_argument("--min-stock", type=int, default=1, metavar="N",
                    help="flag lines with stock below N (default: 1)")
    pb.add_argument("--suggest", action="store_true",
                    help="search the catalog for not-found / no-part-id "
                         "lines (match by value + package) and print the "
                         "best candidate")
    pb.add_argument("--fix", action="store_true",
                    help="write suggested C-numbers into the schematic's "
                         "LCSC parameters (implies --suggest; .kicad_sch "
                         "only; leaves a .bak — `akcli undo` reverts; "
                         "high-confidence suggestions only)")
    pb.add_argument("--fix-all", dest="fix_all", action="store_true",
                    help="like --fix but also writes LOW-confidence "
                         "suggestions (package matched, value not verified "
                         "in the candidate description/MPN)")
    pb.add_argument("--csv", metavar="OUT.csv",
                    help="also write a JLCPCB upload BOM CSV (Comment,"
                         "Designator,Footprint,LCSC Part #); '-' writes "
                         "to stdout")
    pb.add_argument("--exit-zero", action="store_true",
                    help="always exit 0 (report mode)")
    pb.set_defaults(handler=_cmd_jlc_bom)

    psh = jlc_sub.add_parser("show", parents=[common],
                             help="show one part by LCSC C-number (e.g. C7593)")
    psh.add_argument("lcsc", nargs="?", help="LCSC part number, e.g. C7593")
    psh.add_argument("--easyeda", action="store_true",
                     help="also query EasyEDA for metadata + 3D-model availability")
    psh.set_defaults(handler=_cmd_jlc_show)

    pa = jlc_sub.add_parser(
        "add", parents=[common],
        help="fetch an LCSC part and convert it into a KiCad library (networked)",
    )
    pa.add_argument("lcsc", nargs="?", help="LCSC part number, e.g. C2040")
    pa.add_argument("--3d", dest="with_3d", action="store_true",
                    help="also download the 3D STEP model")
    pa.add_argument("--out", metavar="DIR",
                    help="output directory (default: ./akcli-parts/<C-number>/)")
    pa.add_argument("--lib-name", metavar="NAME", default="akcli",
                    help="KiCad symbol library name (default: akcli)")
    pa.add_argument("--force", action="store_true",
                    help="overwrite existing artifacts")
    pa.add_argument("--place", action="store_true",
                    help="also emit a place_component op-list")
    pa.add_argument("--designator", metavar="REF",
                    help="reference designator for --place (e.g. U1)")
    pa.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"),
                    help="placement position in mils for --place")
    pa.set_defaults(handler=_cmd_jlc_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Backfill global-flag defaults (they use argparse.SUPPRESS so a value given before
    # the subcommand isn't clobbered by the subparser's copy).
    for _attr, _default in _GLOBAL_DEFAULTS.items():
        if not hasattr(args, _attr):
            setattr(args, _attr, _default)

    if getattr(args, "version", False):
        print(f"altium-kicad-cli {__version__} (protocol {PROTOCOL_VERSION})")
        return EXIT["OK"]

    handler = getattr(args, "handler", None)
    if not getattr(args, "command", None) or handler is None:
        parser.print_help(sys.stderr)
        return EXIT["USAGE"]

    try:
        return handler(args)
    except _ExitWith as exc:
        if exc.msg:
            sys.stderr.write(exc.msg + "\n")
        return exc.code
    except AkcliError as exc:
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(as_error(exc) + "\n")
        return to_exit(exc)
    except FileNotFoundError as exc:
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(f"ERROR: file not found: {exc.filename or exc}\n")
        return EXIT["NOT_FOUND"]
    except BrokenPipeError:
        return EXIT["OK"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
