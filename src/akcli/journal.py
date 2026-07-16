"""Workspace write journal: `.akcli/journal.jsonl` next to the edited file.

The CLI is stateless per invocation, but an agent's work is a multi-step
session. Every write-path command (``plan``/``draw``/``arrange``/``undo``/
``relink-symbols``) appends one JSON line here so a later invocation — or a
harness hook — can answer "what was the last edit, was it applied, was there a
dry-run for this op-list, how deep is the undo stack" without re-deriving it.

Contract
--------
* One directory-level journal per workspace: ``<target-dir>/.akcli/journal.jsonl``.
* Append-only JSONL; every entry carries ``journal_version``/``ts`` (UTC ISO
  8601)/``cmd``/``target``/``status``. Readers skip corrupt lines.
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

JOURNAL_VERSION = "1.0"
DIR_NAME = ".akcli"
FILE_NAME = "journal.jsonl"

_MAX_BYTES = 5 * 1024 * 1024


def journal_path(target: Path) -> Path:
    """The journal file for the workspace containing ``target`` (file or dir)."""
    base = target if target.is_dir() else target.parent
    return base / DIR_NAME / FILE_NAME


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
        path.parent.mkdir(exist_ok=True)
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
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
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
