"""PCB <-> footprint-library leg of ``akcli verify <sch> <board> --library``.

A board carries its own COPY of every footprint. Nothing ties that copy to
the library it came from: a pad deleted, renumbered or re-drilled on the
board survives every netlist check, and a footprint later fixed in the
library does not reach a board that was never updated. This check compares
each board footprint against the definition its library id names — for the
libraries the PROJECT ``fp-lib-table`` registers (the ones the project owns
and ships; :func:`akcli.libtable.load_libraries`).

Finding codes (all ``refs=[designator]``):

* ``FPLIB_PAD_SET_DRIFT``   — pad numbers / pad count differ from the
  library definition (WARNING);
* ``FPLIB_PAD_DRILL_DRIFT`` — a pad's type or drill differs (WARNING);
* ``FPLIB_SOURCE_MISSING``  — the nickname is registered in the project
  table, but the library does not hold that footprint (WARNING);
* ``FPLIB_MODEL_MISSING``   — the board footprint has no 3D model, or a model
  path that does not resolve (NOTE).

The drift findings are WARNINGs, not errors: a deliberate local board edit is
legitimate, and the messages say so — waive the designator when it is.

**Coverage is reported, never implied.** Only footprints whose library the
project table registers can be compared; the rest (KiCad's own libraries in
the per-user global table, unregistered nicknames, footprints with no library
id, project libraries that are not ``.pretty`` directories) are COUNTED in
:func:`run`'s coverage record so a clean result cannot read as "every
footprint was checked".
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .. import libtable
from ..model import FootprintPad, Pcb
from ..report import Finding, Severity, anchor

__all__ = ["NOT_COMPARED_REASONS", "coverage_text", "run"]

# reason key -> how the text report words it (order = report order)
NOT_COMPARED_REASONS: dict[str, str] = {
    "global_library": "library only in the global fp-lib-table",
    "unregistered_library": "library nickname in no fp-lib-table",
    "no_library_id": "no library id on the footprint",
    "library_unavailable": "project library did not resolve",
    "unsupported_library": "project library is not a .pretty directory",
    "not_in_library": "footprint not in its project library",
    "ambiguous_designator": "no designator, or one shared by several "
                            "footprints",
}

_LIMIT = 8          # max pad numbers / pad diffs listed per message
_LOCAL_EDIT = ("a deliberate local board edit is legitimate — waive this "
               "designator if so, otherwise update the footprint from the "
               "library")


def _drill(value: object) -> float | None:
    """A pad's drill as one number, whatever shape the reader gave it."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return next((d for d in (_drill(v) for v in value) if d is not None),
                    None)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _sig(pad_type: str, drill: float | None) -> tuple[str, float]:
    """Comparable ``(type, drill)``; no drill sorts as ``-1``."""
    return pad_type, (-1.0 if drill is None else round(drill, 4))


def _fmt_sig(sigs: list[tuple[str, float]]) -> str:
    return "/".join(
        t + ("" if d < 0 else f" drill {d:g}") for t, d in sigs)


def _listed(numbers: list[str]) -> str:
    shown = ", ".join(n or "(unnumbered)" for n in numbers[:_LIMIT])
    if len(numbers) > _LIMIT:
        shown += f", … (+{len(numbers) - _LIMIT})"
    return shown


def _pad_findings(ref: str, lib_id: str, board_pads: list[dict[str, Any]],
                  lib_pads: list[FootprintPad]) -> list[Finding]:
    out: list[Finding] = []
    anchors = [anchor("component", ref)]
    b_nums = Counter(str(p.get("number") or "") for p in board_pads)
    l_nums = Counter(p.number for p in lib_pads)
    if b_nums != l_nums:
        only_board = sorted((b_nums - l_nums).elements())
        only_lib = sorted((l_nums - b_nums).elements())
        detail = [f"board has {len(board_pads)} pad(s), library has "
                  f"{len(lib_pads)}"]
        if only_board:
            detail.append(f"only on the board: {_listed(only_board)}")
        if only_lib:
            detail.append(f"only in the library: {_listed(only_lib)}")
        out.append(Finding(
            "FPLIB_PAD_SET_DRIFT", Severity.WARNING,
            f"{ref}: pad set differs from {lib_id} — {'; '.join(detail)} "
            f"({_LOCAL_EDIT})", refs=[ref], anchors=anchors))

    b_sigs: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for p in board_pads:
        b_sigs[str(p.get("number") or "")].append(
            _sig(str(p.get("pad_type") or "smd"), _drill(p.get("drill"))))
    l_sigs: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for lp in lib_pads:
        l_sigs[lp.number].append(_sig(lp.pad_type, _drill(lp.drill_mm)))
    diffs: list[str] = []
    for num in sorted(set(b_sigs) & set(l_sigs)):
        if b_nums[num] != l_nums[num]:
            continue                 # the pad-set finding already covers it
        board, lib = sorted(b_sigs[num]), sorted(l_sigs[num])
        if board != lib:
            diffs.append(f"pad {num or '(unnumbered)'}: board "
                         f"{_fmt_sig(board)}, library {_fmt_sig(lib)}")
    if diffs:
        shown = "; ".join(diffs[:_LIMIT])
        if len(diffs) > _LIMIT:
            shown += f"; … (+{len(diffs) - _LIMIT})"
        out.append(Finding(
            "FPLIB_PAD_DRILL_DRIFT", Severity.WARNING,
            f"{ref}: pad type/drill differs from {lib_id} — {shown} "
            f"({_LOCAL_EDIT})", refs=[ref], anchors=anchors))
    return out


