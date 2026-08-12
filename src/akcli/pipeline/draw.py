"""``plan`` / ``draw`` orchestration: gate first, then (maybe) write.

One driver for both verbs — ``plan`` is ``draw`` with ``apply=False``. The
sequence is fixed and is the safety contract:

1. stage a dry-apply and diff the netlists (:mod:`.netgate`), so the net
   effect is known BEFORE the target is touched (and ``--render`` gets its
   preview from that same staged copy);
2. honour ``--strict-nets``: an unavailable diff or a risky split/merge is a
   refusal, reported as data — the caller owns the exit code;
3. apply for real, routed through the multi-sheet transaction when the
   op-list addresses several files;
4. report: per-op results, connectivity findings, the net diff, and the
   advisory post-apply ERC.

The command layer adds the ``--json`` envelope, the status line and the
journal entry; nothing in this module prints or knows what a CLI is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import EXIT, AkcliError
from . import draw_exit
from .netgate import Log, NetGate, Warn, evaluate

__all__ = ["DrawOutcome", "DrawRequest", "Refusal", "draw_exit", "run"]


@dataclass(frozen=True)
class DrawRequest:
    """Everything the driver needs, already resolved by the caller.

    ``sources`` is the writer's symbol-source list (``--symbols`` + config +
    lib-table fallback), which only the command layer can assemble; the flags
    mirror the CLI's, but as values rather than a ``Namespace``.
    """

    target: Path
    oplist: dict[str, Any]
    sources: Any
    warn: Warn                  # stderr sink: one line, no trailing newline
    log: Log                    # verbosity-gated sink: (level, line)
    do_apply: bool = False
    backup_depth: int = 3
    allow_open: bool = False
    strict_nets: bool = False
    no_net_diff: bool = False
    render: str | None = None
    root: Path | None = None
    flat: bool = False
    no_erc: bool = False


@dataclass(frozen=True)
class Refusal:
    """A write the engine will not perform, described for the caller to render.

    ``lines`` are the diagnostic lines that precede ``message`` on stderr (the
    net-diff summary of what would have changed).
    """

    exit_code: int
    message: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrawOutcome:
    """Per-op results + the gate's findings; ``refusal`` short-circuits the rest."""

    exit_code: int = EXIT["OK"]
    results: list[Any] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)
    gate: NetGate = field(default_factory=NetGate)
    txn_report: dict[str, Any] | None = None
    routed: bool = False
    refusal: Refusal | None = None


def run(req: DrawRequest) -> DrawOutcome:
    """Validate-by-dry-apply, gate on connectivity, then write when asked."""
    from ..writers import kicad as kwriter
    from ..writers import txn as _txn
    from ..writers.backups import backups_dir

    # Multi-sheet routing: an op-list using the envelope's `sheets` map (or
    # per-op `sheet` tags) edits several files in ONE transaction.
    routed = _txn.sheet_routed(req.oplist)

    strict = req.do_apply and req.strict_nets
    # Connectivity diff (advisory unless --strict-nets): dry-apply the op-list
    # to a temp copy and compare netlists, so both dry-run and apply report the
    # net effect BEFORE the target is touched. When the diff cannot be computed
    # at all, --strict-nets fails CLOSED (a silently skipped gate is no gate).
    # The SAME temp dry-apply also feeds the --render preview (one apply, two
    # consumers) — with --no-net-diff the preview still gets its own dry-apply.
    want_diff = not req.no_net_diff or strict

    gate = evaluate(target=req.target, oplist=req.oplist, sources=req.sources,
                    routed=routed, want_diff=want_diff, render_out=req.render,
                    explicit_root=req.root, flat=req.flat, strict=strict,
                    warn=req.warn, log=req.log)

    if gate.net_diff_err is not None:
        if strict:
            return DrawOutcome(exit_code=EXIT["OPLIST"], gate=gate, routed=routed,
                               refusal=Refusal(
                                   EXIT["OPLIST"],
                                   "REFUSED: --strict-nets: net diff unavailable "
                                   f"({gate.net_diff_err}); nothing written"))
        req.warn(f"WARNING: net diff unavailable: {gate.net_diff_err}")

    if strict and gate.net_risk:
        return DrawOutcome(exit_code=EXIT["OPLIST"], gate=gate, routed=routed,
                           refusal=Refusal(
                               EXIT["OPLIST"],
                               "REFUSED: --strict-nets: net split/merge touches a "
                               "named net; nothing written",
                               lines=tuple(gate.net_lines or ())))

    findings: list[Any] = []
    txn_report = None
    if routed:
        results, txn_report = _txn.apply_project(
            req.oplist, req.target, apply=req.do_apply, sources=req.sources,
            verify_out=findings,
            allow_open=req.allow_open,
        )
    else:
        results = kwriter.apply(
            req.oplist, str(req.target), apply=req.do_apply, sources=req.sources,
            verify_out=findings,
            # write a rotated <name>.bak under .akcli/backups/ on apply (the
            # atomic write already guarantees the original is never corrupted;
            # this is extra safety and the stack `akcli undo` walks). Depth
            # from [project] backup_depth.
            backup_dir=(backups_dir(req.target) if req.do_apply else None),
            backup_depth=req.backup_depth,
            allow_open=req.allow_open,
        )

    if req.do_apply and draw_exit(results, findings) == EXIT["OK"]:
        req.log(1, f"wrote {req.target}")
        if kwriter.gui_lock_path(req.target).exists():
            req.warn(
                "WARNING: this file is open in the KiCad GUI — use File>Revert "
                "there NOW; a GUI save would overwrite this edit from memory")
        _advisory_erc(req)

    return DrawOutcome(exit_code=draw_exit(results, findings), results=results,
                       findings=findings, gate=gate, txn_report=txn_report,
                       routed=routed)


def _advisory_erc(req: DrawRequest) -> None:
    """Post-apply ERC via an installed ``kicad-cli`` — advisory, never fatal.

    A broken integration is surfaced on stderr rather than swallowed;
    ``--no-erc`` skips it honestly instead of leaving the agent to wonder
    whether the external run happened.
    """
    try:
        from ..drivers import kicad_cli
        if req.no_erc:
            req.log(1, "advisory kicad-cli ERC skipped (--no-erc)")
        elif kicad_cli.available():
            rep = kicad_cli.erc(str(req.target))
            if rep is not None:
                req.log(1, f"kicad-cli erc: exit {rep.get('exit_code')}")
    except AkcliError as exc:  # pragma: no cover - advisory only
        req.warn(f"note: advisory kicad-cli ERC skipped: {exc}")
    except OSError as exc:  # pragma: no cover - advisory only
        req.warn(f"note: advisory kicad-cli ERC failed: {exc}")
