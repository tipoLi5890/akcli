"""Multi-file op-list transaction: one op-list, several sheets, one commit.

The envelope's ``sheets`` map + per-op ``sheet`` tags route ops onto the draw
target's child files. This module executes such an op-list as a TRANSACTION:

* every file is read + snapshotted up front (optimistic lock),
* every op dispatches into its file's own document/ctx (per-op containment,
  file-local uuid namespaces — each doc seeds from its own root uuid),
* every dirty file is verified, then STAGED via :func:`atomic.prepare` (temp
  written + fsync'd + re-verified) BEFORE the first ``os.replace``,
* the pre-write bytes of every dirty file are copied into
  ``.akcli/backups/runs/<run_id>/`` with a ``manifest.json`` (the recovery
  unit — deliberately independent of the journal, which can be disabled),
* the temps are then promoted one ``os.replace`` at a time; a failure rolls
  the already-promoted files back from the run backup (``VERIFY_FAILED``), and
  a rollback failure is loud (``TXN_ROLLBACK_FAILED``) with the full backup
  set intact and ``akcli undo <root> --run <run_id>`` as the remediation.

``os.replace`` cannot be atomic ACROSS files; the guarantee here is that every
file on disk is a complete, verified document at every instant, and that a
partial commit is mechanically restorable.

With ``file_map`` (the shadow-tree dry-apply) the transaction machinery is
skipped: ops apply into the staged copies, verify runs, nothing else happens.

``add_sheet {"create": true}`` lets the SAME transaction bring a child file
into existence: the blank stub is written before any op runs (see
:func:`apply_project`), so the handler, the connectivity gate and the netlist
read-back all see a real file instead of a hole. A stub left behind by a
rolled-back commit is harmless BY DESIGN — it is a valid empty sheet nobody
references (the parent that would have pointed at it was rolled back), and
re-running the op-list adopts it byte-for-byte instead of re-creating it.
Deleting files is not something a rollback does here: the recovery unit
restores CONTENT, and an unlink can never be undone from a backup set.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid as _uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import journal as _journal
from ..errors import AkcliError, fail
from .. import sexpr
from ..report import Severity
from ..safety import MAX_FILE_BYTES, refuse_if_gui_locked
from . import atomic, blank, connectivity, identity, instances
from . import kicad as _kw

__all__ = ["PendingChild", "apply_project", "resolve_sheet_files",
           "sheet_routed"]

MANIFEST_NAME = "manifest.json"

# Paper size of a stub `add_sheet {"create": true}` writes. A child sheet is
# authored by later ops, never by this stub, so the value only has to be the
# same one `akcli new` would have used — the two on-ramps must not disagree.
CREATED_SHEET_PAPER = "A4"


def sheet_routed(oplist: dict) -> bool:
    """Does this op-list need the multi-sheet transaction at all?

    True for the ``sheets`` envelope and for per-op ``sheet`` tags — and for
    an ``add_sheet {"create": true}`` even in a single-file op-list, because
    creating the child is the transaction's job (:func:`apply_project`), not
    the single-file executor's.
    """
    if isinstance(oplist.get("sheets"), dict) and oplist["sheets"]:
        return True
    for op in oplist.get("ops") or ():
        if not isinstance(op, dict):
            continue
        if isinstance(op.get("sheet"), str):
            return True
        if op.get("op") == "add_sheet" and op.get("create"):
            return True
    return False


@dataclass(frozen=True)
class PendingChild:
    """A child ``.kicad_sch`` this transaction will bring into existence."""

    path: Path
    """Resolved absolute path of the child that does not exist yet."""

    parent: Path
    """Resolved absolute path of the file whose ``add_sheet`` asked for it —
    its root uuid seeds the child's own (``identity.seed_sheet_file``)."""

    file_name: str
    """The ``Sheetfile`` string exactly as the op wrote it (the seed input,
    so ``sub/x.kicad_sch`` and ``x.kicad_sch`` stay distinct identities)."""


