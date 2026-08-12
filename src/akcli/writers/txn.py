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
from . import atomic, connectivity, instances
from . import kicad as _kw

__all__ = ["apply_project", "resolve_sheet_files", "sheet_routed"]

MANIFEST_NAME = "manifest.json"


def sheet_routed(oplist: dict) -> bool:
    """Does this op-list use the multi-sheet envelope at all?"""
    if isinstance(oplist.get("sheets"), dict) and oplist["sheets"]:
        return True
    return any(isinstance(op, dict) and isinstance(op.get("sheet"), str)
               for op in oplist.get("ops") or ())


def resolve_sheet_files(oplist: dict, target: Path) -> dict[str, Path]:
    """``sheet key -> resolved child path`` (``""`` is the target itself).

    Every declared file must exist on disk (``SHEET_UNKNOWN``) — tree
    membership is NOT required, because the same op-list may legally
    ``add_sheet`` the reference and then populate the child.
    """
    out: dict[str, Path] = {"": target.resolve()}
    for key, meta in (oplist.get("sheets") or {}).items():
        f = (target.parent / str(meta.get("file", ""))).resolve()
        if not f.is_file():
            fail("SHEET_UNKNOWN",
                 f"sheets[{key!r}]: {meta.get('file')!r} does not exist next "
                 f"to {target.name} — `akcli new` the child first")
        out[key] = f
    return out


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
    text = data.decode("utf-8")
    doc = sexpr.parse(text)
    return _FileRun(
        real=real, path=actual, doc=doc, original=text,
        snap_sha=hashlib.sha256(data).hexdigest(),
        ctx=_kw._ctx_new(actual),  # noqa: SLF001 - same package
        inst_path=instances.instances_path(doc),
    )


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
    ``files`` (per-file {file, dirty, written, sha_before, sha_after}) and
    ``backup`` (the run-backup label when a transaction committed).
    ``verify_out`` accumulates connectivity findings across every file.
    """
    _kw.envelope_gate(oplist)
    target = Path(target)
    rid = run_id or str(oplist.get("run_id") or "") or _uuidlib.uuid4().hex[:12]

    sheet_files = resolve_sheet_files(oplist, target)
    scratch = file_map is not None

    if apply and not scratch:
        refuse_if_gui_locked(sorted(set(sheet_files.values())),
                             allow_open=allow_open, what="this multi-sheet draw")

    runs: dict[str, _FileRun] = {}
    by_real: dict[Path, _FileRun] = {}
    for key, real in sheet_files.items():
        actual = (file_map or {}).get(real, real)
        if real in by_real:              # two keys naming one file share a run
            runs[key] = by_real[real]
            continue
        runs[key] = by_real[real] = _load(real, actual)

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
        run.findings = connectivity.verify(redoc, tol_nm=tol_nm,
                                           path=run.path)
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
            {"file": r.real.as_posix(), "dirty": r.dirty, "written": False,
             "sha_before": r.snap_sha}
            for r in by_real.values()
        ],
        "backup": None,
    }

    if not (apply and not any_error and not verify_errors and dirty):
        return results, report

    if scratch:
        # Shadow-tree dry-apply: plain writes into the staged copies (they ARE
        # the scratch space; no locks, no backups, no journal).
        for run in dirty:
            run.path.write_bytes(run.text_out.encode("utf-8"))
        for entry in report["files"]:
            entry["written"] = entry["dirty"]
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
        entry["written"] = entry["dirty"]
    for m, run in zip(manifest["files"], dirty):
        for entry in report["files"]:
            if entry["file"] == m["file"]:
                entry["sha_after"] = m["sha_after"]
    report["backup"] = f"runs/{rid}"
    return results, report
