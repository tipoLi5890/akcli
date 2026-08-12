"""``apply`` — the public entry point + the per-op dispatch loop.

Read + optimistic-lock snapshot, parse, run every op through
:data:`~.registry.HANDLERS` with per-op containment, auto-junction, verify
connectivity over the RESULTING bytes, and (only on ``apply=True`` and only
when everything is clean) hand the text to the atomic writer.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b); the dispatch
call is the one line that changed shape, from six positional arguments to
one :class:`~.registry.OpCall`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import sexpr
from ..errors import AkcliError, fail
from ..errors import remediation_for as _remediation_for
from ..ops import PROTOCOL_VERSION, validate_oplist
from ..report import Severity
from ..safety import MAX_FILE_BYTES, gui_lock_path
from . import atomic, connectivity, instances
from . import handlers as _handlers  # noqa: F401 - imports register the ops
from .atomic import backup_name  # noqa: F401 - re-exported by the facade
from .context import _ctx_new
from .registry import HANDLERS, OpCall, _Note

_DEFAULT_BACKUP_DEPTH = atomic.DEFAULT_BACKUP_DEPTH


@dataclass
class OpResult:
    """Per-op result object (SPEC §2.4)."""

    op_index: int
    op: str | None
    status: str = "ok"                      # "ok" | "error"
    created_uuids: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str = ""
    remediation: str | None = None          # actionable next step, machine-readable

    def to_dict(self) -> dict:
        return {
            "op_index": self.op_index,
            "op": self.op,
            "status": self.status,
            "created_uuids": list(self.created_uuids),
            "error_code": self.error_code,
            "message": self.message,
            "remediation": self.remediation,
        }


# Per-op remediation hints live in ``errors.REMEDIATION`` (one registry for
# every surface: per-op results, CLI top level, findings) — imported above.


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
# gui_lock_path is re-exported from safety (its single source of truth) for
# the callers that have always imported it from here.


def apply(
    oplist: dict,
    path: str,
    apply: bool = False,
    *,
    sources: object = None,
    backup_dir: object = None,
    backup_depth: int = _DEFAULT_BACKUP_DEPTH,
    tol_nm: int = 0,
    verify_out: list | None = None,
    allow_open: bool = False,
) -> list[OpResult]:
    """Apply ``oplist`` to the ``.kicad_sch`` at ``path`` (SPEC §3.5).

    Returns one :class:`OpResult` per op. When ``apply`` is ``False`` (the default,
    "--dry-run") nothing is written: the document is edited in memory, connectivity
    is verified, and the results + verify findings (into ``verify_out``) are
    returned. When ``apply`` is ``True`` the edited document is written atomically
    with a backup, but ONLY if every op succeeded and post-write
    :func:`connectivity.verify` reports no errors.

    ``verify_out``, when a list is supplied, is populated with the connectivity
    :class:`~..report.Finding` objects (so the CLI can show them).

    Raises :class:`~..errors.AkcliError` for fatal, document-level problems:
    ``PROTOCOL_MISMATCH`` (op-list major version too new), op-list structural
    errors, a non-``kicad`` ``target_format``, or ``VERIFY_FAILED`` on an
    optimistic-lock conflict / failed post-write verify.
    """
    envelope_gate(oplist)

    # --- read + snapshot (optimistic lock) --------------------------------- #
    p = Path(path)

    # GUI-open guard: KiCad drops ~<name>.lck next to an open document. A write
    # under an open GUI is a losing race — the GUI's later save overwrites it
    # from memory. Refuse by default; --allow-open is explicit risk acceptance.
    if apply and not allow_open and gui_lock_path(p).exists():
        fail("TARGET_LOCKED",
             f"{p.name} appears open in the KiCad GUI (found "
             f"{gui_lock_path(p).name}); close it first — or pass --allow-open "
             "and File>Revert in KiCad afterwards, because a GUI save would "
             "overwrite this edit")

    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"file exceeds {MAX_FILE_BYTES} bytes")
    snap_sha = hashlib.sha256(data).hexdigest()

    doc = sexpr.parse(data.decode("utf-8"))

    src_libs = _coerce_source_list(sources)

    # --- apply ops --------------------------------------------------------- #
    ctx = _ctx_new(p)
    inst_path = instances.instances_path(doc)
    results, any_error = run_ops(
        oplist, src_libs, lambda op: (doc, inst_path, ctx))

    # --- connectivity verify (always; over the resulting bytes) ------------ #
    if not any_error:
        connectivity.auto_junctions(doc, tol_nm=tol_nm)
    text_out = sexpr.dumps(doc)
    redoc = sexpr.parse(text_out)
    findings = ([] if any_error
                else connectivity.verify(redoc, tol_nm=tol_nm, path=p))
    if verify_out is not None:
        verify_out.clear()
        verify_out.extend(findings)

    verify_errors = [
        f for f in findings if f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]

    # --- write (only on --apply AND fully clean) --------------------------- #
    if apply and not any_error and not verify_errors:
        atomic.atomic_write(p, text_out, snap_sha, backup_dir, backup_depth,
                            verify=_verify_temp)

    return results


def _coerce_source_list(sources: object) -> list:
    if sources is None:
        return []
    if isinstance(sources, (list, tuple)):
        return list(sources)
    return [sources]


def envelope_gate(oplist: dict) -> None:
    """Fatal document-level gates shared by the single-file executor and the
    multi-file transaction: protocol major, structural validity, target format.
    """
    pv = oplist.get("protocol_version")
    if isinstance(pv, int) and pv > PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"op-list protocol_version {pv} > supported {PROTOCOL_VERSION}")
    errs = validate_oplist(oplist)
    if errs:
        e = errs[0]
        fail(e.code, e.message)
    if oplist.get("target_format") != "kicad":
        fail("OP_UNSUPPORTED",
             f"kicad writer cannot apply target_format {oplist.get('target_format')!r}")


def run_ops(oplist: dict, src_libs: list, resolve) -> tuple[list["OpResult"], bool]:
    """The per-op dispatch loop with per-op containment (SPEC §2.4).

    ``resolve(op)`` returns ``(doc, instances_path, ctx)`` for the op — a
    constant triple in the single-file executor, a sheet-routed one in the
    multi-file transaction. A handler bug surfaces as ONE failed OpResult,
    never as a traceback aborting the whole run (nothing is written on
    ``any_error``).
    """
    results: list[OpResult] = []
    any_error = False
    for idx, op in enumerate(oplist.get("ops", [])):
        name = op.get("op")
        handler = HANDLERS.get(name)
        res = OpResult(op_index=idx, op=name)
        if handler is None:
            res.status = "error"
            res.error_code = "OP_UNSUPPORTED"
            res.message = f"no kicad writer for op {name!r}"
            res.remediation = _remediation_for("OP_UNSUPPORTED")
            any_error = True
            results.append(res)
            continue
        try:
            doc, inst_path, ctx = resolve(op)
            res.created_uuids = handler(OpCall(
                doc=doc, op=op, index=idx, src_libs=src_libs,
                inst_path=inst_path, ctx=ctx))
        except _Note as note:
            res.message = note.message
        except AkcliError as exc:
            res.status = "error"
            res.error_code = exc.code
            res.message = exc.message
            res.remediation = _remediation_for(exc.code)
            any_error = True
        except Exception as exc:  # noqa: BLE001 — per-op containment (SPEC §2.4)
            res.status = "error"
            res.error_code = "INTERNAL"
            res.message = f"{type(exc).__name__}: {exc}"
            any_error = True
        results.append(res)
    return results, any_error


def _verify_temp(tmp: str) -> None:
    """Re-parse + connectivity-verify a staged temp file (SPEC §3.5)."""
    verify_doc = sexpr.parse(Path(tmp).read_text(encoding="utf-8"))
    bad = [
        f for f in connectivity.verify(verify_doc, path=tmp)
        if f.severity in (Severity.ERROR, Severity.CRITICAL)
    ]
    if bad:
        fail("VERIFY_FAILED",
             f"post-write connectivity verify failed: {bad[0].message}")
