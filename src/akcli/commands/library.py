"""`akcli library` — project library workspace: audit, repair, author, lint.

``audit`` cross-checks schematics <-> sym/fp-lib-table <-> library contents <->
3D models and reports lint-style findings (exit 1 on >= WARNING, after the
config ``[[waiver]]`` entries — same conventions as ``akcli check``). ``repair``
productizes the two historically hand-``sed``-ed fixes — footprint-nickname
renames and 3D-model path policy — as a dry-run plan; ``--apply`` writes
atomically with a ``.bak`` next to each file, re-audits, and exits 1 when an
ERROR is left in a file it rewrote. It refuses ``TARGET_LOCKED``
(the shared :func:`akcli.safety.refuse_if_gui_locked` gate) if KiCad holds any
target open — the same invariant as ``draw --apply``, with the same
``--allow-open`` override (``repair``, ``relink``, ``import-altium``).

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

from .. import contract as _contract
from .. import report as _report
from ..errors import EXIT
from ._shared import (
    _add_exit_policy_flags,
    _apply_config_waivers,
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


def _project_cfg(args: argparse.Namespace, ws):
    """Config for a project-scoped command: ``-C``, else walk-up discovery
    starting IN the project directory (``_load_cfg`` starts at ``near.parent``,
    so it is handed a path inside the project, not the directory itself)."""
    return _load_cfg(args, ws.project_dir / "akcli.toml")


def _baseline_path(ws, cfg, value: str) -> Path:
    """Where the library baseline lives: an explicit FILE, else
    ``[paths].library_baseline``, else the default name beside the
    ``.kicad_pro`` — a committed deliverable, never under ``.akcli/``."""
    from .. import library_baseline
    if value:
        return Path(value)
    configured = cfg.paths.get("library_baseline")
    if configured:
        return Path(configured)
    return ws.project_dir / library_baseline.DEFAULT_FILENAME


def _baseline_step(args: argparse.Namespace, ws, cfg, sch_override
                   ) -> tuple[list, dict | None]:
    """``--baseline`` / ``--write-baseline``: ``(extra findings, report)``.

    ``--write-baseline`` is itself the write intent (no ``--apply``); the one
    refusal is overwriting a file that is not a library baseline, because
    ``--write-baseline WORD`` reads WORD as the file — a project path placed
    after the flag must never be clobbered.
    """
    import hashlib

    from .. import library_baseline
    from ..errors import AkcliError

    write_arg = getattr(args, "write_baseline", None)
    compare_arg = getattr(args, "baseline", None)
    if write_arg is None and compare_arg is None:
        return [], None
    if write_arg is not None and compare_arg is not None:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: --baseline compares and --write-baseline "
                        "rewrites the same file — pass one of them")
    path = _baseline_path(ws, cfg, write_arg or compare_arg or "")

    if compare_arg is not None:
        base = library_baseline.load(path)     # missing -> exit 4; bad -> BAD_CONFIG
        drift = library_baseline.compare(
            base, library_baseline.build(ws, sch_override))
        return drift, {
            "path": path.as_posix(), "action": "compared",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "differences": len(drift),
        }

    hint = ("`--write-baseline WORD` reads WORD as the file: put the project "
            "first (`akcli library audit DIR --write-baseline`), or delete "
            "the file to replace it")
    if path.is_dir():
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: --write-baseline {path}: is a directory — "
                        + hint)
    if path.exists():
        try:
            library_baseline.load(path)
        except AkcliError:
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: refusing to overwrite {path}: it exists "
                            "and is not a library baseline — " + hint)
    doc = library_baseline.build(ws, sch_override)
    changed = library_baseline.write(path, doc)
    return [], {
        "path": path.as_posix(),
        "action": "written" if changed else "unchanged",
        "sha256": hashlib.sha256(
            library_baseline.dumps(doc).encode("utf-8")).hexdigest(),
        **library_baseline.summary(doc),
    }


def _baseline_line(report: dict) -> str:
    """The text-mode line for a ``--baseline`` / ``--write-baseline`` run."""
    if report["action"] == "compared":
        n = report["differences"]
        return (f"{report['path']} — compared: "
                + ("matches" if not n else f"{n} difference(s)"))
    return (f"{report['path']} — {report['action']}: "
            f"{report['libraries']} librar(ies), {report['files']} file(s), "
            f"{report['models_hashed']} 3D model(s) hashed, "
            f"{report['models_unresolved']} unresolved, "
            f"{report['models_builtin']} KiCad built-in (by reference); "
            f"{report['global_libraries']} global librar(ies) by nickname")


def _cmd_library_audit(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    sch_override = [Path(s) for s in (getattr(args, "sch", None) or [])] or None
    from .. import libtable
    cfg = _project_cfg(args, ws)
    raw = libtable.audit(ws, sch_override)
    # --baseline adds its differences to the audit's findings (same waivers,
    # same exit policy); --write-baseline writes and adds none.
    baseline_findings, baseline = _baseline_step(args, ws, cfg, sch_override)
    # config [[waiver]] entries apply exactly as in `akcli check`: waived
    # findings are named on stderr, counted in the output, and the exit is
    # computed from what is left.
    findings, waived_summary, waived = _apply_config_waivers(
        raw + baseline_findings, cfg)

    if args.json:
        payload = {
            "project": str(ws.project_dir),
            "sym_lib_table": str(ws.sym_table.path) if ws.sym_table else None,
            "fp_lib_table": str(ws.fp_table.path) if ws.fp_table else None,
            "schematics": [str(s) for s in (sch_override or ws.schematics)],
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message, "refs": f.refs, "anchors": f.anchors}
                for f in findings
            ],
            "config_waived": waived_summary,
            "waived": waived,
        }
        if baseline is not None:
            payload["baseline"] = baseline
        _emit(_dumps(_stamp(payload)))
        return _findings_exit(findings, args)

    _emit(f"library audit: {ws.project_dir}")
    _emit(f"  sym-lib-table: {ws.sym_table.path.name if ws.sym_table else '(none)'}"
          f"{'  (' + str(len(ws.sym_table.entries)) + ' libs)' if ws.sym_table else ''}")
    _emit(f"  fp-lib-table:  {ws.fp_table.path.name if ws.fp_table else '(none)'}"
          f"{'  (' + str(len(ws.fp_table.entries)) + ' libs)' if ws.fp_table else ''}")
    _emit(f"  schematics:    {len(sch_override or ws.schematics)}")
    _emit(f"  config-waived: {waived_summary}")
    if baseline is not None:
        _emit(f"  baseline:      {_baseline_line(baseline)}")
    if not findings:
        compared = baseline is not None and baseline["action"] == "compared"
        _emit("no findings left — every finding was config-waived" if waived
              else "no findings — nicknames, footprints and 3D references "
                   "all resolve"
                   + (", and every library file matches the baseline"
                      if compared else ""))
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
    applied = do_apply and bool(edits)
    if applied:
        # Same invariant as `draw --apply` / `relink --apply`: never write a
        # file KiCad holds open (a GUI save would overwrite it from memory).
        # Checked before any output so a refusal writes nothing at all.
        from .. import safety
        safety.refuse_if_gui_locked(
            [e.path for e in edits],
            allow_open=bool(getattr(args, "allow_open", False)),
            what="the repair write")
        # Loaded BEFORE the first write, for the post-apply audit: a broken
        # akcli.toml must refuse the repair (BAD_CONFIG), not interrupt it
        # between the write and the report.
        waivers = _project_cfg(args, ws).waivers
    payload: dict = {
        "project": str(ws.project_dir),
        "applied": applied,
        "edits": [{"path": str(e.path), "description": e.description}
                  for e in edits],
    }
    if not args.json:
        if not edits:
            _emit("repair: nothing to change")
            return EXIT["OK"]
        for e in edits:
            _emit(f"  {e.description}")
        if not do_apply:
            _emit(f"dry-run: {len(edits)} file(s) would change — re-run with --apply")

    code = EXIT["OK"]
    if applied:
        from .. import safety
        for e in edits:
            safety.atomic_write_with_backup(e.path, e.new_text,
                                            backup_dir=e.path.parent)
        if not args.json:
            _emit(f"applied {len(edits)} file(s) (each left a .bak) — "
                  "re-run `akcli library audit` to confirm")
        # Post-apply re-audit so a bad repair cannot claim success: an ERROR
        # left in a file this repair REWROTE fails it (exit 1). An error
        # elsewhere in the project was there before and is not the repair's
        # doing — reported, never the exit.
        mine, elsewhere = _post_repair_errors(ws, edits, waivers)
        payload["post_audit"] = {
            "errors": len(mine),
            "remaining": sorted({f.code for f in mine}),
            "unrelated_errors": len(elsewhere),
        }
        if mine:
            code = EXIT["FINDINGS"]
            sys.stderr.write(
                f"post-repair audit: {len(mine)} error(s) remain in the "
                "repaired file(s):\n")
            for f in mine:
                sys.stderr.write(f"  {f.code}: {f.message}\n")
        if elsewhere:
            sys.stderr.write(
                f"WARNING: post-repair audit: {len(elsewhere)} error(s) in "
                "files this repair did not touch; run `akcli library audit` "
                "for details\n")
    if args.json:
        # emitted AFTER the write + re-audit: the payload reports what
        # happened, and a failed write leaves the error envelope the only
        # document on stdout
        _emit(_dumps(_stamp(payload)))
    return code


_ERROR_SEVERITIES = (_report.Severity.ERROR, _report.Severity.CRITICAL)


def _post_repair_errors(ws, edits: list, waivers: list[dict]) -> tuple[list, list]:
    """``(in_repaired_files, elsewhere)`` — the ERROR findings a re-audit of
    the whole project still reports, split by whether the finding is about a
    file one of ``edits`` rewrote.

    Config ``[[waiver]]`` entries apply as in ``library audit`` (a waived or
    demoted finding is not a remaining error), so the two commands agree on
    what "clean" means for the same project.
    """
    from .. import libtable

    touched: set[Path] = set()
    for e in edits:
        p = Path(e.path).resolve()
        touched.add(p)
        if p.suffix == ".kicad_mod":
            touched.add(p.parent)     # footprint findings name the .pretty dir
    mine: list = []
    elsewhere: list = []
    for finding, origin in libtable.audit_with_origins(ws):
        kept, _waived, _demoted = _report.apply_waivers([finding], waivers)
        if not kept or kept[0].severity not in _ERROR_SEVERITIES:
            continue
        hit = origin is not None and Path(origin).resolve() in touched
        (mine if hit else elsewhere).append(kept[0])
    return mine, elsewhere


def _sanitize_fp_name(name: str) -> str:
    """Make a footprint name filesystem/library-safe (declared, reported)."""
    out = "".join(("-" if c in '()[]{}<>:;"/\\|?*' or c.isspace() else c)
                  for c in name).strip("-.")
    return out or "footprint"


def _parse_layer_map(values: list[str] | None) -> dict[str, str]:
    """`--layer-map ALTIUM=KICAD[,ALTIUM=KICAD...]` (repeatable) -> a validated,
    name-sorted mapping. Any unknown layer name is a usage error: a typo here
    would otherwise silently leave the layer on its default."""
    from ..readers import footprint_lib

    def bad(msg: str) -> _ExitWith:
        return _ExitWith(EXIT["USAGE"], f"ERROR: --layer-map: {msg}")

    out: dict[str, str] = {}
    for value in values or []:
        for pair in value.split(","):
            altium, sep, kicad = (part.strip() for part in pair.partition("="))
            if not sep or not altium or not kicad:
                raise bad(f"expected ALTIUM=KICAD, got {pair.strip()!r}")
            source = footprint_lib.canonical_altium_layer(altium)
            if source is None:
                raise bad(f"unknown Altium layer {altium!r} (e.g. TopOverlay, "
                          "Mechanical15, KeepOut)")
            if kicad not in footprint_lib.KICAD_FOOTPRINT_LAYERS:
                raise bad(f"{kicad!r} is not a KiCad footprint layer (e.g. "
                          "F.CrtYd, F.Fab, F.SilkS, Dwgs.User, User.1)")
            if out.get(source, kicad) != kicad:
                raise bad(f"{source} is mapped twice ({out[source]}, {kicad})")
            out[source] = kicad
    return dict(sorted(out.items()))


def _cmd_library_import_altium(args: argparse.Namespace) -> int:
    """`library import-altium part.PcbLib --out lib.pretty` — PcbLib -> .kicad_mod."""
    import hashlib

    from .. import __version__, safety
    from ..readers import footprint_lib
    from ..writers import footprint_mod

    src = Path(getattr(args, "path", None) or "")
    if not str(src):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing input .PcbLib")
    user_layers = _parse_layer_map(getattr(args, "layer_map", None))
    lib, primitives = footprint_lib.read_pcblib_report(src, layer_map=user_layers)
    layer_map = lib.metadata["layer_map"]

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
    row_keys = ("name", "source_name", "pads", "primitives", "file")
    all_warnings: list[str] = list(lib.warnings)
    # a mapping that matched nothing is most likely the wrong layer number
    all_warnings.extend(
        f"--layer-map {source}={target}: no track or arc on {source} in "
        "this library"
        for source, target in user_layers.items() if source not in layer_map)
    for fp, counts in zip(lib.footprints, primitives, strict=True):
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
                     "primitives": counts,
                     "file": str(out_dir / f"{safe}.kicad_mod"),
                     "text": text})

    if do_apply:
        # Same invariant as `draw --apply`, over the files this import would
        # OVERWRITE (a re-import into an existing .pretty): never write a file
        # KiCad holds open. Checked first, so a refusal writes nothing at all.
        outputs = [Path(item["file"]) for item in plan]
        outputs.append(out_dir / "provenance.json")
        safety.refuse_if_gui_locked(
            [f for f in outputs if f.exists()],
            allow_open=bool(getattr(args, "allow_open", False)),
            what="the footprint import")
        out_dir.mkdir(parents=True, exist_ok=True)
        for item in plan:
            safety.atomic_write_with_backup(item["file"], item["text"])
        provenance = {
            "schema_version": _contract.SCHEMA_VERSIONS["envelope"],
            "source": {"path": str(src),
                       "sha256": hashlib.sha256(src.read_bytes()).hexdigest()},
            "converter": {"tool": "akcli library import-altium",
                          "version": __version__},
            "options": {"courtyard_mm": courtyard, "layer_map": user_layers},
            "layer_map": layer_map,
            "footprints": [{k: item[k] for k in row_keys} for item in plan],
            "warnings": all_warnings,
        }
        safety.atomic_write_with_backup(
            out_dir / "provenance.json", _dumps(provenance) + "\n")

    if args.json:
        _emit(_dumps(_stamp({
            "source": str(src),
            "out": str(out_dir),
            "applied": do_apply,
            "layer_map": layer_map,
            "footprints": [{k: item[k] for k in row_keys} for item in plan],
            "warnings": all_warnings,
        })))
    else:
        _emit(f"import-altium: {src.name} -> {out_dir} "
              f"({len(plan)} footprint(s))")
        if layer_map:
            _emit("  layers: " + ", ".join(
                f"{source} -> {target or 'skipped'}"
                for source, target in layer_map.items()))
        for item in plan:
            got = item["primitives"]["imported"]
            _emit(f"  {item['name']}  pads={item['pads']}"
                  f" tracks={got.get('track', 0)} arcs={got.get('arc', 0)}"
                  f" skipped={sum(item['primitives']['skipped'].values())}")
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
    pa.add_argument("--write-baseline", dest="write_baseline", nargs="?",
                    const="", default=None, metavar="FILE",
                    help="write the library baseline: a committed, sha256-"
                         "hashed record of every project lib-table entry "
                         "(.kicad_sym, .kicad_mod, 3D models, provenance.json)"
                         ". FILE defaults to [paths].library_baseline, else "
                         "akcli-library.baseline.json beside the .kicad_pro. "
                         "Writes immediately (the flag is the write intent); "
                         "put the project BEFORE this flag")
    pa.add_argument("--baseline", nargs="?", const="", default=None,
                    metavar="FILE",
                    help="compare the libraries on disk against a baseline "
                         "(same FILE default) and add LIB_BASELINE_DRIFT / "
                         "_MISSING (error) and _UNTRACKED (warning) findings")
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
    pr.add_argument("--allow-open", dest="allow_open", action="store_true",
                    help="write even when a KiCad GUI lock file is present "
                         "next to a file being repaired (File>Revert in "
                         "KiCad afterwards)")
    pr.set_defaults(handler=_cmd_library_repair)

    pi = lib_sub.add_parser(
        "import-altium", parents=[common],
        help="convert an Altium .PcbLib into a .pretty library "
             "(pads verbatim, tracks/arcs as graphics; provenance.json "
             "records source hash + declared transformations)")
    pi.add_argument("path", nargs="?", help="input .PcbLib")
    pi.add_argument("-o", "--out", metavar="DIR",
                    help="output .pretty directory (default: <name>.pretty, "
                         "under [paths].parts_dir when configured)")
    pi.add_argument("--courtyard", type=float, metavar="MM",
                    help="synthesize a pad-bbox courtyard with this clearance "
                         "when the source has none (declared transformation)")
    pi.add_argument("--layer-map", dest="layer_map", metavar="ALTIUM=KICAD",
                    action="append",
                    help="put the graphics of an Altium layer on this KiCad "
                         "layer, e.g. Mechanical15=F.CrtYd (repeatable, or "
                         "comma-separated; default: KiCad's own importer "
                         "mapping, MechanicalN -> User.N with a warning)")
    pi.add_argument("--apply", action="store_true",
                    help="write the files (default: dry-run plan)")
    pi.add_argument("--allow-open", dest="allow_open", action="store_true",
                    help="overwrite existing output files even when a KiCad "
                         "GUI lock file is present next to them")
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
    pk.add_argument("--allow-open", dest="allow_open", action="store_true",
                    help="write even when a KiCad GUI lock file is present "
                         "(File>Revert in KiCad afterwards)")
    pk.set_defaults(handler=_cmd_relink)
