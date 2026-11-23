"""``akcli project`` — create, complete, inspect and gate a real KiCad project.

Until this command existed, ``akcli new`` produced a single ``.kicad_sch`` and
nothing else: opening the result in KiCad prompted to create a project, found
no library tables and no board. Everything akcli itself needed worked, so the
gap was invisible to every gate — and visible to every human the moment they
double-clicked the file.

Two write verbs over one engine (:mod:`..scaffold`):

* ``project new`` — scaffold from scratch. Refuses an existing project rather
  than overwriting it (``PROJECT_EXISTS``, exit 2) unless ``--force``.
* ``project init`` — complete what is already there. Purely additive: existing
  files are ``kept``, never rewritten, so running it on a real design is safe
  and running it twice reports ``unchanged``. There is deliberately no
  ``--force`` here; overwriting is ``new``'s job, and conflating them is how a
  "just add the lib tables" command ends up destroying a schematic.

``--dry-run`` always exits 0, listing what WOULD happen (conflicts included) —
the plan is the same object the apply path consumes, so the preview cannot
drift from the write.

Two read verbs complete the family:

* ``project info`` — what the ``.kicad_pro`` actually says: root-sheet binding,
  sheet tree, boards, lib tables, net classes, text variables, pinned libs.
  Read-only, exit 0 on any project it can find (even a malformed one — the
  degrade is reported in ``ok``/``error``, not raised).
* ``project check`` — lint-style findings over the same material
  (:func:`check_findings`), sharing the one ``--fail-on`` exit policy every
  findings-emitting command uses. A freshly scaffolded project reports NOTEs at
  most, so ``project new`` followed by ``project check`` exits 0: the gate is
  calibrated against the thing this very command family produces.

The read split is deliberate. ``info`` answers "what IS this project", ``check``
answers "is anything WRONG with it"; fusing them would make the inventory verb
gate CI and the gate verb dump inventory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..errors import EXIT, AkcliError, fail
from ._shared import (
    _add_exit_policy_flags,
    _dumps,
    _emit,
    _ExitWith,
    _findings_exit,
    _gitignored,
    _stamp,
)


def _paper(args: argparse.Namespace) -> str:
    from ..writers.blank import KICAD_PAPERS

    paper = getattr(args, "paper", None) or "A4"
    if paper not in KICAD_PAPERS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown paper {paper!r} "
                        f"(one of: {', '.join(KICAD_PAPERS)})")
    return paper


def _example(args: argparse.Namespace) -> str | None:
    """Validate ``--example NAME`` against what this install actually ships.

    The roster comes from the packaged directory (:mod:`..examples`), never a
    hand-kept constant, so a wheel built without the example package data
    reports "(none packaged)" instead of offering a name it cannot deliver.
    """
    name = getattr(args, "example", None)
    if not name:
        return None
    from ..examples import names

    have = names()
    if name not in have:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown example {name!r} "
                        f"(one of: {', '.join(have) or '(none packaged)'})")
    return str(name)


def _build_plan(args: argparse.Namespace, mode: str):
    """Plan the scaffold described by ``args`` (pure — writes nothing)."""
    from .. import scaffold

    return scaffold.plan(
        Path(getattr(args, "directory", None) or "."),
        mode=mode,                                  # type: ignore[arg-type]
        name=getattr(args, "name", None),
        paper=_paper(args),
        title=getattr(args, "title", None),
        force=bool(getattr(args, "force", False)),
        no_pcb=bool(getattr(args, "no_pcb", False)),
        no_lib_tables=bool(getattr(args, "no_lib_tables", False)),
        no_gitignore=bool(getattr(args, "no_gitignore", False)),
        sheets=scaffold.sheet_names(getattr(args, "sheets", None)),
        root=Path(args.root) if getattr(args, "root", None) else None,
        example=_example(args),
    )


# Text-mode action column: uppercase CONFLICT so the one line that means
# "nothing was written" cannot be skimmed past in a list of lowercase verbs.
_ACTION_LABEL = {
    "created": "created",
    "kept": "kept",
    "overwritten": "OVERWROTE",
    "conflict": "CONFLICT",
}


def _render_text(p, status: str, dry_run: bool) -> str:
    lines = [f"{_ACTION_LABEL.get(f.action, f.action):<9} {f.name}"
             for f in p.files]
    suffix = " (nothing written)" if dry_run else ""
    lines.append(
        f"status: {status} — {len(p.created)} created, {len(p.kept)} kept, "
        f"{len(p.overwritten)} overwritten, {len(p.conflicts)} conflict(s) "
        f"in {p.directory}{suffix}")
    return "\n".join(lines)


def _emit_plan(args: argparse.Namespace, p, *, mode: str, status: str,
               dry_run: bool, applied: bool) -> None:
    if args.json:
        payload = {"command": f"project {mode}", "dry_run": dry_run,
                   "applied": applied, "status": status}
        payload.update(p.to_json())
        _emit(_dumps(_stamp(payload)))
        return
    _emit(_render_text(p, status, dry_run))
    sheets = [f.name for f in p.files if f.kind == "sheet"]
    if sheets:
        # The child files exist, but nothing INSTANTIATES them yet — placing
        # the (sheet ...) node is op-list territory, not scaffolding.
        sys.stderr.write(
            f"note: {', '.join(sheets)} are blank child sheets; add an "
            "`add_sheet` op to the root's op-list to instantiate them\n")
    if any(f.kind == "ops" and f.action in ("created", "overwritten")
           for f in p.files):
        # Same shape of note: the file landed, but nothing has DRAWN it.
        sys.stderr.write(
            f"note: ops.json holds the example op-list; draw it with "
            f"`akcli draw {p.root_sch.name} --ops ops.json --apply`\n")


def _run(args: argparse.Namespace, mode: str) -> int:
    from .. import scaffold

    p = _build_plan(args, mode)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        scaffold.record(p, "dry-run", dry_run=True)
        _emit_plan(args, p, mode=mode, status="dry-run", dry_run=True,
                   applied=False)
        return EXIT["OK"]
    if p.conflicts:
        # apply() preflights this too; refusing here lets the message name the
        # verb that WOULD work instead of the generic engine wording.
        alt = ("--force to overwrite (backups under .akcli/backups/)"
               if mode == "new" else "a different --name")
        fail("PROJECT_EXISTS",
             f"{p.directory} already has {', '.join(p.conflicts)} — run "
             f"`akcli project init {p.directory}` to add only what is missing, "
             f"or pass {alt}")
    scaffold.apply(p)
    status = scaffold.status_of(p)
    _emit_plan(args, p, mode=mode, status=status, dry_run=False,
               applied=bool(p.writes))
    return EXIT["OK"]


def _cmd_project_new(args: argparse.Namespace) -> int:
    """`project new [DIR]` — scaffold a complete KiCad project."""
    return _run(args, "new")


def _cmd_project_init(args: argparse.Namespace) -> int:
    """`project init [DIR]` — add the missing project files, keep the rest."""
    return _run(args, "init")


# --------------------------------------------------------------------------- #
# read side: locating the project, and the facts both read verbs share
# --------------------------------------------------------------------------- #
# KiCad churn `project check` expects a .gitignore to cover. Textual patterns,
# matched textually (see ``_gitignored``) — this is a NOTE-level hygiene hint,
# not a gitignore engine.
_STATE_PATTERNS: tuple[str, ...] = (
    ".akcli", "*-backups", "*.kicad_prl", "~*.lck",
)


def _target(args: argparse.Namespace) -> Path:
    """The ``.kicad_pro`` named by ``DIR`` / ``FILE`` (``PROJECT_FILE_MISSING``).

    Accepts either shape for the same reason ``libtable``'s workspace does: an
    agent that already holds the project file path should not have to strip it
    back to a directory, and a human typing ``.`` should not have to find it.
    """
    from ..readers import kicad_pro as kpro

    raw = Path(getattr(args, "directory", None) or ".")
    if raw.is_file() and raw.suffix != ".kicad_pro":
        fail("PROJECT_FILE_MISSING",
             f"{raw} is not a .kicad_pro — pass the project file or the "
             "directory that holds it")
    found = kpro.find(raw)
    if found is None:
        fail("PROJECT_FILE_MISSING",
             f"no .kicad_pro in {raw} — run `akcli project init {raw}` to "
             "create one around the schematics already there")
    return found.resolve()


def _root_sch_of(pro_path: Path) -> Path:
    """The schematic KiCad opens for ``pro_path`` — by KiCad's own naming rule.

    KiCad ties the root sheet to the project by FILENAME (``demo.kicad_pro`` ->
    ``demo.kicad_sch``); the uuid in ``sheets[0]`` then confirms the binding.
    So the path is derived from the stem and the uuid is *checked* against it —
    which is exactly how ``PROJECT_ROOT_SHEET_MISMATCH`` becomes detectable
    instead of the two facts silently disagreeing.
    """
    return pro_path.parent / f"{pro_path.stem}.kicad_sch"


def _sch_root_uuid(sch: Path) -> str:
    """A schematic's own root ``(uuid …)``; ``""`` when it cannot be read."""
    from ..readers import kicad as kreader

    try:
        return kreader.sheet_refs(sch)[0] or ""
    except Exception:
        return ""


