"""Rotated-backup bookkeeping: where snapshots live and how to enumerate them.

``draw --apply`` leaves ``<name>.bak, .bak2, …`` under the workspace's
``.akcli/backups/`` (:mod:`..journal` owns the state root, :mod:`.atomic` owns
the rotation and the file NAMES). This module is the read side of that
contract — the directory, the human/journal label, and the on-disk stack the
``undo`` engine walks — kept next to the writer that produces it so the naming
rule has exactly one author.
"""

from __future__ import annotations

from pathlib import Path

from .. import journal

# ONE naming authority: the writer both rotates and names the backups, so the
# scan below uses the writer's own function (a local copy of the rule drifting
# from it would silently hide snapshots).
from .atomic import backup_name

__all__ = ["BACKUP_SCAN_MAX", "backup_label", "backup_stack", "backups_dir"]

BACKUP_SCAN_MAX = 99          # matches the config backup_depth ceiling


def backups_dir(target: Path) -> Path:
    """Where new rotated backups go: ``.akcli/backups/`` in the workspace."""
    return journal.backups_dir(target)


def backup_label(target: Path) -> str:
    """Human/journal label for the level-1 backup (workspace-relative posix)."""
    return f"{journal.DIR_NAME}/{journal.BACKUP_DIR_NAME}/{target.name}.bak"


def backup_stack(target: Path) -> list[tuple[Path, int]]:
    """Every rotated backup for ``target``, newest first: ``[(path, level), …]``.

    Gap-TOLERANT: a missing level (e.g. a crash between the rotation renames
    and the fresh ``.bak`` copy) must not hide the deeper snapshots that still
    exist — ``undo --list``/``--steps`` walk whatever is really on disk, in
    level order.

    Backups live in ``.akcli/backups/``; when that directory holds none for
    this target, fall back to the legacy pre-0.12 location (``<name>.bak``
    next to the file) so `undo` keeps working on an existing workspace.
    """
    def _scan(base: Path) -> list[tuple[Path, int]]:
        return [
            (p, level)
            for level in range(1, BACKUP_SCAN_MAX + 1)
            if (p := base / backup_name(target.name, level)).exists()
        ]

    return _scan(backups_dir(target)) or _scan(target.parent)
