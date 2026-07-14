"""libngspice runner with child-subprocess isolation.

A malformed SPICE deck can make ngspice call ``abort()`` on the whole process,
and a pathological transient can spin forever. To contain both, the shared
library is never driven in-process: :func:`run` writes the deck and command
list to ``workdir`` and spawns ``python -m akcli.sim.engine`` as a
child (see :func:`_child_main`). The child loads libngspice via ctypes using
the callback pattern proven against libngspice 45.2, feeds the deck with
``circbyline``, executes each command, and echoes every ngspice ``SendChar``
line to stdout. The parent kills the child on timeout and scrapes ``.meas``
output and ``wrdata`` files from the result — it never raises for engine
trouble; :class:`EngineResult.error` carries the failure instead.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- library discovery ------------------------------------------------------

# macOS KiCad bundle locations (the lib ships inside KiCad.app).
_MACOS_KICAD = (
    "/Applications/KiCad/KiCad.app/Contents/PlugIns/sim/libngspice.0.dylib",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/libngspice.0.dylib",
)
# Common Linux sonames (searched on the default loader path).
_LINUX_SONAMES = ("libngspice.so.0", "libngspice.so")
# Windows: KiCad ships libngspice*.dll under each versioned install's bin/.
_WIN_GLOB = "C:/Program Files/KiCad/*/bin/libngspice*.dll"


def _loadable(path: str) -> bool:
    """True if ``path`` can actually be dlopen'd as a shared library."""
    import ctypes

    try:
        ctypes.CDLL(path)
    except OSError:
        return False
    return True


def available() -> str | None:
    """Return a loadable libngspice path, or ``None`` if none is found.

    Search order: the ``AKCLI_NGSPICE`` env override (a path; ``"0"``/``"off"``
    disables simulation entirely) -> the macOS KiCad bundle -> the platform
    loader's ``find_library("ngspice")`` -> common Linux sonames -> the newest
    Windows KiCad install. Each candidate is verified by actually loading it.
    """
    env = os.environ.get("AKCLI_NGSPICE")
    if env is not None:
        low = env.strip().lower()
        if low in ("0", "off", "none", "false", ""):
            return None
        if _loadable(env):
            return env
        # An explicit override that will not load is a dead end, not a signal
        # to fall through and silently pick a different library.
        return None

    for cand in _MACOS_KICAD:
        if os.path.exists(cand) and _loadable(cand):
            return cand

    import ctypes.util

    found = ctypes.util.find_library("ngspice")
    if found and _loadable(found):
        return found

    for soname in _LINUX_SONAMES:
        if _loadable(soname):
            return soname

    # Newest KiCad version first (numeric, so 10.0 beats 9.0 — a plain string
    # reverse-sort ranks '9.0' above '10.0').
    for cand in sorted(glob.glob(_WIN_GLOB), key=_kicad_version_key, reverse=True):
        if _loadable(cand):
            return cand

    return None


def _kicad_version_key(path: str) -> tuple[int, ...]:
    """Numeric sort key for a Windows KiCad install path's version directory.

    Extracts the ``<ver>`` from ``.../KiCad/<ver>/bin/...`` and returns its digit
    groups as an int tuple (``.../KiCad/10.0/...`` -> ``(10, 0)``), so ``10.0``
    sorts above ``9.0``. Paths without a parseable version sort lowest.
    """
    norm = path.replace("\\", "/")
    m = re.search(r"/KiCad/([^/]+)/", norm)
    nums = re.findall(r"\d+", m.group(1)) if m else []
    return tuple(int(n) for n in nums) if nums else (-1,)


# --- parent-side runner -----------------------------------------------------