def check_findings(pro_path: Path, *, with_lib_audit: bool = True) -> list:
    """Findings about the integrity of the project rooted at ``pro_path``.

    Shared by ``project check`` and the ``release preflight`` ``project`` gate,
    which passes ``with_lib_audit=False`` because preflight already runs
    ``library-audit`` as its own gate — one finding reported twice reads as two
    problems.

    Severity policy, calibrated so a freshly scaffolded project exits 0:

    * ``PROJECT_FILE_MALFORMED`` (WARNING) — the ``.kicad_pro`` did not parse.
      Not an ERROR: every akcli read path still works without it, so this is
      "KiCad will repair this on open", not "the design is broken".
    * ``PROJECT_META_FILENAME_STALE`` (NOTE) — ``meta.filename`` no longer
      matches the file's own name (a renamed project). Cosmetic to KiCad.
    * ``PROJECT_ROOT_SHEET_MISSING`` / ``PROJECT_ROOT_SHEET_MISMATCH`` (ERROR)
      — the project does not point at a schematic that exists, or points at a
      DIFFERENT design than the one on disk. Both mean opening the project
      loses work, which is the one thing worth failing a gate for.
    * ``SHEET_FILE_MISSING`` (ERROR, reused from the op-list vocabulary) — a
      ``Sheetfile`` in the tree names a file that is not there.
    * ``PROJECT_PCB_MISSING`` (NOTE) — no board container. akcli never writes
      PCB content, and ``project new --no-pcb`` is a supported choice.
    * ``PROJECT_STATE_UNIGNORED`` (NOTE) — KiCad/akcli churn is not gitignored.
    """
    from ..readers import kicad_pro as kpro
    from ..report import Finding, Severity

    directory = pro_path.parent
    pro = kpro.read(pro_path)
    out: list = []

    if not pro.ok:
        out.append(Finding(
            "PROJECT_FILE_MALFORMED", Severity.WARNING,
            pro.error or f"{pro_path.name} could not be read"))
    elif pro.meta_filename and pro.meta_filename != pro_path.name:
        out.append(Finding(
            "PROJECT_META_FILENAME_STALE", Severity.NOTE,
            f"{pro_path.name}: meta.filename says {pro.meta_filename!r} "
            "(the project was renamed on disk; KiCad rewrites this on save)"))

    # --- root sheet binding -------------------------------------------------
    root_sch = _root_sch_of(pro_path)
    want = kpro.root_sheet_uuid(pro)
    if not root_sch.is_file():
        out.append(Finding(
            "PROJECT_ROOT_SHEET_MISSING", Severity.ERROR,
            f"{pro_path.name} has no root schematic on disk "
            f"({root_sch.name} is missing)"))
    elif want is None:
        # A file that PARSED but names no sheet is a real defect; a file that
        # did not parse already reported itself and must not report twice.
        if pro.ok:
            out.append(Finding(
                "PROJECT_ROOT_SHEET_MISSING", Severity.ERROR,
                f"{pro_path.name} names no root sheet (empty `sheets`); KiCad "
                f"will re-bind it to {root_sch.name} and mint a new uuid"))
    else:
        got = _sch_root_uuid(root_sch)
        if got and got != want:
            out.append(Finding(
                "PROJECT_ROOT_SHEET_MISMATCH", Severity.ERROR,
                f"{pro_path.name} binds root sheet {want} but "
                f"{root_sch.name} is {got} — the project points at a design "
                "this file no longer is (regenerated root schematic?)"))

    # --- sheet tree: every referenced child must exist ----------------------
    # The ONE tree walk (`check --integrity` runs the same one), narrowed to
    # the finding this command owns: the document-level findings it also
    # returns belong to `check`, and `release preflight` runs both gates —
    # one finding reported twice reads as two problems.
    missing: list = []
    if root_sch.is_file():
        from ..checks import integrity

        try:
            missing = [f for f in integrity.run_tree(root_sch, label_root=True)
                       if f.code == "SHEET_FILE_MISSING"]
        except (AkcliError, OSError):
            pass              # unreadable root: `read`/`check` reports it
        out.extend(missing)

    # --- board container ----------------------------------------------------
    if not (directory / f"{pro_path.stem}.kicad_pcb").is_file():
        out.append(Finding(
            "PROJECT_PCB_MISSING", Severity.NOTE,
            f"no {pro_path.stem}.kicad_pcb — pcbnew will offer to create one; "
            "`akcli project init` writes a blank board container (akcli never "
            "writes PCB content)"))

    # --- library workspace --------------------------------------------------
    if with_lib_audit:
        from .. import libtable

        ws = libtable.discover(pro_path)
        audit = libtable.audit(ws, [root_sch] if root_sch.is_file() else None)
        # The audit loads the tree through the reader, which stops at a
        # missing child and reports "<child> (sheet '<name>' referenced from
        # <parent>)" as SCH_UNREADABLE: the SHEET_FILE_MISSING above, worded
        # as its symptom. One finding per cause — drop that echo, and only
        # that (a schematic unreadable for any OTHER reason still reports).
        echoes = {f"{Path(str(f.refs[1])).name} (sheet "
                  for f in missing if len(f.refs) > 1}
        out.extend(f for f in audit
                   if not (f.code == "SCH_UNREADABLE"
                           and any(e in f.message for e in echoes)))

    # --- derived-state hygiene ----------------------------------------------
    unignored = [tok for tok in _STATE_PATTERNS
                 if not _gitignored(directory, tok)]
    if unignored:
        out.append(Finding(
            "PROJECT_STATE_UNIGNORED", Severity.NOTE,
            f"no .gitignore rule covers {', '.join(unignored)} — KiCad and "
            "akcli churn (backups, per-user state, GUI locks) will show up as "
            "untracked changes"))
    return out


