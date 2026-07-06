"""argparse dispatch + exit codes for the ``akcli`` CLI (SPEC §3.1).

Subcommands ``read net component check diff pinmap export plan draw`` are live.
``plan``/``draw`` drive the KiCad op-list executor (``draw`` writes only on
``--apply``). Every handler does its heavy imports LAZILY
(inside the handler) so ``akcli --help`` / ``--version`` run from a clean checkout
with only the Foundation modules present.

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
    fmt = _detect_format(path)
    if fmt == "altium_sch":
        from .readers import altium_sch  # lazy
        return altium_sch.read(str(path))
    if fmt == "kicad_sch":
        from .readers import kicad  # lazy
        return kicad.read_sch(str(path))
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
    if not which:
        which = ["erc", "power", "bom"]

    findings: list = []
    for name in which:
        findings.extend(_run_check(name, sch, cfg, args))

    meta = _schematic_meta(sch)
    fmt = "json" if args.json else "text"
    _emit(_report.render(findings, fmt, meta))
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


def _cmd_jlc(args: argparse.Namespace) -> int:
    """No subcommand given: print usage."""
    raise _ExitWith(
        EXIT["USAGE"],
        "ERROR: use `akcli jlc search <query>` or `akcli jlc show <C-number>`",
    )


def _cmd_jlc_search(args: argparse.Namespace) -> int:
    query = getattr(args, "query", None)
    if not query:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing search query")
    from .parts import search as parts_search  # lazy: keeps network out of offline paths
    try:
        results = parts_search.search(query, limit=getattr(args, "limit", None) or 20)
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


def _cmd_jlc_show(args: argparse.Namespace) -> int:
    lcsc = getattr(args, "lcsc", None)
    if not lcsc:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing LCSC C-number")
    from .parts import search as parts_search  # lazy
    try:
        part = parts_search.get(lcsc)
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

    from .ops import load_oplist, validate_oplist
    from .writers import kicad as kwriter

    oplist = load_oplist(args.ops)               # FileNotFound -> exit 4 via main
    errs = validate_oplist(oplist)
    if errs:
        for e in errs:
            sys.stderr.write(f"ERROR: [{e.op_index}] {e.code}: {e.message}\n")
        return EXIT["OPLIST"]

    cfg = _load_cfg(args, target)
    sources = _draw_symbol_sources(args, cfg)

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

    if args.json:
        payload = {
            "applied": bool(do_apply and _draw_exit(results, findings) == EXIT["OK"]),
            "ops": [r.to_dict() for r in results],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
            ],
        }
        _emit(_dumps(payload))
    else:
        _emit(_draw_results_text(results, findings))

    return _draw_exit(results, findings)


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
                    "run ERC/design checks, and draw KiCad schematics.",
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

    p = sub.add_parser("component", parents=[common], help="query one component's pin->net")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("ref", nargs="?", help="component designator (e.g. U3)")
    p.set_defaults(handler=_cmd_component)

    p = sub.add_parser("check", parents=[common], help="run ERC/power/BOM checks")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--erc", action="store_true", help="run ERC checks")
    p.add_argument("--power", action="store_true", help="run power-rail checks")
    p.add_argument("--bom", action="store_true", help="run BOM-hygiene checks")
    p.add_argument("--exit-zero", action="store_true",
                   help="always exit 0 (report mode)")
    p.set_defaults(handler=_cmd_check)

    p = sub.add_parser("diff", parents=[common], help="net-level diff of two schematics")
    p.add_argument("path", nargs="?", help="schematic A")
    p.add_argument("other", nargs="?", help="schematic B")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_diff)

    p = sub.add_parser("pinmap", parents=[common], help="MCU pin->net map + cross-check")
    p.add_argument("path", nargs="?", help="input schematic")
    p.add_argument("--mcu", metavar="REF", help="MCU designator (overrides config)")
    p.add_argument("--expected", metavar="FILE",
                   help="expected pin->signal table (.csv or .json)")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0")
    p.set_defaults(handler=_cmd_pinmap)

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
    p.set_defaults(handler=_cmd_draw)

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

    psh = jlc_sub.add_parser("show", parents=[common],
                             help="show one part by LCSC C-number (e.g. C7593)")
    psh.add_argument("lcsc", nargs="?", help="LCSC part number, e.g. C7593")
    psh.add_argument("--easyeda", action="store_true",
                     help="also query EasyEDA for metadata + 3D-model availability")
    psh.set_defaults(handler=_cmd_jlc_show)

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