def resolve_sheet_files(
    oplist: dict, target: Path,
) -> tuple[dict[str, Path], dict[Path, PendingChild]]:
    """``(sheet key -> resolved child path, pending creations)``.

    ``""`` keys the target itself. A declared file must exist on disk
    (``SHEET_UNKNOWN``) UNLESS some ``add_sheet {"create": true}`` in the same
    op-list creates it — tree membership is NOT required either way, because
    the same op-list may legally ``add_sheet`` the reference and then populate
    the child.

    The pending map is keyed by the resolved child path and carries the parent
    the op targets: an ``add_sheet`` routed to sheet key ``k`` names its child
    relative to THAT file's directory (KiCad's own ``Sheetfile`` rule), not to
    the draw target's.
    """
    out: dict[str, Path] = {"": target.resolve()}
    declared: dict[str, str] = {}
    for key, meta in (oplist.get("sheets") or {}).items():
        declared[key] = str(meta.get("file", "") if isinstance(meta, dict)
                            else meta)
        out[key] = (target.parent / declared[key]).resolve()

    pending: dict[Path, PendingChild] = {}
    for op in oplist.get("ops") or ():
        if not isinstance(op, dict) or op.get("op") != "add_sheet":
            continue
        if not op.get("create"):
            continue
        fname = str(op.get("file") or "")
        if not fname:
            continue                          # validate.py owns the refusal
        tag = op.get("sheet")
        parent = out.get(tag, out[""]) if isinstance(tag, str) else out[""]
        child = (parent.parent / fname).resolve()
        if child == parent or child.is_file() or child in pending:
            continue
        pending[child] = PendingChild(path=child, parent=parent,
                                      file_name=fname)

    for key, f in out.items():
        if key == "" or f.is_file() or f in pending:
            continue
        fail("SHEET_UNKNOWN",
             f"sheets[{key!r}]: {declared[key]!r} does not exist next to "
             f"{target.name} — `akcli new` the child first, or set "
             "`create: true` on the add_sheet op that references it")
    return out, pending


@dataclass
class _FileRun:
    real: Path                 # the user-visible file
    path: Path                 # where ops actually land (staged copy or real)
    doc: sexpr.SNode
    original: str
    snap_sha: str
    ctx: "_kw.ExecCtx"
    inst_path: str
    text_out: str = ""
    findings: list = field(default_factory=list)
    prepared: atomic.PreparedWrite | None = None

    @property
    def dirty(self) -> bool:
        return self.text_out != self.original


def _load(real: Path, actual: Path) -> _FileRun:
    data = actual.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"{actual} exceeds {MAX_FILE_BYTES} bytes")
    return _run_of(real, actual, data.decode("utf-8"))


def _run_of(real: Path, actual: Path, text: str) -> _FileRun:
    """Build a run from ``text`` (read from disk, or a not-yet-written stub)."""
    doc = sexpr.parse(text)
    return _FileRun(
        real=real, path=actual, doc=doc, original=text,
        snap_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ctx=_kw._ctx_new(actual),  # noqa: SLF001 - same package
        inst_path=instances.instances_path(doc),
    )


def _creation_order(pending: dict[Path, PendingChild]) -> list[PendingChild]:
    """Pending children, parents before their own children, order-stable.

    A child created by this transaction can itself carry an ``add_sheet``
    (routed onto it) that creates a grandchild, and the grandchild's root uuid
    is seeded from the child's — so the child has to exist first. Marking
    before recursing makes a (nonsensical) cycle terminate instead of
    recursing forever.
    """
    order: list[PendingChild] = []
    seen: set[Path] = set()

    def visit(pc: PendingChild) -> None:
        if pc.path in seen:
            return
        seen.add(pc.path)
        up = pending.get(pc.parent)
        if up is not None:
            visit(up)
        order.append(pc)

    for path in sorted(pending):
        visit(pending[path])
    return order


def _stub_text(pc: PendingChild, by_real: dict[Path, "_FileRun"]) -> str:
    """The blank child document, root uuid seeded from its PARENT's.

    Content-seeded like every other identity akcli mints (SPEC §3.5): plan and
    apply agree on the child's uuid without either of them writing a file
    first, and re-creating a deleted child re-identifies every node inside it.
    """
    run = by_real.get(pc.parent)
    if run is not None:                       # loaded, or created just before
        parent_text: str | None = run.original
    elif pc.parent.is_file():
        parent_text = pc.parent.read_text(encoding="utf-8")
    else:                                     # unreachable in practice
        parent_text = None
    parent_root = (instances.root_uuid(sexpr.parse(parent_text))
                   if parent_text is not None else "")
    return blank.blank_sch(
        CREATED_SHEET_PAPER, None,
        uuid=identity.node_uuid(parent_root,
                                identity.seed_sheet_file(pc.file_name)))