def _cmd_project_info(args: argparse.Namespace) -> int:
    """`project info [DIR|FILE]` — what the .kicad_pro binds and holds."""
    from .. import project as project_mod
    from ..readers import kicad_pro as kpro

    pro_path = _target(args)
    directory = pro_path.parent
    pro = kpro.read(pro_path)
    root_sch = _root_sch_of(pro_path)
    tree = project_mod.tree_files(root_sch) if root_sch.is_file() else []
    schematics = sorted(directory.glob("*.kicad_sch"))
    boards = sorted(directory.glob("*.kicad_pcb"))
    sym = directory / "sym-lib-table"
    fp = directory / "fp-lib-table"

    if args.json:
        _emit(_dumps(_stamp({
            "project_file": pro_path.as_posix(),
            "name": pro_path.stem,
            "ok": pro.ok,
            "error": pro.error,
            "root_sheet": {
                "uuid": kpro.root_sheet_uuid(pro),
                "file": root_sch.as_posix() if root_sch.is_file() else None,
            },
            "sheet_tree": [p.as_posix() for p in tree],
            "schematics": [p.as_posix() for p in schematics],
            "boards": [p.as_posix() for p in boards],
            "lib_tables": {
                "sym": sym.as_posix() if sym.is_file() else None,
                "fp": fp.as_posix() if fp.is_file() else None,
            },
            "net_classes": pro.net_classes,
            "text_variables": pro.text_variables,
            "pinned_libs": {
                "symbol": pro.pinned_symbol_libs,
                "footprint": pro.pinned_footprint_libs,
            },
        })))
        return EXIT["OK"]

    _emit(f"# akcli project info: {pro_path.name}")
    state = "ok" if pro.ok else f"UNREADABLE ({pro.error})"
    _emit(f"  {'project':<12} {pro_path.as_posix()}  [{state}]")
    ruuid = kpro.root_sheet_uuid(pro) or "(none)"
    where = root_sch.name if root_sch.is_file() else f"{root_sch.name} (missing)"
    _emit(f"  {'root sheet':<12} {where}  {ruuid}")
    _emit(f"  {'sheet tree':<12} {len(tree)} file(s)"
          + (f": {', '.join(p.name for p in tree)}" if tree else ""))
    _emit(f"  {'schematics':<12} {len(schematics)}")
    _emit(f"  {'boards':<12} {len(boards)}"
          + (f": {', '.join(p.name for p in boards)}" if boards else ""))
    _emit(f"  {'lib tables':<12} sym-lib-table: "
          f"{'yes' if sym.is_file() else 'no'}, fp-lib-table: "
          f"{'yes' if fp.is_file() else 'no'}")
    _emit(f"  {'net classes':<12} "
          f"{', '.join(pro.net_classes) if pro.net_classes else '(none)'}")
    _emit(f"  {'text vars':<12} "
          + (", ".join(f"{k}={v}" for k, v in sorted(pro.text_variables.items()))
             if pro.text_variables else "(none)"))
    pinned = pro.pinned_symbol_libs + pro.pinned_footprint_libs
    _emit(f"  {'pinned libs':<12} "
          f"{', '.join(pinned) if pinned else '(none)'}")
    return EXIT["OK"]


