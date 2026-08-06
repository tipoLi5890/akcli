"""Shared helpers for the ``akcli`` command modules (see ``..cli``).

Format detection, schematic/config loading, stdout/stderr emit + logging
conventions, lint-style exit mapping, and the small render helpers used by
more than one command family. Every heavy dependency is imported LAZILY inside
the function that needs it so ``akcli --help`` / ``--version`` run from a clean
checkout with only the Foundation modules present.

Conventions
-----------
* **stdout = data, stderr = logs.** Machine-readable output goes to stdout;
  diagnostics/verbose logs go to stderr.
* ``_ExitWith`` is the internal control-flow signal handlers raise to stop with
  a specific exit code + stderr message; ``..cli.main`` maps it to a return code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config as _config
from .. import report as _report
from ..errors import EXIT

# OLE2/CFBF magic (all Altium binary docs).
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Extension -> internal format token.
_EXT_FORMAT = {
    ".schdoc": "altium_sch",
    ".schlib": "altium_schlib",
    ".pcbdoc": "altium_pcb",
    ".pcblib": "altium_pcblib",
    ".kicad_sch": "kicad_sch",
    ".kicad_pcb": "kicad_pcb",
    ".kicad_sym": "kicad_sym",
    ".kicad_mod": "kicad_mod",
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


# Set by ``_emit`` (reset per ``cli.main`` invocation): lets the top-level
# ``--json`` error envelope know whether stdout already carries data, so a
# failure after partial output never produces two JSON documents.
_stdout_written = False


def _stdout_touched() -> bool:
    return _stdout_written


def _reset_stdout_tracking() -> None:
    global _stdout_written
    _stdout_written = False


def _emit(text: str) -> None:
    """Write a data payload to stdout with exactly one trailing newline."""
    global _stdout_written
    _stdout_written = True
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def _require_path(value: str | None, what: str = "input file") -> Path:
    if not value:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: missing {what}")
    return Path(value)


def _sniff_ole(path: Path) -> str:
    """Classify a bare OLE2/CFBF container by its storage/stream layout.

    An unknown OLE file is NEVER assumed to be a schematic: an unrecognized
    layout returns ``"unknown"`` so the caller fails loudly (exit 5) instead of
    silently producing an empty import (the historic ``.PcbLib``-as-SchDoc trap).
    """
    try:
        from ..readers import _cfbf  # lazy
        streams = _cfbf.read_streams_qualified(path)
    except Exception:
        # A corrupt container is still *an OLE file*; route it to the Altium
        # schematic reader so the structured ALTIUM_* parse error surfaces.
        return "altium_sch"
    names = set(streams)
    tops = {n.split("/", 1)[0] for n in names}
    if any(t.startswith("Board6") for t in tops):
        return "altium_pcb"
    if "Library" in tops and any(n.startswith("Library/") for n in names):
        # PcbLib containers carry a Library storage plus per-footprint storages.
        return "altium_pcblib"
    header = streams.get("FileHeader", b"")
    if header:
        if b"Schematic Library" in header[:256]:
            return "altium_schlib"
        if b"Schematic Capture" in header[:256]:
            return "altium_sch"
        if b"PCB" in header[:256] and b"Library" in header[:256]:
            return "altium_pcblib"
        if b"PCB" in header[:256]:
            return "altium_pcb"
        # A FileHeader we cannot classify: most likely a schematic-family doc.
        return "altium_sch"
    return "unknown"


def _detect_format_ex(path: Path) -> tuple[str, str]:
    """Detect the file format; returns ``(format, detection_method)``.

    ``detection_method`` is one of ``"extension"``, ``"content"``, ``"none"``.
    """
    ext = path.suffix.lower()
    if ext in _EXT_FORMAT:
        return _EXT_FORMAT[ext], "extension"
    try:
        head = path.open("rb").read(64)
    except OSError:
        return "unknown", "none"
    if head.startswith(_OLE_MAGIC):
        return _sniff_ole(path), "content"
    stripped = head.lstrip()
    if stripped.startswith(b"(kicad_symbol_lib"):
        return "kicad_sym", "content"
    if stripped.startswith(b"(kicad_sch"):
        return "kicad_sch", "content"
    if stripped.startswith(b"(kicad_pcb"):
        return "kicad_pcb", "content"
    if stripped.startswith(b"(footprint") or stripped.startswith(b"(module"):
        return "kicad_mod", "content"
    return "unknown", "none"


def _detect_format(path: Path) -> str:
    """Detect the file format by extension, falling back to a content sniff."""
    return _detect_format_ex(path)[0]


def _empty_import_warning(path: Path, fmt: str, counts: dict[str, int]) -> str | None:
    """`EMPTY_IMPORT` warning text when a non-empty source normalized to nothing.

    A parse that "succeeds" with zero objects on a non-trivial input file is the
    classic silent-failure signature; surface it instead of exiting 0 quietly.
    """
    if any(counts.values()):
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    rendered = ", ".join(f"{k}=0" for k in counts)
    return (f"EMPTY_IMPORT: {path.name} ({size} bytes, format {fmt}) "
            f"normalized to nothing ({rendered}) — wrong/unsupported format? "
            "(--strict makes this fatal)")


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
        from ..readers import altium_sch  # lazy
        return _warned(altium_sch.read(str(path)))
    if fmt == "altium_prj":
        from ..readers import altium_prj  # lazy
        return _warned(altium_prj.read(str(path)))
    if fmt == "kicad_sch":
        from ..readers import kicad  # lazy
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
    if fmt == "altium_pcblib":
        raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"],
                        "ERROR: .PcbLib is a footprint library, not a schematic (use `read`)")
    if fmt == "kicad_mod":
        raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"],
                        "ERROR: .kicad_mod is a footprint, not a schematic (use `read`)")
    raise _ExitWith(EXIT["UNSUPPORTED_FORMAT"], f"ERROR: unsupported/unknown format: {path}")


def _load_cfg(args: argparse.Namespace, near: Path | None):
    """Load config from ``-C/--config`` or walk-up discovery; default empty Config.

    An explicit ``-C`` goes through :func:`config.load_explicit_config` (a
    missing/unparseable file is ``BAD_CONFIG``, never a silent fallback);
    walk-up discovery finding nothing stays silent, which is the ordinary
    no-config case. ``cli.main`` applies the same check to every invocation,
    so this is the library-caller path.
    """
    if getattr(args, "config", None):
        return _config.load_explicit_config(Path(args.config))
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
    from ..model import PinType  # lazy
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


# --fail-on token -> the least-severe Severity that trips a non-zero exit.
_FAIL_ON_SEVERITY = {
    "info": _report.Severity.INFO,
    "note": _report.Severity.NOTE,
    "warning": _report.Severity.WARNING,
    "error": _report.Severity.ERROR,
}


def _findings_exit(findings: list, args: argparse.Namespace) -> int:
    """Lint-style exit: 1 when any finding meets ``--fail-on`` (default warning).

    ``--fail-on never`` (and the deprecated ``--exit-zero`` alias) always exit
    0. One policy for every findings-emitting command — an agent learns the
    flag once.
    """
    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    fail_on = getattr(args, "fail_on", None) or "warning"
    if fail_on == "never":
        return EXIT["OK"]
    threshold = _report._SEV_RANK[_FAIL_ON_SEVERITY[fail_on]]
    if any(_report._SEV_RANK.get(f.severity, 0) >= threshold for f in findings):
        return EXIT["FINDINGS"]
    return EXIT["OK"]


def _add_exit_policy_flags(parser: argparse.ArgumentParser) -> None:
    """The one lint-style exit-policy pair: ``--fail-on`` + deprecated alias."""
    parser.add_argument("--fail-on", dest="fail_on",
                        choices=["info", "note", "warning", "error", "never"],
                        help="minimum finding severity that exits non-zero "
                             "(default: warning — findings gate by default; "
                             "never always exits 0. `akcli review analyze` "
                             "is the opposite: advisory unless --fail-on)")
    parser.add_argument("--exit-zero", action="store_true",
                        help="always exit 0 (deprecated alias for "
                             "--fail-on never)")


# --------------------------------------------------------------------------- #
# gate aggregation — shared by `release preflight` and `gate` (both run a
# fixed list of sub-checks, each producing "pass" / "fail" / "skipped").
# --------------------------------------------------------------------------- #
# A finding at or above WARNING is what turns a gate "fail" (the default
# --fail-on threshold); NOTE/INFO stay visible but do not flip the status.
_GATE_ACTIONABLE = {_report.Severity.WARNING, _report.Severity.ERROR,
                    _report.Severity.CRITICAL}


def _gate(name: str, findings: list) -> dict:
    """One gate's result: ``pass``/``fail`` + its findings, JSON-shaped."""
    worst = max((_report._SEV_RANK.get(f.severity, 0) for f in findings),
                default=0)
    return {
        "gate": name,
        "status": "fail" if any(f.severity in _GATE_ACTIONABLE for f in findings)
                  else "pass",
        "findings": [
            {"code": f.code, "severity": f.severity.value, "message": f.message}
            for f in findings
        ],
        "worst_rank": worst,
    }


