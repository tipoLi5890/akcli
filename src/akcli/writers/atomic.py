"""Atomic-write primitives shared by the executor and the project transaction.

``prepare()`` / ``commit()`` / ``abort()`` decompose the historical single-shot
atomic write (SPEC §3.5) so a multi-file caller can stage EVERY file's temp —
each written, fsync'd and verified — before the first ``os.replace``: the
two-phase-commit shape. :func:`atomic_write` keeps the one-file composition the
single-file executor uses.

The write itself is **binary** (``os.fdopen(fd, "wb")``) because ``.kicad_sch``
bytes matter (Windows newline translation would break byte-count assertions and
output determinism).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import fail

__all__ = [
    "DEFAULT_BACKUP_DEPTH",
    "PreparedWrite",
    "abort",
    "atomic_write",
    "backup_name",
    "check_snapshot",
    "commit",
    "prepare",
    "rotate_backups",
]

# How many rotated backups to keep on --apply: <name>.bak (newest) plus
# .bak2..bak{depth}. Overridable via config [project] backup_depth; `akcli undo`
# walks this stack. 1 == the historical single-.bak behaviour.
DEFAULT_BACKUP_DEPTH = 3


@dataclass
class PreparedWrite:
    """A staged temp next to its target: written, fsync'd, verified — not yet live."""

    path: Path
    tmp: str


def backup_name(name: str, level: int) -> str:
    """Backup filename for ``level`` >= 1: level 1 is ``<name>.bak``, 2 ``.bak2`` …"""
    return f"{name}.bak" if level <= 1 else f"{name}.bak{level}"


def rotate_backups(bd: Path, name: str, depth: int) -> None:
    """Shift ``<name>.bak`` -> ``.bak2`` -> … -> ``.bak{depth}`` to free ``.bak``.

    The caller writes a fresh ``<name>.bak`` afterwards, so this makes room while
    keeping the ``depth`` most-recent snapshots; the oldest (``.bak{depth}``) is
    overwritten and any deeper stragglers are left untouched. ``depth`` 1 skips
    rotation entirely (historical single-backup behaviour).
    """
    depth = max(1, int(depth))
    for level in range(depth, 1, -1):
        src = bd / backup_name(name, level - 1)
        if src.exists():
            os.replace(src, bd / backup_name(name, level))


def check_snapshot(p: Path, snap_sha: str) -> None:
    """Optimistic-lock re-check: refuse if ``p`` changed since it was snapshotted."""
    try:
        cur = p.read_bytes()
    except OSError:
        cur = b""
    if hashlib.sha256(cur).hexdigest() != snap_sha:
        fail("VERIFY_FAILED",
             f"{p} changed on disk since it was read (optimistic-lock conflict)")


def prepare(
    p: Path, text: str, snap_sha: str,
    verify: Callable[[str], None] | None = None,
) -> PreparedWrite:
    """Stage ``text`` as a temp file next to ``p``: check -> write -> fsync -> verify.

    ``verify`` (if given) receives the temp path and must raise on failure — the
    temp lives in the target's own directory, so relative sheet files resolve
    exactly as they will after the replace. Any failure unlinks the temp and
    leaves the original untouched.
    """
    check_snapshot(p, snap_sha)
    directory = p.parent if str(p.parent) else Path(".")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        if verify is not None:
            verify(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return PreparedWrite(path=p, tmp=tmp)


def commit(
    pw: PreparedWrite, backup_dir=None, backup_depth: int = DEFAULT_BACKUP_DEPTH,
) -> None:
    """Back up the live target (rotated stack), then promote the prepared temp."""
    p = pw.path
    try:
        if backup_dir is not None and p.exists():
            bd = Path(backup_dir)
            bd.mkdir(parents=True, exist_ok=True)
            # Snapshot FIRST, rotate, then promote atomically: a crash
            # mid-sequence leaves at worst a .bak-pending file next to a
            # fully usable stack — never a level-1 gap with the newest
            # snapshot existing only as the (about-to-change) live file.
            pending = bd / (backup_name(p.name, 1) + ".pending")
            shutil.copy2(p, pending)
            rotate_backups(bd, p.name, backup_depth)
            os.replace(pending, bd / backup_name(p.name, 1))
        os.replace(pw.tmp, p)
    except BaseException:
        abort(pw)
        raise


def abort(pw: PreparedWrite) -> None:
    """Discard a prepared temp (idempotent; the target is never touched)."""
    try:
        os.unlink(pw.tmp)
    except OSError:
        pass


def atomic_write(
    p: Path, text: str, snap_sha: str, backup_dir,
    backup_depth: int = DEFAULT_BACKUP_DEPTH,
    verify: Callable[[str], None] | None = None,
) -> None:
    """One-file composition: :func:`prepare` then :func:`commit` (SPEC §3.5)."""
    commit(prepare(p, text, snap_sha, verify=verify), backup_dir, backup_depth)