def _cmd_project_check(args: argparse.Namespace) -> int:
    """`project check [DIR|FILE]` — lint-style project-integrity findings."""
    pro_path = _target(args)
    findings = check_findings(pro_path)

    if args.json:
        _emit(_dumps(_stamp({
            "project_file": pro_path.as_posix(),
            "name": pro_path.stem,
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message}
                for f in findings
            ],
        })))
        return _findings_exit(findings, args)

    _emit(f"project check: {pro_path.as_posix()}")
    if not findings:
        _emit("no findings — project file, root binding, sheet tree, board and "
              "lib tables all line up")
        return EXIT["OK"]
    for f in findings:
        _emit(f"{f.severity.value.upper():<8} {f.code}: {f.message}")
    _emit(f"{len(findings)} finding(s)")
    return _findings_exit(findings, args)


def _cmd_project_help(args: argparse.Namespace) -> int:
    raise _ExitWith(EXIT["USAGE"],
                    "ERROR: akcli project needs a subcommand "
                    "(new | init | info | check) — see `akcli project --help`")


def _add_common_scaffold_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("directory", nargs="?", default=".", metavar="DIR",
                        help="project directory (default: .)")
    parser.add_argument("--name", metavar="NAME",
                        help="project name (default: the existing project/root "
                             "stem, else the directory name)")
    parser.add_argument("--paper", metavar="SIZE",
                        help="schematic paper size (default: A4)")
    parser.add_argument("--title", metavar="TEXT",
                        help="title block title for a newly written root sheet")
    parser.add_argument("--no-pcb", action="store_true",
                        help="skip the blank .kicad_pcb board container")
    parser.add_argument("--no-lib-tables", action="store_true",
                        help="skip the empty project sym-lib-table/fp-lib-table")
    parser.add_argument("--no-gitignore", action="store_true",
                        help="skip the KiCad .gitignore (an existing one is "
                             "never touched either way)")
    parser.add_argument("--sheets", metavar="A,B",
                        help="also create blank child sheets A.kicad_sch, "
                             "B.kicad_sch (instantiate them with an add_sheet op)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit 0 without writing")


