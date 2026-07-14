"""Optional secondary verify wrapper around the ``kicad-cli`` tool (SPEC §3.7).

This is **advisory only**. The primary post-write gate is the pure-Python
:mod:`..writers.connectivity` (which needs no KiCad install); ``kicad-cli`` — when
present — provides a second opinion via KiCad's own ERC / netlist export.

Every entry point is **gated on** :func:`shutil.which`: a missing ``kicad-cli`` is
**non-fatal** and returns ``None`` (never raises). Subprocesses run through
:func:`..safety.run_subprocess` (``shell=False``, absolute exe, timeout + output
cap). Input paths are passed **absolute** instead of behind a ``--`` separator:
KiCad 10's argument parser rejects ``--`` ("Unknown argument"), which silently
degraded every advisory run to ``report: null``; an absolute path cannot start
with ``-``, so the option-injection property is kept.

Critical correctness note (SPEC §3.7, risk #14): we **never** pass
``--exit-code-violations`` to ``sch erc``. With that flag KiCad exits non-zero when
it finds *design* violations, which we would misread as a *tool* failure; without
it ``erc`` exits 0 even with violations and we read the JSON report instead. A
genuinely non-zero exit therefore signals a real tool/our-write problem.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..safety import run_subprocess

__all__ = ["available", "find", "version", "erc", "netlist", "EXE"]

EXE = "kicad-cli"

# Known install locations probed when the tool is neither in ``KICAD_CLI`` nor
# on PATH — the single discovery ladder shared by the advisory ERC here, the
# ``view live`` SVG exporter, and ``akcli doctor`` (they must never disagree).
_FALLBACKS = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/lib/kicad/bin/kicad-cli",
)
_WIN_GLOB = r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"


def _version_key(path: str) -> tuple:
    """Numeric sort key for versioned install dirs (10.0 must beat 9.0)."""
    return tuple(int(n) for n in re.findall(r"\d+", path))


def find() -> str | None:
    """The ``kicad-cli`` executable to run, or ``None``.

    Ladder: ``KICAD_CLI`` env (used verbatim when it exists on disk) →
    ``PATH`` → known bundle/install locations (newest Windows version first).
    """
    env = os.environ.get("KICAD_CLI")
    if env:
        return env if Path(env).exists() else None
    found = shutil.which(EXE)
    if found:
        return found
    for cand in _FALLBACKS:
        if Path(cand).exists():
            return cand
    wins = sorted(glob.glob(_WIN_GLOB), key=_version_key, reverse=True)
    return wins[0] if wins else None

# Generous per-invocation timeout (ERC on a large board is not instant, but a hang
# must not wedge the CLI). Output is capped inside run_subprocess.
_TIMEOUT = 120.0


def available() -> bool:
    """``True`` when a ``kicad-cli`` executable can be found (see :func:`find`)."""
    return find() is not None


def version() -> tuple[int, ...] | None:
    """Return ``kicad-cli`` version as an int tuple (e.g. ``(8, 0, 4)``), or ``None``.

    ``None`` when the tool is absent or its version output cannot be parsed; never
    raises for an absent tool.
    """
    if not available():
        return None
    try:
        proc = run_subprocess([find() or EXE, "version"], timeout=_TIMEOUT)
    except Exception:  # pragma: no cover - defensive: tool present but unrunnable
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return _parse_version(text)


def _parse_version(text: str) -> tuple[int, ...] | None:
    """Extract the first ``N.N[.N...]`` dotted-int run from ``text``."""
    import re

    m = re.search(r"(\d+(?:\.\d+)+)", text)
    if not m:
        return None
    try:
        return tuple(int(part) for part in m.group(1).split("."))
    except ValueError:  # pragma: no cover
        return None


def _major() -> int | None:
    v = version()
    return v[0] if v else None


def erc(path: str) -> dict | None:
    """Run KiCad's ERC on a ``.kicad_sch`` and return a parsed report dict.

    Uses ``sch erc --format json`` on KiCad >= 8 (parsing the JSON report file);
    on older KiCad (7) — which has no JSON ERC — it falls back to
    :func:`netlist` as a best-effort "does this parse + net out" check. Returns
    ``None`` when ``kicad-cli`` is absent or the report cannot be produced/parsed.
    Never passes ``--exit-code-violations``.
    """
    if not available():
        return None
    major = _major()
    if major is not None and major < 8:
        return netlist(path)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "erc.json"
        try:
            proc = run_subprocess(
                [find() or EXE, "sch", "erc", "--format", "json", "--output", str(out),
                 str(Path(path).absolute())],
                timeout=_TIMEOUT,
            )
        except Exception:  # pragma: no cover - tool present but failed to spawn
            return None
        report: dict | None = None
        if out.exists():
            try:
                report = json.loads(out.read_text(encoding="utf-8"))
            except (ValueError, OSError):  # pragma: no cover
                report = None
        return {
            "tool": "kicad-cli",
            "command": "sch erc",
            "exit_code": proc.returncode,
            "report": report,
            "stderr": (proc.stderr or b"").decode("utf-8", "replace"),
        }


def netlist(path: str) -> dict | None:
    """Export a KiCad netlist for ``path`` (best-effort second opinion).

    Returns a dict describing the exported netlist (the netlist text is returned
    inline) or ``None`` when ``kicad-cli`` is absent / the export failed.
    """
    if not available():
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "netlist.net"
        try:
            proc = run_subprocess(
                [find() or EXE, "sch", "export", "netlist", "--output", str(out),
                 str(Path(path).absolute())],
                timeout=_TIMEOUT,
            )
        except Exception:  # pragma: no cover
            return None
        text = None
        if out.exists():
            try:
                text = out.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover
                text = None
        return {
            "tool": "kicad-cli",
            "command": "sch export netlist",
            "exit_code": proc.returncode,
            "netlist": text,
            "stderr": (proc.stderr or b"").decode("utf-8", "replace"),
        }