def _skipped(name: str, reason: str) -> dict:
    """A gate whose input was absent — explicit, never a silent pass."""
    return {"gate": name, "status": "skipped", "reason": reason, "findings": []}


def _gate_exit(gates: list[dict], args: argparse.Namespace) -> int:
    """Worst-of exit across a gate list, honoring ``--fail-on``/``--exit-zero``.

    Default (no flag): FINDINGS iff any gate's status is ``fail`` — the
    ordinary "all pass or skipped" rule, because the default --fail-on
    threshold (warning) is exactly what ``_gate`` used to compute "fail".
    A non-default ``--fail-on`` re-thresholds against each gate's
    ``worst_rank`` (present even on a "pass" gate that only carries
    sub-warning findings), without changing the reported pass/fail text —
    same split as ``_findings_exit``: findings are reported honestly,
    ``--fail-on`` only tunes the exit code.
    """
    if getattr(args, "exit_zero", False):
        return EXIT["OK"]
    fail_on = getattr(args, "fail_on", None) or "warning"
    if fail_on == "never":
        return EXIT["OK"]
    threshold = _report._SEV_RANK[_FAIL_ON_SEVERITY[fail_on]]
    worst = max((g.get("worst_rank", 0) for g in gates), default=0)
    return EXIT["FINDINGS"] if worst >= threshold else EXIT["OK"]