def register(sub, common) -> None:
    from ..examples import names as _example_names

    p = sub.add_parser(
        "project", parents=[common],
        help="KiCad project scaffolding and integrity: create (new), complete "
             "(init), inspect (info) or gate (check) the .kicad_pro / "
             ".kicad_sch / .kicad_pcb / lib-table set")
    proj_sub = p.add_subparsers(dest="project_command", metavar="<subcommand>")
    p.set_defaults(handler=_cmd_project_help)

    pn = proj_sub.add_parser(
        "new", parents=[common],
        help="scaffold a complete KiCad project from scratch "
             "(refuses an existing project without --force)")
    _add_common_scaffold_flags(pn)
    pn.add_argument("--force", action="store_true",
                    help="overwrite existing project files (previous bytes are "
                         "backed up under .akcli/backups/)")
    pn.add_argument("--example", metavar="NAME",
                    help="also drop in a worked op-list as ops.json "
                         f"(one of: {', '.join(_example_names()) or 'none'})")
    pn.set_defaults(handler=_cmd_project_new)

    pi = proj_sub.add_parser(
        "init", parents=[common],
        help="add the project files a directory is MISSING; existing files "
             "are kept untouched (idempotent, no --force)")
    _add_common_scaffold_flags(pi)
    pi.add_argument("--root", metavar="FILE",
                    help="the root .kicad_sch the project should open "
                         "(default: the only one, else the tree root)")
    pi.set_defaults(handler=_cmd_project_init)

    pf = proj_sub.add_parser(
        "info", parents=[common],
        help="report what the .kicad_pro binds: root sheet, sheet tree, "
             "boards, lib tables, net classes, text variables, pinned libs")
    pf.add_argument("directory", nargs="?", default=".", metavar="DIR",
                    help="project directory or .kicad_pro (default: .)")
    pf.set_defaults(handler=_cmd_project_info)

    pc = proj_sub.add_parser(
        "check", parents=[common],
        help="project-integrity findings: malformed project file, root-sheet "
             "binding, missing child sheets/board, lib tables, ignored state")
    pc.add_argument("directory", nargs="?", default=".", metavar="DIR",
                    help="project directory or .kicad_pro (default: .)")
    _add_exit_policy_flags(pc)
    pc.set_defaults(handler=_cmd_project_check)