def _pending_missing(finding, run: "_FileRun",
                     pending: dict[Path, PendingChild]) -> bool:
    """Is this ``SHEET_FILE_MISSING`` about a child this transaction creates?

    Only ``draw`` WITHOUT ``--apply`` can produce one: nothing is written, so
    the gate looks for a child that only exists in memory. Reporting it would
    make the plan of a perfectly applicable op-list fail.
    """
    refs = getattr(finding, "refs", None) or ()
    if finding.code != connectivity.SHEET_FILE_MISSING or len(refs) < 2:
        return False
    return (run.path.parent / str(refs[1])).resolve() in pending


def apply_project(
    oplist: dict,
    target: Path | str,
    apply: bool = False,
    *,
    sources: object = None,
    file_map: dict[Path, Path] | None = None,
    tol_nm: int = 0,
    verify_out: list | None = None,
    allow_open: bool = False,
    run_id: str | None = None,
) -> tuple[list, dict]:
    """Apply a sheet-routed op-list across the target + its declared children.

    Returns ``(op_results, report)`` where ``report`` carries ``run_id``,
    ``files`` (per-file {file, dirty, written, created, sha_before, sha_after})
    and ``backup`` (the run-backup label when a transaction committed).
    ``verify_out`` accumulates connectivity findings across every file.

    Children an ``add_sheet {"create": true}`` asks for are materialised HERE,
    before the first file is read, so that every downstream consumer (the
    handler's existence check, the connectivity gate's ``SHEET_FILE_MISSING``,
    the netlist read-back through the root) sees a real document:

    * ``apply`` — the blank stub is written to disk first, and the file joins
      the transaction like any other (backed up, prepared, committed). A stub
      that outlives a refused op-list or a rolled-back commit is harmless by
      design: it is a valid empty sheet nothing references, and re-running the
      op-list adopts it instead of re-creating it (same content-seeded root
      uuid), so the second run is byte-identical to the first.
    * ``apply`` + ``file_map`` (the shadow tree) — the stub is written into
      the STAGED copy, which is scratch space; the real tree is untouched.
    * no ``apply`` (plan) — the stub stays in memory only. Nothing is written,
      and the ``SHEET_FILE_MISSING`` the gate would raise for the parent is
      dropped, because the plan describes a write that would create it.
    """
    _kw.envelope_gate(oplist)
    target = Path(target)
    rid = run_id or str(oplist.get("run_id") or "") or _uuidlib.uuid4().hex[:12]

    sheet_files, pending = resolve_sheet_files(oplist, target)
    scratch = file_map is not None

    if apply and not scratch:
        refuse_if_gui_locked(sorted(set(sheet_files.values())),
                             allow_open=allow_open, what="this multi-sheet draw")

    runs: dict[str, _FileRun] = {}
    by_real: dict[Path, _FileRun] = {}
    for key, real in sheet_files.items():
        if real in pending:
            continue                     # created below, once its parent is read
        actual = (file_map or {}).get(real, real)
        if real in by_real:              # two keys naming one file share a run
            runs[key] = by_real[real]
            continue
        runs[key] = by_real[real] = _load(real, actual)

    # --- create the pending children (parents first) ------------------------ #
    for pc in _creation_order(pending):
        text = _stub_text(pc, by_real)
        # A created child lands NEXT TO the document that references it — in
        # the shadow tree that is the staged parent, so a scratch run can
        # never write into the user's directory.
        parent_run = by_real.get(pc.parent)
        actual = (parent_run.path.parent / pc.file_name
                  if parent_run is not None else pc.path)
        if apply:
            actual.parent.mkdir(parents=True, exist_ok=True)
            # binary, like every other .kicad_sch akcli writes (a text-mode
            # write would translate newlines on Windows)
            actual.write_bytes(text.encode("utf-8"))
        by_real[pc.path] = _run_of(pc.path, actual, text)
    for key, real in sheet_files.items():
        if key not in runs and real in by_real:
            runs[key] = by_real[real]

    src_libs = _kw._coerce_source_list(sources)  # noqa: SLF001

    def resolve(op: dict):
        run = runs[op.get("sheet") or ""]
        return run.doc, run.inst_path, run.ctx

    results, any_error = _kw.run_ops(oplist, src_libs, resolve)

    # --- verify every file (over its resulting bytes) ---------------------- #
    all_findings: list = []
    for run in by_real.values():
        if not any_error:
            connectivity.auto_junctions(run.doc, tol_nm=tol_nm)
        run.text_out = sexpr.dumps(run.doc)
        if any_error:
            continue
        redoc = sexpr.parse(run.text_out)
        run.findings = [f for f in connectivity.verify(redoc, tol_nm=tol_nm,
                                                       path=run.path)
                        if not _pending_missing(f, run, pending)]
        all_findings.extend(run.findings)
    if verify_out is not None:
        verify_out.clear()
        verify_out.extend(all_findings)
    verify_errors = [f for f in all_findings
                     if f.severity in (Severity.ERROR, Severity.CRITICAL)]

    dirty = [r for r in by_real.values() if r.dirty]
    report: dict = {
        "run_id": rid,
        "files": [
            # `created` is the transaction's own doing (add_sheet create:true);
            # `sha_before` is then the blank stub the ops started from, which
            # is also what the run backup restores.
            {"file": r.real.as_posix(), "dirty": r.dirty, "written": False,
             "created": r.real in pending, "sha_before": r.snap_sha}
            for r in by_real.values()
        ],
        "backup": None,
    }

    if not (apply and not any_error and not verify_errors and dirty):
        # A created child that no op touched is still on disk after a real
        # apply — it was written before the ops ran, on purpose.
        for entry in report["files"]:
            entry["written"] = bool(apply) and entry["created"]
        return results, report

    if scratch:
        # Shadow-tree dry-apply: plain writes into the staged copies (they ARE
        # the scratch space; no locks, no backups, no journal).
        for run in dirty:
            run.path.write_bytes(run.text_out.encode("utf-8"))
        for entry in report["files"]:
            entry["written"] = entry["dirty"] or entry["created"]
        return results, report

    # --- two-phase commit --------------------------------------------------- #
    # PREPARE: every dirty file's temp written + fsync'd + re-verified before
    # the first replace; any failure aborts with the disk untouched.
    try:
        for run in dirty:
            run.prepared = atomic.prepare(
                run.path, run.text_out, run.snap_sha,
                verify=_kw._verify_temp)  # noqa: SLF001
    except BaseException:
        for run in dirty:
            if run.prepared is not None:
                atomic.abort(run.prepared)
        raise

    # BACKUP: the recovery unit. Deliberately journal-independent.
    bdir = _journal.ensure_backups_dir(target) / "runs" / rid
    bdir.mkdir(parents=True, exist_ok=True)
    manifest = {"journal_version": _journal.JOURNAL_VERSION, "run_id": rid,
                "target": target.resolve().as_posix(), "files": []}
    for i, run in enumerate(dirty):
        bname = f"{i:03d}-{run.real.name}.bak"
        shutil.copy2(run.path, bdir / bname)
        manifest["files"].append({
            "file": run.real.as_posix(), "backup": bname,
            "sha_before": run.snap_sha,
            "sha_after": hashlib.sha256(
                run.text_out.encode("utf-8")).hexdigest(),
        })
    (bdir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=1), encoding="utf-8", newline="\n")

    # COMMIT: one os.replace per file; a failure rolls the promoted ones back.
    committed: list[_FileRun] = []
    try:
        for run in dirty:
            atomic.commit(run.prepared, backup_dir=None)
            run.prepared = None
            committed.append(run)
    except BaseException as exc:
        for run in dirty:
            if run.prepared is not None:
                atomic.abort(run.prepared)
                run.prepared = None
        try:
            for run in committed:
                entry = next(m for m in manifest["files"]
                             if m["file"] == run.real.as_posix())
                shutil.copy2(bdir / entry["backup"], run.path)
        except BaseException as rb_exc:
            fail("TXN_ROLLBACK_FAILED",
                 f"commit failed ({type(exc).__name__}: {exc}) and rollback "
                 f"also failed ({type(rb_exc).__name__}: {rb_exc}); restore "
                 f"with `akcli undo {target.name} --run {rid} --apply` — the "
                 f"full backup set is under {bdir}")
        if isinstance(exc, AkcliError):
            raise
        fail("VERIFY_FAILED",
             f"multi-file commit failed on {run.real.name} "
             f"({type(exc).__name__}: {exc}); every file was rolled back — "
             "nothing changed")

    for entry in report["files"]:
        entry["written"] = entry["dirty"] or entry["created"]
    for m, run in zip(manifest["files"], dirty):
        for entry in report["files"]:
            if entry["file"] == m["file"]:
                entry["sha_after"] = m["sha_after"]
    report["backup"] = f"runs/{rid}"
    return results, report
