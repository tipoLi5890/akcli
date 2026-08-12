"""``undo`` engine: swap the target with its rotated backups, reversibly.

Three restores, one invariant — **every snapshot lives in some FILE at every
instant**. A swap goes through an on-disk temp (never through RAM), so a crash
mid-undo can at worst leave one pair part-swapped with the third copy in
``<name>.undo-tmp``; it can never lose the only copy of a design.

* :func:`swap` — the single step: target <-> newest backup, so undo twice is
  a redo;
* :func:`walk` — ``--steps N``: reverse the top ``N+1`` of
  ``[file, .bak, .bak2, …]``, which walks back N snapshots AND leaves the
  stack arranged so one more single-step undo redoes the last step;
* :func:`restore_run` — the multi-sheet transaction: all-or-nothing restore
  of every file a routed draw committed, with the completed swaps reversed on
  any failure.

Refusals come back as :class:`UndoRefused` (the caller maps it onto an exit
code) except the two that already have frozen error codes — ``TARGET_LOCKED``
and ``TXN_ROLLBACK_FAILED`` — which are raised as ``AkcliError``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import journal
from ..errors import fail
from ..safety import gui_lock_path
from ..writers.backups import backup_stack, backups_dir

__all__ = [
    "BackupEntry",
    "RunEntry",
    "RunRestore",
    "SwapOutcome",
    "UndoRefused",
    "list_backups",
    "refuse_if_gui_open",
    "restore_run",
    "summarize",
    "swap",
    "walk",
]


class UndoRefused(ValueError):
    """An undo the engine will not perform; the message is the operator's line.

    ``missing`` marks the "there is nothing to restore" class (the caller maps
    it to the not-found exit code); everything else is a refusal to overwrite
    something the user would not get back.
    """

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class BackupEntry:
    """One rotated snapshot on the stack (``undo --list``)."""

    level: int
    path: Path
    size: int
    mtime: str


@dataclass(frozen=True)
class SwapOutcome:
    """Result of a single- or multi-step restore (dry-run included)."""

    applied: bool
    backup: Path
    steps: int
    summary: str


@dataclass(frozen=True)
class RunEntry:
    """One file of a multi-sheet transaction backup."""

    file: str
    backup: str


@dataclass(frozen=True)
class RunRestore:
    """Result of a transaction restore (dry-run lists the same files)."""

    run_id: str
    directory: Path
    entries: tuple[RunEntry, ...]
    applied: bool


def refuse_if_gui_open(target: Path | list[Path] | tuple[Path, ...],
                       allow_open: bool = False) -> None:
    """Refuse a file swap while the KiCad GUI holds any target open.

    ``target`` may be one path or a list (a multi-file ``undo --run`` checks
    every file BEFORE the first swap).

    Deliberately NOT ``safety.refuse_if_gui_locked``: same code, same exit,
    but this remediation names ``--allow-open`` + File>Revert, which is the
    actionable next step for a swap (the writer's phrasing talks about the
    write it is refusing instead).
    """
    if allow_open:
        return
    targets = target if isinstance(target, (list, tuple)) else [target]
    for t in targets:
        lck = gui_lock_path(t)
        if lck.exists():
            fail("TARGET_LOCKED",
                 f"{t.name} appears open in the KiCad GUI "
                 f"(found {lck.name}); close it first, or pass --allow-open and "
                 "File>Revert in KiCad afterwards")


def summarize(cur: Any, old: Any) -> str:
    """One-line parts/nets delta between the current file and a restore target."""
    from ..checks import diff as diffmod
    rep = diffmod.run(cur, old)
    return (f"{len(cur.components)} parts/{len(cur.nets)} nets -> "
            f"{len(old.components)} parts/{len(old.nets)} nets "
            f"(+{len(rep.added_components)} −{len(rep.removed_components)} "
            f"components, {len(rep.member_changed_nets)} nets change membership)")


def list_backups(target: Path) -> list[BackupEntry]:
    """The rotated-backup stack with sizes and mtimes, newest first."""
    entries: list[BackupEntry] = []
    for p, level in backup_stack(target):
        st = p.stat()
        entries.append(BackupEntry(
            level=level, path=p, size=st.st_size,
            mtime=_dt.datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec="seconds")))
    return entries


def _newest_or_refuse(target: Path) -> list[tuple[Path, int]]:
    stack = backup_stack(target)
    if not stack:
        bak = backups_dir(target) / (target.name + ".bak")
        raise UndoRefused(
            f"ERROR: no backup at {bak} (created by `akcli draw --apply`)",
            missing=True)
    return stack


def swap(target: Path, *, apply: bool = False, allow_open: bool = False,
         note: str | None = None) -> SwapOutcome:
    """Single-step undo: swap the target with the NEWEST backup on the stack.

    Normally that is ``<name>.bak`` (undo twice = redo); after a crash left a
    level-1 gap, the stack is gap-tolerant and the swap falls back to the
    newest snapshot that really exists (e.g. ``.bak2``).
    """
    stack = _newest_or_refuse(target)
    bak = stack[0][0]
    from ..readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(bak))
    summary = summarize(cur, old)
    if not apply:
        return SwapOutcome(applied=False, backup=bak, steps=1, summary=summary)
    refuse_if_gui_open(target, allow_open)
    tmp = target.parent / (target.name + ".undo-tmp")
    shutil.copy2(bak, tmp)
    shutil.copy2(target, bak)
    tmp.replace(target)
    journal.record(target, "undo", "applied", steps=1, backup=bak.name,
                   note=note)
    return SwapOutcome(applied=True, backup=bak, steps=1, summary=summary)


def walk(target: Path, steps: int, *, apply: bool = False,
         allow_open: bool = False, note: str | None = None) -> SwapOutcome:
    """Multi-step undo: restore the snapshot ``steps`` back, keeping redo alive.

    Reverses the top ``steps+1`` entries of ``[file, .bak, .bak2, …]``: the file
    becomes the deepest snapshot and ``.bak`` becomes the one just newer than it,
    so a following single-step `undo` swaps that pair and redoes the last step.
    Steps beyond the available stack are clamped to walk back as far as it goes.
    """
    stack = _newest_or_refuse(target)
    steps = min(steps, len(stack))
    # positions[0] = the live file, positions[i] = the level-i backup
    positions = [target] + [p for p, _lvl in stack[:steps]]
    from ..readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(positions[steps]))
    summary = summarize(cur, old)
    if not apply:
        return SwapOutcome(applied=False, backup=positions[steps], steps=steps,
                           summary=summary)
    refuse_if_gui_open(target, allow_open)
    # Reverse the [target, .bak, ...] chain by swapping end-pairs through an
    # ON-DISK temp: every snapshot lives in SOME file at every instant, so a
    # crash mid-undo can at worst leave one pair part-swapped (with the third
    # copy in <name>.undo-tmp) — it can never hold the only copy in RAM.
    lo, hi = 0, len(positions) - 1
    while lo < hi:
        a, b = positions[lo], positions[hi]
        tmp = a.parent / (a.name + ".undo-tmp")
        shutil.copy2(a, tmp)
        shutil.copy2(b, a)
        os.replace(tmp, b)
        lo += 1
        hi -= 1
    journal.record(target, "undo", "applied", steps=steps,
                   backup=positions[steps].name, note=note)
    return SwapOutcome(applied=True, backup=positions[steps], steps=steps,
                       summary=summary)


def restore_run(target: Path, run_id: str, *, apply: bool = False,
                allow_open: bool = False,
                note: str | None = None) -> RunRestore:
    """``undo --run <id>`` — all-or-nothing multi-file restore.

    A multi-sheet draw commits several files in one transaction and copies
    every pre-write file into ``.akcli/backups/runs/<run_id>/`` with a
    manifest. This restores ALL of them by three-way swap (file <-> backup),
    so running the same ``--run`` again is a redo. Refuses when any file was
    modified since the transaction (its sha no longer matches the manifest's
    ``sha_after``) — a swap would silently discard that later edit.
    """
    from ..writers.txn import MANIFEST_NAME

    bdir = backups_dir(target) / "runs" / run_id
    man_path = bdir / MANIFEST_NAME
    if not man_path.is_file():
        raise UndoRefused(
            f"ERROR: no transaction backup {run_id!r} under "
            f"{bdir.parent} (see the journal: `akcli log`)", missing=True)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    entries = manifest.get("files") or []

    stale: list[str] = []
    for m in entries:
        f = Path(m["file"])
        sha = (hashlib.sha256(f.read_bytes()).hexdigest()
               if f.is_file() else "<missing>")
        # forward state == sha_after -> restoring goes BACK to sha_before;
        # forward state == sha_before -> this is a redo (back to sha_after).
        if sha not in (m.get("sha_after"), m.get("sha_before")):
            stale.append(f.name)
    if stale:
        raise UndoRefused(f"REFUSED: {', '.join(stale)} changed since "
                          f"transaction {run_id} (a swap would discard that "
                          "edit); restore manually from "
                          f"{bdir} if you really mean it")

    if apply:
        refuse_if_gui_open([Path(m["file"]) for m in entries], allow_open)
        swapped: list[dict[str, Any]] = []
        try:
            for m in entries:
                f = Path(m["file"])
                bak = bdir / m["backup"]
                tmp = f.parent / f".{f.name}.undorun.tmp"
                shutil.copy2(bak, tmp)
                shutil.copy2(f, bak)
                tmp.replace(f)
                swapped.append(m)
        except BaseException as exc:
            # reverse the completed swaps so the set stays consistent
            try:
                for m in reversed(swapped):
                    f = Path(m["file"])
                    bak = bdir / m["backup"]
                    tmp = f.parent / f".{f.name}.undorun.tmp"
                    shutil.copy2(bak, tmp)
                    shutil.copy2(f, bak)
                    tmp.replace(f)
            except BaseException:
                fail("TXN_ROLLBACK_FAILED",
                     f"undo --run {run_id} failed "
                     f"partway AND could not reverse ({type(exc).__name__}: "
                     f"{exc}); the backup set under {bdir} is intact")
            raise UndoRefused(f"ERROR: undo --run {run_id} failed "
                              f"({type(exc).__name__}: {exc}); every completed "
                              "swap was reversed — nothing changed") from exc
        journal.record(target, "undo", "applied", run_id=run_id,
                       files=[m["file"] for m in entries], note=note)

    return RunRestore(
        run_id=run_id, directory=bdir, applied=apply,
        entries=tuple(RunEntry(file=m["file"], backup=m["backup"])
                      for m in entries))