def _dumps(obj) -> str:
    """The single JSON exit for every ``--json`` payload in the CLI.

    ``allow_nan=False`` is load-bearing, not paranoia: Python's default emits
    bare ``NaN``/``Infinity``/``-Infinity`` literals, which RFC 8259 has no
    production for — ``JSON.parse`` and Go's ``encoding/json`` both reject
    them, so an agent consuming us would fail on the *parse*, not on a value
    it could inspect. Producers must clamp/guard upstream (see
    ``calc.si.parse_value`` and ``calc.registry.compute``); if one ever slips
    through, the ``ValueError`` raised here is caught by ``cli.main`` and
    reported as a normal error envelope rather than shipping invalid JSON.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False,
                      allow_nan=False)


def _stamp(payload: dict, version: str = "1") -> dict:
    """Prepend the agent-contract ``schema_version`` on a hand-built payload.

    Every JSON *object* payload carries ``schema_version`` (or a family
    version field like ``protocol_version``) so an agent can detect a
    breaking change; payloads with a canonical schema stamp their own pinned
    version instead of routing through here. Never overwrites an existing
    stamp.
    """
    if "schema_version" in payload:
        return payload
    return {"schema_version": version, **payload}


def _match_limit(items: list, args: argparse.Namespace, key) -> tuple[list, dict]:
    """Apply ``--match GLOB`` / ``--limit N`` to a listing; return (items, meta).

    ``meta`` is the truncation envelope (``total``/``matched``/``returned``/
    ``truncated``) merged into ``--json`` payloads so an agent can see that a
    listing was cut without diffing counts itself. ``--match`` is a
    case-sensitive ``fnmatch`` glob over ``key(item)``.
    """
    import fnmatch

    total = len(items)
    pattern = getattr(args, "match", None)
    if pattern:
        items = [i for i in items if fnmatch.fnmatchcase(key(i) or "", pattern)]
    matched = len(items)
    limit = getattr(args, "limit", None)
    truncated = limit is not None and limit >= 0 and len(items) > limit
    if truncated:
        items = items[:limit]
    return items, {"total": total, "matched": matched,
                   "returned": len(items), "truncated": truncated}


def _throttle_note(args: argparse.Namespace, meta: dict, what: str) -> None:
    """stderr note when a listing was filtered/truncated (text mode courtesy)."""
    if meta["returned"] != meta["total"]:
        sys.stderr.write(
            f"note: showing {meta['returned']} of {meta['total']} {what}"
            + (f" matching {getattr(args, 'match', None)!r}"
               if getattr(args, "match", None) else "")
            + (" (truncated by --limit)" if meta["truncated"] else "")
            + "\n")


def _add_throttle_flags(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--match", metavar="GLOB",
                        help=f"only list {what} whose name matches this "
                             "fnmatch glob (case-sensitive)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help=f"list at most N {what} (JSON carries "
                             "total/matched/returned/truncated)")


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
# draw helpers shared by draw / arrange / pins / jlc bom
# --------------------------------------------------------------------------- #
def _draw_symbol_sources(args: argparse.Namespace, cfg, *,
                         lib_ids=None, project_dir=None) -> list:
    """Collect symbol sources for the writer: --symbols paths + config paths.

    When ``lib_ids`` is given (the ``lib_id``s an op-list/lookup actually needs),
    the **library tables** are consulted as a LAST resort: a nickname that the
    explicit sources cannot serve is looked up in the project and per-user global
    ``sym-lib-table`` (:func:`..libtable.discover`), so ``place_component
    Device:R`` works against an installed KiCad instead of failing
    ``SYMBOL_NOT_FOUND``. Discovered libraries are appended *after* the explicit
    sources, so an explicit ``--symbols`` always wins.

    ``project_dir`` is where the project tables are looked for (default: CWD).
    Global-table discovery honours ``KICAD_CONFIG_HOME`` (see
    :func:`..libtable._kicad_config_bases`), which is how tests point it at a
    fixture table instead of the machine's real KiCad install.
    """
    sources: list = []
    for s in getattr(args, "symbols", None) or []:
        sources.append(s)
    # config [paths] entries pointing at .kicad_sym files are usable symbol sources
    for key, val in (getattr(cfg, "paths", None) or {}).items():
        if isinstance(val, str) and val.lower().endswith(".kicad_sym"):
            sources.append(val)
    if lib_ids:
        sources.extend(_table_symbol_sources(lib_ids, sources, project_dir))
    return sources


def _oplist_lib_ids(oplist) -> list[str]:
    """Every ``lib_id`` an op-list references, in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for op in (oplist or {}).get("ops") or ():
        lib_id = op.get("lib_id") if isinstance(op, dict) else None
        if isinstance(lib_id, str) and lib_id and lib_id not in seen:
            seen.add(lib_id)
            out.append(lib_id)
    return out


