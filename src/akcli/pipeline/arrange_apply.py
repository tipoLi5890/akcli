"""``arrange`` writes: planned moves executed through the ordinary draw pipeline.

The layout planners (:mod:`..arrange`) only ever produce ``move_component``
ops; this module is what turns them into a write, and it deliberately reuses
the draw path — atomic write, rotated backup, connectivity re-verify, journal
entry — so ``akcli undo`` reverts an arrange like any other edit.

Two extra rails live here because they are specific to moving finished
copper:

* :func:`check_net_preservation` — the ``--groups`` contract made mechanical:
  dry-apply the rigid bundle moves to a temp copy and require net EQUIVALENCE
  before anything is written;
* :func:`refresh_frames` — redraw group borders after a re-pack, on top of the
  same write so one undo reverts both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import journal
from ..contract import PROTOCOL_VERSIONS
from ..errors import EXIT
from . import draw_exit
from .preview import Warn

__all__ = [
    "FrameApply",
    "MoveApply",
    "NetPreservation",
    "apply_group_frames",
    "check_net_preservation",
    "apply_moves",
    "refresh_frames",
]


@dataclass(frozen=True)
class MoveApply:
    """Per-op results + connectivity findings of an arrange write."""

    exit_code: int
    results: list[Any] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class FrameApply:
    """Result of a ``groups --frame`` write (dry-run included).

    ``ok`` folds "every op succeeded" and "no error/critical connectivity
    finding" — the caller turns it into the exit code.  ``frames`` is the
    number of groups framed (each group costs a rectangle + a title op).
    """

    ok: bool
    frames: int
    results: list[Any] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class NetPreservation:
    """Would the re-layout keep every net? ``lines`` explain a ``False``."""

    equivalent: bool
    lines: tuple[str, ...] = ()


def _oplist(target: Path, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL_VERSIONS["oplist"],
            "target_format": "kicad",
            "target_file": target.name, "ops": ops}


def apply_moves(target: Path, ops: list[dict[str, Any]], *, sources: Any,
                backup_depth: int, allow_open: bool, journal_cmd: str,
                note: str | None = None) -> MoveApply:
    """Execute planned moves on ``target`` (backup + connectivity gate + journal)."""
    from ..writers import kicad as kwriter
    from ..writers.backups import backup_label, backups_dir

    findings: list[Any] = []
    results = kwriter.apply(_oplist(target, ops), str(target), apply=True,
                            sources=sources,
                            verify_out=findings, backup_dir=backups_dir(target),
                            backup_depth=backup_depth,
                            allow_open=allow_open)
    code = draw_exit(results, findings)
    journal.record(target, journal_cmd,
                   "applied" if code == EXIT["OK"] else "refused",
                   op_count=len(ops), note=note,
                   backup=(backup_label(target) if code == EXIT["OK"] else None))
    return MoveApply(exit_code=code, results=results, findings=findings)


def apply_group_frames(target: Path, ops: list[dict[str, Any]], *, sources: Any,
                       apply: bool, allow_open: bool) -> FrameApply:
    """Draw (or dry-run) the planned group frames on ``target``.

    The engine half of ``akcli groups --frame``: keyed frame uuids make a
    re-run replace stale borders in place, so this goes through the ordinary
    writer with a rotated backup and a journal entry — ``akcli undo`` reverts
    a frame draw like any other edit.  Planning (:func:`..arrange.frames.
    plan_frames`) stays with the caller, which owns the "nothing to frame"
    message.
    """
    from ..writers import kicad as kwriter

    findings: list[Any] = []
    results = kwriter.apply(
        _oplist(target, ops), str(target), apply=apply,
        sources=sources, verify_out=findings,
        backup_dir=(journal.backups_dir(target) if apply else None),
        allow_open=allow_open)
    ok = (all(r.status == "ok" for r in results)
          and not any(f.severity.value in ("error", "critical") for f in findings))
    if apply and ok:
        journal.record(
            target, "groups-frame", "applied", op_count=len(ops),
            backup=f"{journal.DIR_NAME}/{journal.BACKUP_DIR_NAME}/"
                   f"{target.name}.bak")
    return FrameApply(ok=ok, frames=len(ops) // 2, results=results,
                      findings=findings)


def check_net_preservation(target: Path, ops: list[dict[str, Any]], *,
                           sources: Any) -> NetPreservation:
    """Dry-apply the moves to a temp copy and diff the netlists.

    NET-PRESERVATION GATE — the ``--groups`` contract made mechanical.
    Label-on-pin sheets pass by construction; a sheet wired ACROSS group
    boundaries can split/merge nets when bundles separate, and the caller
    refuses that loudly instead of stretching wires silently.
    """
    import os
    import shutil

    from ..netdiff import diff as _net_diff
    from ..netdiff import format_summary as _net_summary
    from ..readers import kicad as kreader
    from ..writers import kicad as kwriter

    before_nets = kreader.read_sch(str(target)).nets
    # next to the target so hierarchical child sheets still resolve
    tmp = target.parent / f".{target.name}.groupsdiff.{os.getpid()}.tmp"
    shutil.copy2(target, tmp)
    try:
        kwriter.apply(_oplist(target, ops), str(tmp), apply=True,
                      sources=sources,
                      verify_out=[], backup_dir=None, allow_open=True)
        after_nets = kreader.read_sch(str(tmp)).nets
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    nd = _net_diff(before_nets, after_nets)
    return NetPreservation(equivalent=nd.equivalent,
                           lines=tuple(_net_summary(nd)))


def refresh_frames(target: Path, *, sources: Any, allow_open: bool,
                   warn: Warn) -> int:
    """Redraw every group frame after a re-layout (``arrange --groups --frames``).

    Keyed frame uuids replace stale borders in place. Applied on top of the
    arrange write with no extra backup rotation, so a single ``akcli undo``
    reverts the arrange together with its frame refresh. Returns the number
    of frames drawn (0 = nothing to frame / refresh refused).
    """
    from ..arrange.frames import plan_frames
    from ..writers import kicad as kwriter

    ops_list = plan_frames(target)
    if not ops_list:
        return 0
    findings: list[Any] = []
    results = kwriter.apply(_oplist(target, ops_list), str(target), apply=True,
                            sources=sources,
                            verify_out=findings, backup_dir=None,
                            allow_open=allow_open)
    if draw_exit(results, findings) != EXIT["OK"]:
        warn("WARNING: frame refresh failed; frames left unchanged")
        return 0
    journal.record(target, "groups-frame", "applied", op_count=len(ops_list))
    return len(ops_list) // 2
