"""`akcli library` — project library workspace: audit, repair, author, lint.

``audit`` cross-checks schematics <-> sym/fp-lib-table <-> library contents <->
3D models and reports lint-style findings (exit 1 on >= WARNING). ``repair``
productizes the two historically hand-``sed``-ed fixes — footprint-nickname
renames and 3D-model path policy — as a dry-run plan; ``--apply`` writes
atomically with a ``.bak`` next to each file, and refuses ``TARGET_LOCKED``
(the shared :func:`akcli.safety.refuse_if_gui_locked` gate) if KiCad holds any
target open — the same invariant as ``draw --apply``.

``new-symbol`` turns a declarative pin table into a ``.kicad_sym``: the caller
supplies the pin *table* (which a datasheet gives up reliably) and akcli
computes the *coordinates* (which is where hand-written symbols acquire two
pins on one point — a mis-join KiCad cannot report, because the symbol is what
told ERC those pins are one node). ``lint-symbols`` runs the same check over a
library akcli did not write.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..errors import EXIT
from ._shared import (
    _add_exit_policy_flags,
    _dumps,
    _emit,
    _ExitWith,
    _findings_exit,
    _load_cfg,
    _require_path,
    _stamp,
)


def _workspace(args: argparse.Namespace):
    from .. import libtable
    project = Path(getattr(args, "project", None) or ".")
    if not project.exists():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: no such project: {project}")
    return libtable.discover(project)


def _cmd_library_audit(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    sch_override = [Path(s) for s in (getattr(args, "sch", None) or [])] or None
    from .. import libtable
    findings = libtable.audit(ws, sch_override)

    if args.json:
        _emit(_dumps(_stamp({
            "project": str(ws.project_dir),
            "sym_lib_table": str(ws.sym_table.path) if ws.sym_table else None,
            "fp_lib_table": str(ws.fp_table.path) if ws.fp_table else None,
            "schematics": [str(s) for s in (sch_override or ws.schematics)],
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message, "anchors": f.anchors}
                for f in findings
            ],
        })))
        return _findings_exit(findings, args)

    _emit(f"library audit: {ws.project_dir}")
    _emit(f"  sym-lib-table: {ws.sym_table.path.name if ws.sym_table else '(none)'}"
          f"{'  (' + str(len(ws.sym_table.entries)) + ' libs)' if ws.sym_table else ''}")
    _emit(f"  fp-lib-table:  {ws.fp_table.path.name if ws.fp_table else '(none)'}"
          f"{'  (' + str(len(ws.fp_table.entries)) + ' libs)' if ws.fp_table else ''}")
    _emit(f"  schematics:    {len(sch_override or ws.schematics)}")
    if not findings:
        _emit("no findings — nicknames, footprints and 3D references all resolve")
        return EXIT["OK"]
    for f in findings:
        _emit(f"{f.severity.value.upper():<8} {f.code}: {f.message}")
    _emit(f"{len(findings)} finding(s)")
    return _findings_exit(findings, args)


def _cmd_library_repair(args: argparse.Namespace) -> int:
    from .. import libtable
    ws = _workspace(args)
    edits: list = []
    for spec in getattr(args, "rename_footprint_lib", None) or []:
        if "=" not in spec:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: --rename-footprint-lib wants OLD=NEW, got {spec!r}")
        old, new = spec.split("=", 1)
        edits.extend(libtable.plan_rename(ws, old, new))
    mode = getattr(args, "model_path", None)
    if mode:
        try:
            edits.extend(libtable.plan_model_paths(ws, mode))
        except ValueError as exc:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: {exc}")
    if not getattr(args, "rename_footprint_lib", None) and not mode:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: nothing to repair — pass --rename-footprint-lib "
                        "OLD=NEW and/or --3d-path MODE")

    do_apply = bool(getattr(args, "apply", False))
    if do_apply and edits:
        # Same invariant as `draw --apply` / `relink --apply`: never write a
        # file KiCad holds open (a GUI save would overwrite it from memory).
        # Checked before any output so a refusal writes nothing at all.
        from .. import safety
        safety.refuse_if_gui_locked([e.path for e in edits],
                                    what="the repair write")
    if args.json:
        _emit(_dumps(_stamp({
            "project": str(ws.project_dir),
            "applied": do_apply and bool(edits),
            "edits": [{"path": str(e.path), "description": e.description}
                      for e in edits],
        })))
    else:
        if not edits:
            _emit("repair: nothing to change")
            return EXIT["OK"]
        for e in edits:
            _emit(f"  {e.description}")
        if not do_apply:
            _emit(f"dry-run: {len(edits)} file(s) would change — re-run with --apply")

    if do_apply and edits:
        from .. import safety
        for e in edits:
            safety.atomic_write_with_backup(e.path, e.new_text,
                                            backup_dir=e.path.parent)
        if not args.json:
            _emit(f"applied {len(edits)} file(s) (each left a .bak) — "
                  "re-run `akcli library audit` to confirm")
        # post-apply re-audit so a bad repair cannot claim success silently
        findings = libtable.audit(ws)
        bad = [f for f in findings if f.severity.value in ("error", "critical")]
        if bad:
            sys.stderr.write(
                f"WARNING: post-repair audit still has {len(bad)} error(s); "
                "run `akcli library audit` for details\n")
    return EXIT["OK"]


def _sanitize_fp_name(name: str) -> str:
    """Make a footprint name filesystem/library-safe (declared, reported)."""
    out = "".join(("-" if c in '()[]{}<>:;"/\\|?*' or c.isspace() else c)
                  for c in name).strip("-.")
    return out or "footprint"


def _cmd_library_import_altium(args: argparse.Namespace) -> int:
    """`library import-altium part.PcbLib --out lib.pretty` — PcbLib -> .kicad_mod."""
    import hashlib

    from .. import __version__, safety
    from ..readers import footprint_lib
    from ..writers import footprint_mod

    src = Path(getattr(args, "path", None) or "")
    if not str(src):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing input .PcbLib")
    lib = footprint_lib.read_pcblib(src)

    out_arg = getattr(args, "out", None)
    if out_arg:
        out_dir = Path(out_arg)
    else:
        parts_root = _load_cfg(args, None).paths.get("parts_dir")
        out_dir = (Path(parts_root) / (src.stem + ".pretty") if parts_root
                   else Path(src.stem + ".pretty"))
    do_apply = bool(getattr(args, "apply", False))
    courtyard = getattr(args, "courtyard", None)

    plan: list[dict] = []
    all_warnings: list[str] = list(lib.warnings)
    for fp in lib.footprints:
        safe = _sanitize_fp_name(fp.name)
        if safe != fp.name:
            all_warnings.append(
                f"{fp.name}: renamed to {safe!r} (filesystem-safe) — declared "
                "transformation")
        conv_warnings: list[str] = []
        fp_render = fp
        if safe != fp.name:
            import dataclasses
            fp_render = dataclasses.replace(fp, name=safe)
        text = footprint_mod.to_kicad_mod(
            fp_render, courtyard_mm=courtyard, warnings=conv_warnings)
        all_warnings.extend(conv_warnings)
        plan.append({"name": safe, "source_name": fp.name,
                     "pads": len(fp.pads),
                     "file": str(out_dir / f"{safe}.kicad_mod"),
                     "text": text})

    if do_apply:
        out_dir.mkdir(parents=True, exist_ok=True)
        for item in plan:
            safety.atomic_write_with_backup(item["file"], item["text"])
        provenance = {
            "schema_version": "1",
            "source": {"path": str(src),
                       "sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
            "converter": {"tool": "akcli library import-altium",
                          "version": __version__},
            "options": {"courtyard_mm": courtyard},
            "footprints": [{k: item[k] for k in ("name", "source_name", "pads", "file")}
                           for item in plan],
            "warnings": all_warnings,
        }
        safety.atomic_write_with_backup(
            out_dir / "provenance.json", _dumps(provenance) + "\n")

    if args.json:
        _emit(_dumps(_stamp({
            "source": str(src),
            "out": str(out_dir),
            "applied": do_apply,
            "footprints": [{k: item[k] for k in ("name", "source_name", "pads", "file")}
                           for item in plan],
            "warnings": all_warnings,
        })))
    else:
        _emit(f"import-altium: {src.name} -> {out_dir} "
              f"({len(plan)} footprint(s))")
        for item in plan:
            _emit(f"  {item['name']}  pads={item['pads']}")
        for w in all_warnings:
            _emit(f"  warning: {w}")
        if not do_apply:
            _emit("dry-run: nothing written — re-run with --apply")
        else:
            _emit(f"wrote {len(plan)} .kicad_mod + provenance.json")
    return EXIT["OK"]


def _cmd_library_new_symbol(args: argparse.Namespace) -> int:
    """`library new-symbol spec.json [-o LIB] [--apply]` — pin table -> .kicad_sym.

    Dry-run by default: prints the placement it computed (and any findings) so a
    caller can read the geometry before anything is written.

    The write gate is the deliberate exception to "geometry findings are
    advisory": ``SYMLIB_PIN_COINCIDENT`` in our OWN output is a connectivity
    defect — KiCad would merge those pins into one node in every placement —
    so it refuses the write (``VERIFY_FAILED``, exit 6) instead of warning.
    """
    from .. import safety, symbol_spec
    from ..checks import symlib
    from ..writers import symbol_lib

    spec_path = _require_path(getattr(args, "spec", None), "input spec JSON")
    if not spec_path.exists():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: no such spec: {spec_path}")
    spec = symbol_spec.load(spec_path)
    override = getattr(args, "name", None)
    if override:
        spec.name = override

    lay = symbol_spec.layout(spec)
    text = symbol_lib.to_kicad_sym(lay)

    out_arg = getattr(args, "out", None)
    if out_arg:
        out_path = Path(out_arg)
    else:
        parts_root = _load_cfg(args, spec_path).paths.get("parts_dir")
        name = f"{lay.name}.kicad_sym"
        out_path = Path(parts_root) / name if parts_root else Path(name)

    findings = symlib.run_text(text, str(out_path))
    blocking = [f for f in findings if f.code == symlib.SYMLIB_PIN_COINCIDENT]
    do_apply = bool(getattr(args, "apply", False))

    if args.json:
        _emit(_dumps(_stamp({
            "spec": str(spec_path),
            "out": str(out_path),
            "applied": do_apply and not blocking,
            "symbol": lay.name,
            "reference_prefix": lay.reference_prefix,
            "body_mil": {"x0": lay.body[0], "y0": lay.body[1],
                         "x1": lay.body[2], "y1": lay.body[3]},
            "pins": [{"number": p.spec.number, "name": p.spec.name,
                      "type": p.spec.type, "side": p.spec.side,
                      "group": p.spec.group, "hidden": p.spec.hidden,
                      "stack_with": p.spec.stack_with,
                      "at_mil": [p.x_mil, p.y_mil],
                      "orientation": p.orientation}
                     for p in lay.pins],
            "warnings": list(lay.warnings),
            "findings": [{"code": f.code, "severity": f.severity.value,
                          "message": f.message, "refs": f.refs}
                         for f in findings],
        })))
    else:
        _emit(f"new-symbol: {spec_path.name} -> {out_path}")
        _emit(f"  symbol {lay.name} ({lay.reference_prefix}), "
              f"{len(lay.pins)} pin(s), body "
              f"{lay.body_width_mil}x{lay.body_height_mil} mil")
        for p in lay.pins:
            stack = f"  stacked on {p.spec.stack_with}" if p.spec.stack_with else ""
            _emit(f"    {p.spec.number:>4}  {p.spec.name or '~':<14} "
                  f"{p.spec.type:<15} {p.spec.side:<6} "
                  f"at ({p.x_mil},{p.y_mil}) rot {p.orientation}"
                  f"{'  hidden' if p.spec.hidden else ''}{stack}")
        for w in lay.warnings:
            _emit(f"  warning: {w}")
        for f in findings:
            _emit(f"  {f.severity.value.upper():<8} {f.code}: {f.message}")

    if blocking:
        raise _ExitWith(
            EXIT["OPLIST"],
            f"ERROR: VERIFY_FAILED: {len(blocking)} coincident-pin finding(s) in "
            "the generated symbol; nothing was written. Two pins on one point "
            "is an electrical mis-join KiCad's ERC cannot see, so this gates "
            "the write rather than warning about it.")

    if do_apply:
        safety.refuse_if_gui_locked([out_path], what="the symbol write")
        replaced = out_path.exists()
        safety.atomic_write_with_backup(out_path, text,
                                        backup_dir=out_path.parent)
        if not args.json:
            _emit(f"wrote {out_path}"
                  + (" (left a .bak)" if replaced else ""))
    elif not args.json:
        _emit("dry-run: nothing written — re-run with --apply")
    return EXIT["OK"]


def _cmd_library_lint_symbols(args: argparse.Namespace) -> int:
    """`library lint-symbols lib.kicad_sym` — coincident/untyped/off-grid pins."""
    from ..checks import symlib

    path = _require_path(getattr(args, "path", None), "input .kicad_sym")
    if not path.exists():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: no such library: {path}")
    findings = symlib.run(path)

    if args.json:
        _emit(_dumps(_stamp({
            "library": str(path),
            "findings": [{"code": f.code, "severity": f.severity.value,
                          "message": f.message, "refs": f.refs,
                          "anchors": f.anchors}
                         for f in findings],
        })))
        return _findings_exit(findings, args)

    _emit(f"lint-symbols: {path}")
    if not findings:
        _emit("no findings — every pin has its own point, a real electrical "
              "type, and lands on the 100-mil grid")
        return EXIT["OK"]
    for f in findings:
        _emit(f"{f.severity.value.upper():<8} {f.code}: {f.message}")
    _emit(f"{len(findings)} finding(s)")
    return _findings_exit(findings, args)


_LOCKABLE_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_pro")


def _cmd_library_check_lock(args: argparse.Namespace) -> int:
    """`library check-lock [project]` — is any KiCad file open in the GUI?

    KiCad drops a ``~<name>.lck`` next to a document it holds open. akcli's own
    write ops already refuse a locked target, but hand scripts / ``sed`` do not —
    this exposes the same check so an external flow can gate on it
    (``akcli library check-lock . && ./relayout.sh``). Exit 6 (TARGET_LOCKED)
    when ANY file is locked, 0 when the tree is writable.
    """
    from ..writers import kicad as kwriter

    project = Path(getattr(args, "project", None) or ".")
    if not project.exists():
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: no such project: {project}")
    root = project if project.is_dir() else project.parent
    files = sorted(
        f for suf in _LOCKABLE_SUFFIXES for f in root.rglob(f"*{suf}")
        if f.is_file() and not f.name.startswith("~"))
    locked = [f for f in files if kwriter.gui_lock_path(f).exists()]

    if args.json:
        _emit(_dumps(_stamp({
            "project": str(root),
            "scanned": len(files),
            "locked": [{"file": str(f), "lock": str(kwriter.gui_lock_path(f))}
                       for f in locked],
            "writable": not locked,
        })))
        return EXIT["OPLIST"] if locked else EXIT["OK"]

    if not locked:
        _emit(f"check-lock: {len(files)} KiCad file(s) under {root} — none open "
              "in the GUI (safe to write)")
        return EXIT["OK"]
    _emit(f"check-lock: {len(locked)} of {len(files)} file(s) appear OPEN in the "
          "KiCad GUI — external writes are unsafe (a GUI save would overwrite them):")
    for f in locked:
        _emit(f"  LOCKED  {f}  ({kwriter.gui_lock_path(f).name})")
    _emit("close them in KiCad first, or File>Revert after writing")
    return EXIT["OPLIST"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "library", parents=[common],
        help="project library workspace: audit + repair + author "
             "(sym/fp-lib-table, footprints, 3D, symbol generation)")
    lib_sub = p.add_subparsers(dest="library_command", metavar="<subcommand>")
    p.set_defaults(handler=_cmd_library_audit, project=None, sch=None)

    pa = lib_sub.add_parser(
        "audit", parents=[common],
        help="cross-check schematics <-> lib tables <-> libraries <-> 3D models")
    pa.add_argument("project", nargs="?", default=".",
                    help="project directory or .kicad_pro (default: .)")
    pa.add_argument("--sch", metavar="FILE", action="append",
                    help="audit this schematic instead of auto-discovery "
                         "(repeatable)")
    _add_exit_policy_flags(pa)
    pa.set_defaults(handler=_cmd_library_audit)

    pr = lib_sub.add_parser(
        "repair", parents=[common],
        help="plan/apply library fixes (dry-run by default)")
    pr.add_argument("project", nargs="?", default=".",
                    help="project directory or .kicad_pro (default: .)")
    pr.add_argument("--rename-footprint-lib", metavar="OLD=NEW",
                    action="append",
                    help="rewrite Footprint fields 'OLD:*' -> 'NEW:*' in "
                         "registered symbol libraries and project schematics "
                         "(repeatable)")
    pr.add_argument("--3d-path", dest="model_path", metavar="MODE",
                    help="rewrite bare-relative 3D model paths in registered "
                         "footprint libraries: 'absolute' or a '${VAR}' prefix")
    pr.add_argument("--apply", action="store_true",
                    help="write the planned edits (atomic, with .bak)")
    pr.set_defaults(handler=_cmd_library_repair)

    pi = lib_sub.add_parser(
        "import-altium", parents=[common],
        help="convert an Altium .PcbLib into a .pretty library "
             "(pads verbatim; provenance.json records source hash + "
             "declared transformations)")
    pi.add_argument("path", nargs="?", help="input .PcbLib")
    pi.add_argument("-o", "--out", metavar="DIR",
                    help="output .pretty directory (default: <name>.pretty, "
                         "under [paths].parts_dir when configured)")
    pi.add_argument("--courtyard", type=float, metavar="MM",
                    help="synthesize a pad-bbox courtyard with this clearance "
                         "when the source has none (declared transformation)")
    pi.add_argument("--apply", action="store_true",
                    help="write the files (default: dry-run plan)")
    pi.set_defaults(handler=_cmd_library_import_altium)

    pn = lib_sub.add_parser(
        "new-symbol", parents=[common],
        help="generate a .kicad_sym from a pin-table JSON (coordinates are "
             "computed, never authored); dry-run unless --apply")
    pn.add_argument("spec", nargs="?",
                    help="input pin table (schemas/symbol-pins.schema.json)")
    pn.add_argument("-o", "--out", metavar="FILE",
                    help="output .kicad_sym (default: <symbol name>.kicad_sym, "
                         "under [paths].parts_dir when configured)")
    pn.add_argument("--name", metavar="NAME",
                    help="override the spec's symbol name")
    pn.add_argument("--apply", action="store_true",
                    help="write the library (default: dry-run plan). Refuses "
                         "on any SYMLIB_PIN_COINCIDENT in its own output — two "
                         "pins on one point is connectivity, not geometry")
    pn.set_defaults(handler=_cmd_library_new_symbol)

    pls = lib_sub.add_parser(
        "lint-symbols", parents=[common],
        help="lint a .kicad_sym for coincident pins (an ERC-invisible "
             "mis-join), unset electrical types and off-grid pins")
    pls.add_argument("path", nargs="?", help="the .kicad_sym to lint")
    _add_exit_policy_flags(pls)
    pls.set_defaults(handler=_cmd_library_lint_symbols)

    pl = lib_sub.add_parser(
        "check-lock", parents=[common],
        help="report which KiCad files are open in the GUI (~<name>.lck); "
             "exit 6 if any are locked, so external flows can gate")
    pl.add_argument("project", nargs="?", default=".",
                    help="project directory or file (default: .)")
    pl.set_defaults(handler=_cmd_library_check_lock)

    # `library relink` — the library-maintenance home of the top-level
    # `relink-symbols` (same handler + flags; the old name stays for compat)
    from .checks import _cmd_relink
    pk = lib_sub.add_parser(
        "relink", parents=[common],
        help="re-embed stale lib_symbols cache entries from fresh "
             ".kicad_sym libraries (alias of `relink-symbols`; dry-run "
             "unless --apply)")
    pk.add_argument("target", nargs="?", help="the .kicad_sch to relink")
    pk.add_argument("--libs", metavar="DIR", action="append",
                    help="symbol library dir or .kicad_sym file (repeatable); "
                         "default: the KiCad.app SharedSupport symbols dir "
                         "if it exists")
    pk.add_argument("--only", metavar="NICKS",
                    help="comma-separated library nicknames (or full lib_ids) "
                         "to consider")
    pk.add_argument("--apply", action="store_true",
                    help="write the replacements (default: preview only; "
                         "net-membership equivalence gated, leaves <name>.bak)")
    pk.set_defaults(handler=_cmd_relink)
