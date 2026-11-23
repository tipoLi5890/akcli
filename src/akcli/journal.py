"""Workspace write journal: `.akcli/journal.jsonl` next to the edited file.

The CLI is stateless per invocation, but an agent's work is a multi-step
session. Every write-path command (``plan``/``draw``/``arrange``/``undo``/
``relink-symbols``) appends one JSON line here so a later invocation — or a
harness hook — can answer "what was the last edit, was it applied, was there a
dry-run for this op-list, how deep is the undo stack" without re-deriving it.

Contract
--------
* One directory-level journal per workspace: ``<target-dir>/.akcli/journal.jsonl``.
  The ``.akcli/`` directory is the workspace state root; rotated draw backups
  live under ``.akcli/backups/`` (see :func:`backups_dir`).
* The state root is **self-ignoring**: whoever creates it (see
  :func:`ensure_workspace_dir`) drops a ``.gitignore`` containing ``*`` inside
  it, so the user's own ``.gitignore`` never needs an ``.akcli/`` entry and
  ``git status`` stays clean without akcli touching any user file.
* Append-only JSONL; every entry carries ``journal_version``/``ts`` (UTC ISO
  8601)/``cmd``/``target``/``status``. Readers skip corrupt lines.
* Write commands may attach a free-form ``note`` (``--note``) recording WHY
  an edit was made — design intent next to the mechanical record.
* Journaling **never fails the parent command**: any ``OSError`` degrades to a
  stderr note. ``AKCLI_JOURNAL=off`` disables writes entirely.
* Size-capped: past ``_MAX_BYTES`` the file rotates once to
  ``journal.jsonl.1`` (the tail of history survives, unbounded growth doesn't).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import contract
from .safety import ensure_state_root_ignore

JOURNAL_VERSION = contract.JOURNAL_VERSION
DIR_NAME = ".akcli"
FILE_NAME = "journal.jsonl"
BACKUP_DIR_NAME = "backups"

_MAX_BYTES = 5 * 1024 * 1024


def workspace_dir(target: Path) -> Path:
    """The ``.akcli/`` state root for the workspace containing ``target``."""
    base = target if target.is_dir() else target.parent
    return base / DIR_NAME


def journal_path(target: Path) -> Path:
    """The journal file for the workspace containing ``target`` (file or dir)."""
    return workspace_dir(target) / FILE_NAME


def backups_dir(target: Path) -> Path:
    """Rotated-backup directory (``.akcli/backups/``) for ``target``'s workspace.

    Writers create it lazily on the first backed-up apply (via
    :func:`ensure_backups_dir`); readers must tolerate its absence (pre-0.12
    workspaces kept ``<name>.bak`` next to the edited file — see the legacy
    fallback in ``commands/drawing.py``).
    """
    return workspace_dir(target) / BACKUP_DIR_NAME


def ensure_self_ignore(root: Path) -> None:
    """Drop a self-ignoring ``.gitignore`` (``*``) into the state root ``root``.

    The state layer's name for :func:`safety.ensure_state_root_ignore`, which
    owns the one implementation: ``safety.atomic_write_with_backup`` writes the
    same file for an ``.akcli/`` backup dir, and a leaf module cannot import
    this one (the layering rule that used to be waived). Best-effort — state-dir
    hygiene must never fail the parent write command; an existing ``.gitignore``
    is left alone (user-managed content wins).
    """
    ensure_state_root_ignore(root)


def ensure_workspace_dir(target: Path) -> Path:
    """Create (if needed) and return the self-ignored ``.akcli/`` state root."""
    root = workspace_dir(target)
    root.mkdir(parents=True, exist_ok=True)
    ensure_self_ignore(root)
    return root


def ensure_backups_dir(target: Path) -> Path:
    """Create (if needed) and return ``.akcli/backups/`` for ``target``."""
    b = ensure_workspace_dir(target) / BACKUP_DIR_NAME
    b.mkdir(exist_ok=True)
    return b


def enabled() -> bool:
    return os.environ.get("AKCLI_JOURNAL", "").lower() not in ("off", "0", "no")


def record(target: Path, cmd: str, status: str, **fields: object) -> None:
    """Append one entry for an edit of ``target``. Never raises for I/O."""
    if not enabled():
        return
    entry: dict = {
        "journal_version": JOURNAL_VERSION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cmd": cmd,
        "target": target.name,
        "status": status,
    }
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    path = journal_path(target)
    try:
        ensure_workspace_dir(target)
        try:
            if path.stat().st_size > _MAX_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(f"note: journal write skipped: {exc}\n")


def read_entries(where: Path, target: str | None = None,
                 limit: int | None = None) -> list[dict]:
    """Entries (oldest → newest) from the journal at/for ``where``.

    ``where`` may be the workspace directory or any file inside it. ``target``
    filters by edited file name; ``limit`` keeps only the newest N entries.
    Corrupt lines are skipped, never fatal.
    """
    path = journal_path(where)
    entries: list[dict] = []
    # The rotated generation first: right after a rotation the live file is
    # empty or missing, and the history must not vanish with it.
    for part in (path.with_suffix(".jsonl.1"), path):
        try:
            text = part.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict):
                if target is not None and doc.get("target") != target:
                    continue
                entries.append(doc)
    if limit is not None and limit >= 0:
        entries = entries[-limit:]
    return entries