@dataclass
class EngineResult:
    """Outcome of one engine invocation.

    ``ok`` is True only when the child completed and exited cleanly. ``error``
    carries any engine-level trouble (timeout, load failure, nonzero exit);
    :func:`run` never raises for these. ``meas_lines`` are the ``.meas`` result
    lines scraped from stdout, ``log`` is the full child output, and
    ``wave_files`` lists absolute paths of any ``wrdata`` files the deck wrote
    into ``workdir``.
    """

    ok: bool
    meas_lines: list[str] = field(default_factory=list)
    log: str = ""
    wave_files: list[str] = field(default_factory=list)
    error: str | None = None


def _is_meas_line(line: str) -> bool:
    """A ``.meas`` result (``name = value``) or a failed-measure marker."""
    return " = " in line or "failed!" in line


# Output substrings that mark a fatal ngspice failure (deck not parsed, analysis
# error). ``meas ... failed!`` is deliberately NOT fatal — a WHEN/edge that never
# crossed is an assertion-level outcome the caller reports as SIM_MEAS_FAILED.
_FATAL_SUBSTRINGS = ("circuit not parsed", "fatal", "error:")


def _is_fatal_line(line: str) -> bool:
    """True when an ngspice output line signals a fatal (deck/analysis) failure."""
    low = line.lower()
    if "failed!" in low:
        return False
    return any(marker in low for marker in _FATAL_SUBSTRINGS)


def _first_fatal_line(text: str) -> str | None:
    """First fatal line in ``text`` (see :func:`_is_fatal_line`), or ``None``."""
    for raw in text.splitlines():
        line = raw.strip()
        if line and _is_fatal_line(line):
            return line
    return None


def run(
    deck_text: str,
    commands: list[str],
    *,
    timeout: float = 60.0,
    workdir: Path,
) -> EngineResult:
    """Simulate ``deck_text`` in an isolated child, run ``commands``, collect results.

    ``commands`` are ngspice control lines (``run``, ``meas ...``, ``wrdata
    ...``) executed after the deck is loaded. The child is killed if it does not
    finish within ``timeout`` seconds. Returns an :class:`EngineResult`; engine
    trouble is reported via its ``error`` field rather than raised.
    """
    # Resolve to an absolute path: the child runs with cwd=workdir, so relative
    # deck/command paths would otherwise be looked up under workdir/workdir.
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    deck_file = workdir / "deck.cir"
    cmd_file = workdir / "commands.txt"
    deck_file.write_text(deck_text, encoding="utf-8")
    cmd_file.write_text("\n".join(commands) + "\n", encoding="utf-8")

    # Snapshot pre-existing files so we can attribute new ones to wrdata.
    before = {p.name for p in workdir.iterdir()}

    argv = [
        sys.executable,
        "-m",
        "akcli.sim.engine",
        str(deck_file),
        str(cmd_file),
    ]
    # Propagate the parent's import path so the child can import this package
    # regardless of how it was made importable (editable install, src-layout
    # path insertion, a bare PYTHONPATH, ...).
    env = os.environ.copy()
    # Absolutize each entry: the child runs with cwd=workdir (a tempdir), so a
    # relative sys.path entry (e.g. "src" from `PYTHONPATH=src python -m ...`)
    # would resolve against the tempdir and the child would die with
    # ModuleNotFoundError, misreported as an ngspice failure (exit 7).
    extra = [os.path.abspath(p) for p in sys.path if p]
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(extra + ([prior] if prior else []))
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child by the time this raises.
        partial = ""
        if exc.stdout:
            partial = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(
                "utf-8", "replace"
            )
        return EngineResult(
            ok=False,
            log=partial,
            error=f"timeout after {timeout:g}s",
        )
    except OSError as exc:  # pragma: no cover - spawning python should not fail
        return EngineResult(ok=False, error=f"could not spawn engine child: {exc}")

    log = (proc.stdout or "") + (proc.stderr or "")
    meas_lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if _is_meas_line(ln)]

    wave_files = sorted(
        str(workdir / name)
        for name in ({p.name for p in workdir.iterdir()} - before)
        if name not in ("deck.cir", "commands.txt")
    )

    error: str | None = None
    if proc.returncode != 0:
        # Prefer the actual fatal ngspice line (e.g. 'Error: circuit not
        # parsed.') the child echoed; fall back to the tail of its output.
        first_fatal = _first_fatal_line(proc.stdout or "")
        if first_fatal:
            detail = first_fatal
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            detail = " | ".join(tail) if tail else ""
        error = f"engine exited with code {proc.returncode}" + (
            f": {detail}" if detail else ""
        )

    return EngineResult(
        ok=proc.returncode == 0,
        meas_lines=meas_lines,
        log=log,
        wave_files=wave_files,
        error=error,
    )


