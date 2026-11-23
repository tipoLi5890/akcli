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
``--exit-code-violations`` to ``sch erc`` (or ``pcb drc``). With that flag KiCad
exits non-zero when it finds *design* violations, which we would misread as a
*tool* failure; without it ``erc``/``drc`` exit 0 even with violations and we
read the JSON report instead. A genuinely non-zero exit therefore signals a
real tool/our-write problem.

``pcb drc`` (:func:`drc`) is the one entry point here that a *gate* consumes
(``release preflight --drc``), so it carries two extra guarantees:

* **It cannot modify the user's project.** Measured on kicad-cli 10.0.4:
  ``pcb drc`` leaves the ``.kicad_pcb`` and ``.kicad_pro`` byte-identical but
  CREATES ``<board>.kicad_prl`` beside the board when none exists — even when
  the board fails to load (exit 3), even for a path that does not exist. So
  :func:`drc` never points KiCad at the user's directory: it stages the board
  and the project files DRC actually reads into a temp dir (see
  :func:`_stage_board`) and runs there. ``--save-board`` and
  ``--refill-zones`` are never passed, so zone fills are evaluated as saved.
* **A report it cannot produce is reported as such** (``report: None`` plus an
  ``error`` string), never raised and never mistaken for a clean board.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .. import sexpr
from ..errors import AkcliError
from ..kicad_escape import quote_atom
from ..safety import run_subprocess

__all__ = ["available", "find", "version", "version_text", "erc", "drc",
           "netlist", "EXE"]

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
# DRC walks every copper item against every other; a dense multilayer board
# legitimately takes minutes. Still bounded — a release gate must not hang.
_DRC_TIMEOUT = 600.0


def available() -> bool:
    """``True`` when a ``kicad-cli`` executable can be found (see :func:`find`)."""
    return find() is not None


def version_text() -> str | None:
    """``kicad-cli version`` verbatim (first line, e.g. ``"10.0.4"``), or ``None``.

    The string a release manifest records: unparsed on purpose, so a nightly
    or distro build suffix (``9.0.2-1~ubuntu24.04.1``) survives as evidence.
    ``None`` when the tool is absent, unrunnable, or prints nothing.
    """
    exe = find()
    if exe is None:
        return None
    try:
        proc = run_subprocess([exe, "version"], timeout=_TIMEOUT)
    except Exception:  # pragma: no cover - defensive: tool present but unrunnable
        return None
    lines = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return lines[0].strip() if lines else None


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


def _stage_board(src: Path, work: Path) -> Path:
    """Copy ``src`` and the project files DRC reads into ``work``.

    Staged, each only when it exists beside the board:

    * the ``.kicad_pcb`` itself (same file name — the report's ``source`` and
      KiCad's project lookup both key on it);
    * ``<stem>.kicad_pro`` — rule severities, DRC exclusions, net classes and
      board constraints live here; without it KiCad checks the board against
      its DEFAULTS and reports a different board than the user's;
    * ``<stem>.kicad_dru`` — custom design rules;
    * ``fp-lib-table`` — the project footprint libraries the
      ``lib_footprint_*`` checks compare against. ``${KIPRJMOD}`` in it is
      rewritten to the ORIGINAL project directory: in the staging dir it
      would resolve to the temp path, every project-local library would go
      missing, and each footprint from one would raise a
      ``lib_footprint_issues`` warning that an in-place run does not have
      (measured; the rewrite restores the in-place result).

    Deliberately NOT staged: ``.kicad_prl`` (per-user UI state — the file
    KiCad writes, and the reason for staging at all), the schematic and
    ``sym-lib-table`` (only ``--schematic-parity`` reads them; never passed).

    Known limit: a project table that reaches a library through a RELATIVE
    uri, or through an environment variable other than ``${KIPRJMOD}`` that is
    only defined inside the KiCad GUI's project, resolves as it would for any
    ``kicad-cli`` run started outside the project directory.
    """
    board = work / src.name
    shutil.copyfile(src, board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        side = src.with_suffix(suffix)
        if side.is_file():
            shutil.copyfile(side, work / side.name)
    table = src.parent / "fp-lib-table"
    if table.is_file():
        (work / table.name).write_bytes(
            _rehome_lib_table(table.read_bytes(), src.parent))
    return board


def _rehome_lib_table(data: bytes, project_dir: Path) -> bytes:
    """A project lib table with ``${KIPRJMOD}`` pinned to ``project_dir``.

    Only the ``(uri ...)`` atoms that mention the variable are re-written,
    through the tree's one quoter, so a directory name with a space, a quote
    or a backslash stays one well-formed string; every other byte — comments,
    indentation, CRLF line endings — is reproduced by the lossless serializer.
    Bytes in, bytes out: no newline translation, no locale codec.

    A table that is not UTF-8 or does not parse is returned untouched: it is
    not ours to repair, and KiCad then reports it exactly as it would in place.
    """
    try:
        root = sexpr.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, AkcliError):
        return data
    # Forward slashes on every platform: KiCad accepts them on Windows, and a
    # backslash would have to survive both this escape and KiCad's unescape.
    home = project_dir.as_posix()
    changed = False
    for lib in root.find_all("lib"):
        uri = lib.find("uri")
        if uri is None or len(uri.children or ()) < 2:
            continue
        atom = uri.children[1]
        value = atom.value if atom.is_atom else None
        if value and "${KIPRJMOD}" in value:
            atom.text = quote_atom(value.replace("${KIPRJMOD}", home))
            changed = True
    return sexpr.dumps(root).encode("utf-8") if changed else data


