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


def _emit(text: str) -> None:
    """Write a data payload to stdout with exactly one trailing newline."""
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
# draw helpers shared by draw / arrange / pins / jlc bom
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


def _draw_exit(results: list, findings: list) -> int:
    """Exit 6 (OPLIST) when any op errored or connectivity has an error finding."""
    if any(r.status == "error" for r in results):
        return EXIT["OPLIST"]
    actionable = {_report.Severity.ERROR, _report.Severity.CRITICAL}
    if any(f.severity in actionable for f in findings):
        return EXIT["OPLIST"]
    return EXIT["OK"]