def _model_finding(ref: str, models: list[str], ws: libtable.Workspace,
                   lib: libtable.LoadedLibrary | None) -> Finding | None:
    """``FPLIB_MODEL_MISSING`` for a board footprint, or ``None``.

    A relative path is tried against the project directory and the library
    directory. A model referenced through a KiCad built-in
    ``${KICAD<N>_3DMODEL_DIR}`` variable is KiCad's own stock model: whether
    it is installed is a property of the machine, not of the board, so it
    counts as present.
    """
    anchors = [anchor("component", ref)]
    if not models:
        return Finding(
            "FPLIB_MODEL_MISSING", Severity.NOTE,
            f"{ref}: the board footprint has no 3D model (it will be absent "
            "from 3D/STEP exports)", refs=[ref], anchors=anchors)
    bases = [ws.project_dir]
    if lib is not None and lib.loaded:
        bases.append(lib.path)
    broken: list[str] = []
    for mpath in models:
        if libtable.is_kicad_builtin_ref(mpath):
            continue
        found = unresolvable = False
        for base in bases:
            resolved, _portable = libtable.resolve_model(
                mpath, base, ws.project_dir)
            if resolved is None:
                unresolvable = True
                break
            if resolved.is_file():
                found = True
                break
        if not found:
            broken.append(f"{mpath!r} ("
                          + ("unresolvable path variable" if unresolvable
                             else "file not found") + ")")
    if not broken:
        return None
    return Finding(
        "FPLIB_MODEL_MISSING", Severity.NOTE,
        f"{ref}: 3D model path does not resolve — {'; '.join(broken)}",
        refs=[ref], anchors=anchors)


def run(pcb: Pcb, ws: libtable.Workspace) -> tuple[list[Finding], dict[str, Any]]:
    """Compare ``pcb``'s footprints against the project footprint libraries.

    Returns ``(findings, coverage)``. ``coverage`` is ``{footprints, compared,
    not_compared, reasons}`` — ``reasons`` counts every key of
    :data:`NOT_COMPARED_REASONS` (zeros included, so the shape is stable) and
    sums to ``not_compared``.
    """
    findings: list[Finding] = []
    reasons = dict.fromkeys(NOT_COMPARED_REASONS, 0)
    libs = {lib.entry.name: lib
            for lib in libtable.load_libraries(ws, kinds=("fp",))}
    seen = Counter(f.designator for f in pcb.footprints)
    pads_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pad in pcb.pads:
        pads_of[str(pad.get("component") or "")].append(pad)

    compared = 0
    for fp in pcb.footprints:
        lib_id = fp.footprint_name or ""
        nick, _, name = lib_id.partition(":")
        if not nick or not name:
            reasons["no_library_id"] += 1
            continue
        where = ws.locate_fp_nick(nick)
        if where != "project":
            reasons["global_library" if where == "global"
                    else "unregistered_library"] += 1
            continue
        ref = fp.designator or lib_id
        lib = libs.get(nick)
        model = _model_finding(ref, list(fp.models), ws, lib)
        if model is not None:
            findings.append(model)
        if lib is None or not lib.loaded:
            reasons["library_unavailable"] += 1      # `library audit` reports why
            continue
        if not lib.path.is_dir():
            # a single-file library of another plugin type (Altium .PcbLib,
            # Eagle .lbr, legacy .mod): KiCad reads it, this check does not —
            # "not compared", never "footprint missing"
            reasons["unsupported_library"] += 1
            continue
        loaded = next((lf for lf in lib.footprints
                       if lf.definition.name == name), None)
        if loaded is None:
            reasons["not_in_library"] += 1
            findings.append(Finding(
                "FPLIB_SOURCE_MISSING", Severity.WARNING,
                f"{ref}: board footprint {lib_id} — library '{nick}' is "
                f"registered in the project fp-lib-table but holds no "
                f"footprint '{name}' (renamed or deleted in the library; the "
                "board keeps its own copy, so nothing fails until it is "
                "updated from the library)",
                refs=[ref], anchors=[anchor("component", ref)]))
            continue
        if not fp.designator or seen[fp.designator] > 1:
            reasons["ambiguous_designator"] += 1     # pads cannot be attributed
            continue
        compared += 1
        findings.extend(_pad_findings(
            ref, lib_id, pads_of[fp.designator], loaded.definition.pads))

    total = len(pcb.footprints)
    return findings, {
        "footprints": total,
        "compared": compared,
        "not_compared": total - compared,
        "reasons": reasons,
    }


def coverage_text(coverage: dict[str, Any]) -> str:
    """One line stating what was — and was NOT — compared."""
    text = (f"{coverage['compared']} of {coverage['footprints']} board "
            "footprint(s) compared against the project fp-lib-table")
    skipped = [f"{count} {NOT_COMPARED_REASONS[key]}"
               for key, count in coverage["reasons"].items() if count]
    if skipped:
        text += (f"; {coverage['not_compared']} NOT compared "
                 f"({', '.join(skipped)})")
    return text