def drc(path: str) -> dict | None:
    """Run KiCad's DRC on a ``.kicad_pcb`` and return a parsed report dict.

    ``kicad-cli pcb drc --format json --severity-error --severity-warning``
    against a STAGED COPY of the board (:func:`_stage_board`) — the user's
    project directory is never handed to KiCad, because ``pcb drc`` writes a
    ``.kicad_prl`` there. Never passes ``--save-board``, ``--refill-zones``
    (zone fills are checked exactly as saved) or ``--exit-code-violations``.

    Returns ``None`` when ``kicad-cli`` is absent. Otherwise a dict shaped
    like :func:`erc`'s, with ``report`` set to KiCad's JSON document
    (``$schema: https://schemas.kicad.org/drc.v1.json`` — ``violations`` and
    ``unconnected_items`` lists of ``{type, severity, description, items:
    [{uuid, pos:{x,y}, description}]}``) or ``None`` when no usable report was
    produced; ``error`` then says why (KiCad < 8 has no ``pcb drc``; the tool
    timed out; the board would not load; the JSON was unreadable). It never
    raises for a tool failure.

    Callers must key on ``type`` / ``severity`` / ``uuid`` / ``pos``: every
    ``description`` is LOCALIZED by KiCad's own language setting (the same
    board reports ``Pad 1 [+3V3] of R1 on F.Cu`` or ``1 上 [+3V3] 的焊盤 R1
    F.Cu``), and forcing a language would mean replacing the user's KiCad
    config — and with it the global library table DRC checks against.
    """
    exe = find()
    if exe is None:
        return None
    result: dict = {
        "tool": "kicad-cli",
        "command": "pcb drc",
        "exit_code": None,
        "report": None,
        "stdout": "",
        "stderr": "",
        "error": None,
        "error_code": None,
    }
    ver = version()
    if ver is not None and ver[0] < 8:
        result["error"] = (f"kicad-cli {'.'.join(map(str, ver))} has no "
                           "`pcb drc` (KiCad >= 8 is required)")
        return result

    src = Path(path).absolute()
    # ignore_cleanup_errors: on Windows a lingering handle on the staged
    # board must not turn a finished DRC into a crash during cleanup.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        work = Path(td) / "project"
        work.mkdir()
        out = Path(td) / "drc.json"
        try:
            board = _stage_board(src, work)
            proc = run_subprocess(
                [exe, "pcb", "drc", "--format", "json", "--output", str(out),
                 "--severity-error", "--severity-warning", str(board)],
                timeout=_DRC_TIMEOUT,
            )
        except AkcliError as exc:           # timeout / tool not runnable
            result["error"] = exc.message or exc.code
            result["error_code"] = exc.code
            return result
        except OSError as exc:              # board unreadable, temp dir full
            result["error"] = f"could not stage {src.name} for DRC: {exc}"
            return result
        result["exit_code"] = proc.returncode
        result["stdout"] = (proc.stdout or b"").decode("utf-8", "replace")
        result["stderr"] = (proc.stderr or b"").decode("utf-8", "replace")
        report = None
        if out.exists():
            try:
                report = json.loads(out.read_text(encoding="utf-8"))
            except (ValueError, OSError):   # ValueError: bad JSON or bad UTF-8
                report = None
        if proc.returncode != 0:
            said = (result["stderr"] or result["stdout"]).strip()
            result["error"] = (f"kicad-cli pcb drc exited {proc.returncode}"
                               + (f": {said[-400:]}" if said else ""))
        elif not isinstance(report, dict):
            result["error"] = "kicad-cli pcb drc wrote no readable JSON report"
        else:
            result["report"] = report
        return result


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
