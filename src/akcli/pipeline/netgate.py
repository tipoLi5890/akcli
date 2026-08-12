"""The connectivity gate: dry-apply an op-list to a COPY and diff the netlists.

Nothing is written to the target here. The op-list is applied to a staged
copy — one temp file next to the target for a flat sheet, the whole sheet tree
under ``.akcli/stage/<pid>/`` when the net view spans several files — and the
before/after netlists are read back THROUGH the tree root, so the answer is
about the design's connectivity rather than one truncated file's. The same
dry-apply feeds ``--render`` (one apply, two consumers).

Windows note: every path/copy/unlink call below is load-bearing and is kept
verbatim from the historical implementation (the copy lives NEXT TO the
target so relative child-sheet references still resolve; the staging root is
removed with ``ignore_errors``). Change the shape of this file's I/O only with
the full CI matrix in hand.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import EXIT, AkcliError
from . import draw_exit
from .preview import Warn, render_preview

__all__ = ["Log", "NetGate", "Warn", "evaluate"]

# Verbosity-gated diagnostic sink: (level, message-without-newline).
Log = Callable[[int, str], None]


@dataclass(frozen=True)
class NetGate:
    """What the staged dry-apply learned; ``net_lines`` is ``[]`` iff equivalent."""

    tree_root: Path | None = None
    net_lines: list[str] | None = None
    net_risk: bool = False
    net_equiv: bool = True
    net_diff_err: str | None = None
    preview: dict[str, Any] | None = field(default=None)


def evaluate(*, target: Path, oplist: dict[str, Any], sources: Any,
             routed: bool, want_diff: bool, render_out: str | None,
             explicit_root: Path | None, flat: bool, strict: bool,
             warn: Warn, log: Log) -> NetGate:
    """Stage, dry-apply, diff. Never touches ``target``; never decides an exit.

    ``strict`` only makes the gate fail CLOSED while resolving the sheet tree
    (a scope we cannot resolve is not a scope we may silently skip); the
    strict REFUSAL itself is the caller's decision, taken from the result.
    """
    from ..writers import kicad as kwriter
    from ..writers import txn as _txn

    net_lines: list[str] | None = None
    net_risk = False
    net_equiv = True
    net_diff_err: str | None = None
    preview: dict[str, Any] | None = None

    # Hierarchy scope: a child sheet read standalone is a TRUNCATED graph (its
    # global labels lose the members other sheets contribute), so evaluating
    # the gate there would grade the wrong design. Resolve the tree's root
    # first — the flat case (no parent references the target) short-circuits
    # to exactly the historical single-file path.
    tree_root: Path | None = None
    if (want_diff or render_out) and not flat:
        from .. import project as _project
        try:
            resolved = _project.resolve_root(target, explicit=explicit_root)
            if resolved != target.resolve():
                tree_root = resolved
        except AkcliError as e:
            if e.code == "PROJECT_ROOT_MISMATCH" or strict:
                raise          # a wrong --root is a usage error; strict = closed
            # Advisory mode degrades HONESTLY: no diff at all beats a
            # truncated one silently presented as the design's net effect.
            net_diff_err = str(e)
            want_diff = False

    if want_diff or render_out:
        from ..netdiff import diff as net_diff
        from ..netdiff import format_summary as net_summary
        from ..netdiff import has_risk as net_has_risk
        from ..readers import kicad as kreader
        dry_ok = False
        had_exc = False
        stage_base = None
        if tree_root is not None or routed:
            # Tree- (or transaction-) scoped target: stage the WHOLE tree
            # (relative layout preserved) so the dry-applied files are read
            # back THROUGH the root — before/after nets both come from the
            # full graph. A routed op-list stages even when the target IS the
            # root: several staged files change, not just one.
            from .. import journal as _journal
            scope_root = tree_root if tree_root is not None else target.resolve()
            stage_base = (_journal.ensure_workspace_dir(scope_root) / "stage"
                          / f"{os.getpid()}")
            tmp = None
        else:
            # the copy lives NEXT TO the target (never a TemporaryDirectory): a
            # hierarchical root must still resolve its child sheets on read-back
            tmp = target.parent / f".{target.name}.netdiff.{os.getpid()}.tmp"
        try:
            if stage_base is not None:
                from .. import project as _project
                before_nets = (kreader.read_sch(str(scope_root)).nets
                               if want_diff else None)
                stage_files = list(_project.tree_files(scope_root))
                if routed:
                    for f in _txn.resolve_sheet_files(oplist, target).values():
                        if f not in stage_files:
                            stage_files.append(f)
                mapping = _project.stage_tree(stage_files, stage_base)
                dry_target = mapping[target.resolve()]
                staged_root = mapping[scope_root]
                if tree_root is not None:
                    log(1, f"net gate scope: sheet tree rooted at "
                           f"{tree_root.name}")
            elif tmp is not None:            # exactly one of the two is set
                before_nets = (kreader.read_sch(str(target)).nets
                               if want_diff else None)
                shutil.copy2(target, tmp)
                dry_target = tmp
                staged_root = tmp
            tmp_findings: list[Any] = []
            if routed:
                tmp_results, _rep = _txn.apply_project(
                    oplist, dry_target, apply=True, sources=sources,
                    file_map={}, verify_out=tmp_findings)
            else:
                tmp_results = kwriter.apply(oplist, str(dry_target), apply=True,
                                            sources=sources,
                                            verify_out=tmp_findings,
                                            backup_dir=None)
            dry_ok = draw_exit(tmp_results, tmp_findings) == EXIT["OK"]
            if want_diff and dry_ok and before_nets is not None:
                after_nets = kreader.read_sch(str(staged_root)).nets
                nd = net_diff(before_nets, after_nets)
                net_lines = net_summary(nd)      # [] iff nd.equivalent
                net_risk = net_has_risk(nd)
                net_equiv = nd.equivalent
            # else: the op-list itself fails — the per-op results below explain
            # it, and the real apply refuses on its own (nothing is written),
            # so a "(none)" net diff here would be misleading
        except Exception as e:                   # noqa: BLE001
            had_exc = True
            if want_diff:
                net_diff_err = f"{type(e).__name__}: {e}"
            else:                                # preview-only dry-apply failed
                warn("WARNING: preview render unavailable: "
                     f"{type(e).__name__}: {e}")
        finally:
            # preview renders INSIDE the temp's lifetime (only OUT.svg persists);
            # a refused op-list leaves the temp pristine, so rendering it would
            # show the before-state as if it were the plan — skip it honestly.
            if render_out and dry_ok:
                preview = render_preview(dry_target, render_out, warn)
            elif render_out and not had_exc:
                warn("WARNING: preview skipped (op-list did not dry-apply "
                     "cleanly; fix the errors below)")
            if stage_base is not None:
                shutil.rmtree(stage_base, ignore_errors=True)
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    return NetGate(tree_root=tree_root, net_lines=net_lines, net_risk=net_risk,
                   net_equiv=net_equiv, net_diff_err=net_diff_err,
                   preview=preview)
