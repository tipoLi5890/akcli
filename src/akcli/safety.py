"""Hard limits + safe IO helpers used everywhere (SPEC §3.1).

All readers parse untrusted binary/text input, so every allocation/loop is
bounded by a constant from this module and every path is validated through
:func:`safe_path`. Writes go through :func:`atomic_write_with_backup`
(snapshot -> temp-in-same-dir -> fsync -> ``os.replace``) so a crash never
leaves a half-written user schematic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import fail

# --- Hard limits (allocation/loop guards against hostile input) -------------
MAX_FILE_BYTES: int = 256 * 1024 * 1024        # 256 MiB largest accepted file
MAX_SECTORS: int = 4_000_000                   # CFBF FAT-chain / sector walk cap
MAX_RECORDS: int = 5_000_000                   # Altium record-count cap
MAX_DIR_ENTRIES: int = 200_000                 # CFBF directory-tree entry cap
MAX_DECODED_BYTES: int = 512 * 1024 * 1024     # decoded-stream accumulation cap
MAX_SEXPR_DEPTH: int = 400                     # KiCad S-expr nesting cap
MAX_ATOM_BYTES: int = 4 * 1024 * 1024          # single S-expr atom byte cap (< 10 MiB)
MAX_NODES: int = 20_000_000                    # total S-expr node cap

DEFAULT_SUBPROCESS_MAXOUT: int = 16 * 1024 * 1024  # captured stdout/stderr cap


def safe_path(base: os.PathLike | str, cand: os.PathLike | str) -> Path:
    """Resolve ``cand`` against ``base`` and reject anything escaping ``base``.

    Both paths are fully resolved (following symlinks); if the real candidate
    path is not contained within the real base, raise ``PATH_OUTSIDE_ROOT``.
    Never expands environment variables from untrusted input.
    """
    base_r = Path(base).resolve()
    p = Path(cand)
    if not p.is_absolute():
        p = base_r / p
    cand_r = p.resolve()
    try:
        cand_r.relative_to(base_r)
    except ValueError:
        fail("PATH_OUTSIDE_ROOT", f"{os.fspath(cand)} escapes root {base_r}")
    return cand_r


def run_subprocess(
    argv: list[str],
    timeout: float,
    maxout: int = DEFAULT_SUBPROCESS_MAXOUT,
) -> subprocess.CompletedProcess:
    """Run an external tool safely: ``shell=False``, absolute exe, timeout, output cap.

    The executable name (``argv[0]``) is resolved via ``shutil.which`` to an
    absolute path; a missing tool raises ``KICAD_CLI_MISSING`` and a timeout
    raises ``KICAD_CLI_TIMEOUT``. Callers are responsible for placing ``--``
    before any file paths in ``argv``.
    """
    if not argv:
        fail("KICAD_CLI_MISSING", "empty argv")
    exe = shutil.which(argv[0]) or (argv[0] if os.path.isabs(argv[0]) else None)
    if not exe or not os.path.isabs(exe):
        fail("KICAD_CLI_MISSING", f"executable not found: {argv[0]!r}")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            capture_output=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail("KICAD_CLI_TIMEOUT", f"{argv[0]} timed out after {timeout}s")
    except (FileNotFoundError, PermissionError):
        fail("KICAD_CLI_MISSING", f"executable not runnable: {argv[0]!r}")
    if maxout and proc.stdout is not None and len(proc.stdout) > maxout:
        proc.stdout = proc.stdout[:maxout]
    if maxout and proc.stderr is not None and len(proc.stderr) > maxout:
        proc.stderr = proc.stderr[:maxout]
    return proc


def atomic_write_with_backup(
    path: os.PathLike | str,
    data: bytes | str,
    backup_dir: os.PathLike | str | None = None,
) -> None:
    """Atomically write ``data`` to ``path``, optionally backing up the prior file.

    Sequence: copy existing file into ``backup_dir`` (if given) -> write to a
    temp file in the SAME directory -> ``flush`` + ``fsync`` -> ``os.replace``.
    On any failure the temp file is cleaned up and the original is untouched.
    """
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")

    if backup_dir is not None and path.exists():
        bd = Path(backup_dir)
        bd.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bd / (path.name + ".bak"))

    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