# --- child mode -------------------------------------------------------------

# ctypes callbacks must outlive the ngSpice_Init call; keep hard refs here.
_KEEP: list = []


def _child_main(deck_path: str, cmd_path: str) -> int:
    """Load libngspice, feed the deck, run each command; echo ngspice output.

    Runs inside the spawned child. Returns a process exit code: 0 on completion,
    nonzero if the library cannot be found or loaded. A fatal ngspice abort
    routes through :func:`ControlledExit` and terminates via ``os._exit`` so it
    can never hang the child.
    """
    import ctypes

    lib = available()
    if lib is None:
        sys.stderr.write("SIM_ENGINE: libngspice not found\n")
        return 3
    try:
        ng = ctypes.CDLL(lib)
    except OSError as exc:
        sys.stderr.write(f"SIM_ENGINE: cannot load {lib}: {exc}\n")
        return 3

    # Mutable box so the ngspice output callback can flag a fatal line for the
    # command loop below (nonlocal via dict mutation — no rebinding needed).
    fatal: dict[str, object] = {"hit": False, "line": ""}

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    def send_char(msg, _id, _user):  # ngspice stdout/stderr, one line per call
        try:
            text = msg.decode("utf-8", "replace")
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            if not fatal["hit"] and _is_fatal_line(text):
                fatal["hit"] = True
                fatal["line"] = text.strip()
        except Exception:
            pass
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    def send_stat(_msg, _id, _user):  # simulation status; we don't surface it
        return 0

    @ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool,
        ctypes.c_int, ctypes.c_void_p,
    )
    def controlled_exit(status, _immediate, _quit, _id, _user):
        # A fatal ngspice fault lands here; bail hard so nothing can deadlock.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(int(status) & 0xFF)

    _KEEP.extend([send_char, send_stat, controlled_exit])
    ng.ngSpice_Init(send_char, send_stat, controlled_exit, None, None, None, None)

    deck = Path(deck_path).read_text(encoding="utf-8")
    # Fresh process => no prior circuit to clear; feed the deck line by line.
    for line in deck.splitlines():
        ng.ngSpice_Command(b"circbyline " + line.encode("utf-8"))

    cmd_rc = 0
    for cmd in Path(cmd_path).read_text(encoding="utf-8").splitlines():
        cmd = cmd.strip()
        if cmd:
            rc = ng.ngSpice_Command(cmd.encode("utf-8"))
            try:
                if int(rc) not in (0, 1):  # ngspice returns 1 on a benign quit
                    cmd_rc = int(rc)
            except (TypeError, ValueError):
                pass

    sys.stdout.flush()
    sys.stderr.flush()
    # A deck that failed to parse (or an analysis that errored) still returns
    # here with the callbacks never raising — so scan for the fatal marker the
    # engine printed and fail LOUDLY, rather than let the parent report ok=True.
    if fatal["hit"]:
        sys.stderr.write(f"SIM_ENGINE: fatal: {fatal['line']}\n")
        return 1
    if cmd_rc:
        sys.stderr.write(f"SIM_ENGINE: ngspice command failed (rc={cmd_rc})\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Child entry point: ``python -m akcli.sim.engine <deck> <cmds>``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        sys.stderr.write(
            "usage: python -m akcli.sim.engine <deck.cir> <commands.txt>\n"
        )
        return 2
    return _child_main(args[0], args[1])


if __name__ == "__main__":
    raise SystemExit(main())
