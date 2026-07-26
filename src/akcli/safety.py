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


def read_bytes_guarded(path: os.PathLike | str,
                       max_bytes: int = MAX_FILE_BYTES) -> bytes:
    """Read a whole file, mapping OS-level failures onto structured codes.

    ``Path.read_bytes`` raises the raw OSError family, which used to escape
    all the way to ``cli.main``'s last-resort handler and surface as
    ``INTERNAL`` + a traceback. Every failure mode an *input path* can have is
    classified here instead:

    * missing path -> ``FileNotFoundError`` is re-raised unchanged (``cli``
      renders it as the ``FILE_NOT_FOUND`` envelope / exit 4, and callers that
      want a nicer message catch it themselves);
    * a directory (or mode-000 file, or any other IO error) ->
      ``FILE_UNREADABLE`` / exit 4. Note the platform split this hides: on
      POSIX a directory read is ``IsADirectoryError``, on Windows it is
      ``PermissionError`` — both are ``OSError``, so both land here.
    * oversized -> ``KICAD_SEXPR_TOOBIG`` (the existing allocation guard).
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        fail("FILE_UNREADABLE", f"{p}: {exc.strerror or exc}")
    if len(data) > max_bytes:
        fail("KICAD_SEXPR_TOOBIG", f"file exceeds {max_bytes} bytes")
    return data


def read_text_guarded(path: os.PathLike | str,
                      max_bytes: int = MAX_FILE_BYTES) -> str:
    """:func:`read_bytes_guarded` + a STRICT UTF-8 decode.

    The one reader every ``.kicad_sch`` / ``.kicad_sym`` text read goes
    through. Strictness is the point: decoding with ``errors="replace"`` let
    2 KB of random bytes parse into an empty-looking document, so ``arrange``
    reported ``clean: true`` on a file ``read`` called corrupt — one file, two
    answers. Undecodable bytes are ``KICAD_BAD_ENCODING`` (exit 3), the
    KiCad-side counterpart of ``ALTIUM_BAD_MAGIC``: these bytes are not this
    format.
    """
    data = read_bytes_guarded(path, max_bytes)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail("KICAD_BAD_ENCODING",
             f"{Path(path)} is not valid UTF-8 text "
             f"(byte 0x{data[exc.start]:02x} at offset {exc.start}); "
             "KiCad documents are UTF-8")


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


def refuse_if_gui_locked(
    paths: os.PathLike | str | list | tuple,
    *,
    allow_open: bool = False,
    what: str = "this edit",
) -> None:
    """Refuse (``TARGET_LOCKED``) when KiCad holds any of ``paths`` open.

    KiCad drops a ``~<name>.lck`` next to a document it has open; writing under
    an open GUI is a losing race (the GUI's later save overwrites the file from
    memory). ``draw --apply`` has always refused this way — this is the shared
    form of the invariant so *every* schematic/library write path (op-list
    executor, ``relink``, ``library repair``) refuses identically, before
    touching a single byte.

    The lock-file naming has exactly one source of truth,
    :func:`akcli.writers.kicad.gui_lock_path`, imported lazily here:
    ``writers.kicad`` imports this module at module level, so a top-level
    import back would be a cycle.
    """
    if allow_open:
        return
    from .writers.kicad import gui_lock_path

    items = [paths] if isinstance(paths, (str, os.PathLike)) else list(paths)
    for cand in items:
        p = Path(os.fspath(cand))
        lck = gui_lock_path(p)
        if lck.exists():
            fail("TARGET_LOCKED",
                 f"{p.name} appears open in the KiCad GUI (found {lck.name}); "
                 f"close it first — refusing {what}, because a GUI save would "
                 "overwrite it")


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
        if bd.parent.name == ".akcli":
            # every `.akcli/` creation path self-ignores the state root, so
            # the user's .gitignore never needs an entry (journal.py contract)
            from . import journal
            journal.ensure_self_ignore(bd.parent)
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