def _table_symbol_sources(lib_ids, explicit: list, project_dir=None) -> list[str]:
    """``.kicad_sym`` paths the lib-tables register for the needed nicknames.

    Deterministic (first-seen nickname order) and best-effort: an unreadable
    table, an unresolvable ``${VAR}`` uri or a missing file is simply skipped —
    the caller then reports the original ``SYMBOL_NOT_FOUND``.
    """
    nicks: list[str] = []
    seen: set[str] = set()
    for lib_id in lib_ids or ():
        nick = str(lib_id or "").split(":", 1)[0] if ":" in str(lib_id or "") else ""
        if nick and nick not in seen:
            seen.add(nick)
            nicks.append(nick)
    if not nicks:
        return []
    # a nickname whose library is already passed explicitly needs no lookup
    stems = {Path(str(s)).stem for s in explicit if isinstance(s, (str, Path))}
    nicks = [n for n in nicks if n not in stems]
    if not nicks:
        return []

    from .. import libtable
    try:
        ws = libtable.discover(Path(project_dir) if project_dir else Path.cwd())
    except Exception:
        return []
    out: list[str] = []
    for nick in nicks:
        where = ws.locate_sym_nick(nick)
        if where is None:
            continue                       # nickname in neither table
        table = ws.sym_table if where == "project" else ws.global_sym_table
        entry = table.get(nick) if table is not None else None
        if entry is None or (entry.type or "KiCad").lower() != "kicad":
            continue                       # unregistered, or a non-KiCad plugin
        path, unresolved = ws.resolve_uri(entry.uri)
        if unresolved or path.suffix.lower() != ".kicad_sym" or not path.is_file():
            continue
        out.append(str(path))
    return out


def _draw_exit(results: list, findings: list) -> int:
    """Exit 6 (OPLIST) when any op errored or connectivity has an error finding."""
    if any(r.status == "error" for r in results):
        return EXIT["OPLIST"]
    actionable = {_report.Severity.ERROR, _report.Severity.CRITICAL}
    if any(f.severity in actionable for f in findings):
        return EXIT["OPLIST"]
    return EXIT["OK"]
