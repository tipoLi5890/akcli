"""`akcli` write-side commands: ``new plan draw arrange undo ops``.

The blank-sheet bootstrap (``new``), the KiCad op-list executor (``draw`` writes
only on ``--apply``) with its before/after net-connectivity diff safety rails,
the auto-arrange nudger, the multi-level ``undo`` (rotated-backup swap/walk with
``--list``/``--steps``), and the ``ops`` authoring kit. Heavy imports stay LAZY
per handler.

**Shape (0.21 batch R4).** Every handler is four steps: read the argparse
``Namespace``, build the engine's request, call one function in
:mod:`..pipeline`, render the returned dataclass as text or ``--json``. The
work itself — staging, net gates, transactions, backup swaps — lives in the
engine layer; what stays here is the CLI contract: flags, the payload shapes
(:func:`_draw_result_payload` is the ONE author of the plan/draw envelope),
status lines, exit codes, and the journal entry's CLI-side provenance.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import contract as _contract
from ..errors import EXIT, AkcliError
from ._shared import (
    _did_you_mean,
    _draw_symbol_sources,
    _oplist_lib_ids,
    _dumps,
    _emit,
    _ExitWith,
    _load_cfg,
    _log,
    _require_path,
    _stamp,
)


def _warn(line: str) -> None:
    """The stderr sink handed to the engines (one line, newline added here).

    Engines describe what they would say; the CLI owns the stream. Keeping the
    write on this side is what lets a library caller silence or capture an
    engine's diagnostics.
    """
    sys.stderr.write(line + "\n")


def _log_sink(args: argparse.Namespace):
    """A verbosity-gated ``(level, message)`` sink bound to this invocation."""
    def _sink(level: int, msg: str) -> None:
        _log(args, level, msg)
    return _sink


def _backup_depth(cfg) -> int:
    """Resolve the rotated-backup depth from config (``[project] backup_depth``).

    Defaults to 3 and validates 1..99. ``cfg`` may not carry the attribute yet
    (config parsing owns the key); ``getattr`` keeps this forward-compatible.
    """
    depth = getattr(cfg, "backup_depth", None)
    if depth is None:
        return 3
    if isinstance(depth, bool) or not isinstance(depth, int) or not (1 <= depth <= 99):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: [project] backup_depth must be an integer 1..99")
    return depth


def _cmd_arrange(args: argparse.Namespace) -> int:
    """`arrange <sch>` — nudge FREE components until nothing overlaps.

    Dry-run by default (prints the planned moves); --apply executes them
    through the standard draw pipeline (.bak + connectivity re-verify), so
    `akcli undo` reverts an arrange like any other write. With ``--groups`` it
    instead relocates each functional block into its own shelf-packed region
    (rigid, net-preserving moves that carry each part's labels/wires).
    """
    target = _require_path(args.path)
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: arrange works on .kicad_sch")
    if getattr(args, "groups", None):
        return _cmd_arrange_groups(args, target)
    from .. import arrange as arrmod
    result = arrmod.plan(target,
                         grid=getattr(args, "grid", None) or arrmod.GRID_MIL,
                         margin=getattr(args, "margin", None) or arrmod.MARGIN_MIL)
    moves, stuck = result["moves"], result["anchored_overlaps"]
    do_apply = bool(getattr(args, "apply", False))
    base = {
        "symbols": result["symbols"], "clean": result["clean"],
        "moves": [{"designator": m.ref, "from": list(m.frm),
                   "to": list(m.to)} for m in moves],
        "anchored_overlaps": stuck,
    }
    if not args.json:
        if result["clean"]:
            _emit(f"arrange: {result['symbols']} symbols, no overlaps — clean")
        for m in moves:
            _emit(f"  move {m.ref}: ({m.frm[0]:g},{m.frm[1]:g}) -> "
                  f"({m.to[0]:g},{m.to[1]:g})")
        if stuck:
            _emit("  cannot auto-fix (wired/labeled or no free slot): "
                  + ", ".join(stuck))
    if not moves or not do_apply:
        if args.json:
            _emit(_dumps(_stamp({**base, "applied": False})))
        elif moves:
            _emit(f"dry-run: {len(moves)} move(s) planned — re-run with --apply")
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    from ..pipeline import arrange_apply
    cfg = _load_cfg(args, target)
    outcome = arrange_apply.apply_moves(
        target, [m.to_op() for m in moves],
        sources=_draw_symbol_sources(args, cfg),
        backup_depth=_backup_depth(cfg),
        allow_open=bool(getattr(args, "allow_open", False)),
        journal_cmd="arrange", note=getattr(args, "note", None))
    code = outcome.exit_code
    if args.json:
        _emit(_dumps(_stamp({
            **base, "applied": code == EXIT["OK"],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in outcome.findings
            ],
        })))
        return (EXIT["FINDINGS"] if stuck else EXIT["OK"]) \
            if code == EXIT["OK"] else code
    if code == EXIT["OK"]:
        _emit(f"arrange: applied {len(moves)} move(s) to {target.name}"
              + (f" — {len(stuck)} overlap(s) left for manual fixing"
                 if stuck else ""))
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    return code


def _write_label_proposal(target, groups: dict, out_path: str) -> None:
    """Write the ``--propose-labels`` draft and report what it does/does not cover.

    The draft itself (label-on-pin ops + the wire/junction/satellite fixes that
    make a re-pack net-safe) is built by :func:`..arrange.propose
    .propose_group_labels`; it NEVER touches the sheet — apply it with
    ``plan``/``draw`` like any op-list, then re-run ``arrange --groups``.
    """
    from ..arrange.propose import propose_group_labels
    proposal = propose_group_labels(target, groups)
    Path(out_path).write_text(_dumps(proposal.doc) + "\n", encoding="utf-8",
                              newline="\n")
    sys.stderr.write(
        f"propose-labels: {proposal.label_ops} label op(s) for "
        f"{proposal.spanning} multi-bundle net(s) + {proposal.fix_ops} "
        f"wire/junction/satellite fix op(s) -> {out_path}\n")
    if proposal.unnamed:
        sys.stderr.write(
            "propose-labels: NOT covered (unnamed multi-bundle nets — name "
            "them first): " + ", ".join(proposal.unnamed) + "\n")


def _cmd_arrange_groups(args: argparse.Namespace, target) -> int:
    """`arrange --groups <file>` — relocate functional blocks into tidy regions.

    Each group's parts (plus the power symbols riding on them) move as rigid
    bundles, shelf-packed inside the group with a wide channel between groups.
    Every move carries its labels/wires, so with label-on-pin connectivity the
    re-layout is net-preserving; the standard draw pipeline still re-verifies
    connectivity and REFUSES to write on any net change (with .bak + undo).
    """
    from .. import arrange as arrmod
    if args.groups == "@properties":
        # bare --groups: the sheet itself is the map (hidden Group property,
        # written by grouped ops — see resolve_groups / `akcli groups`)
        groups = arrmod.groups_from_properties(target)
        if not groups:
            raise _ExitWith(
                EXIT["USAGE"],
                "ERROR: no 'Group' symbol properties on this sheet; pass a "
                "--groups FILE or place components with a group tag first")
    else:
        from ..arrange.groupsfile import GroupsFileError, load_groups_file
        try:
            groups = load_groups_file(args.groups)
        except GroupsFileError as exc:
            raise _ExitWith(
                EXIT["NOT_FOUND"] if exc.missing else EXIT["USAGE"],
                f"ERROR: {exc}")
    if getattr(args, "propose_labels", None):
        _write_label_proposal(target, groups, args.propose_labels)
    cfg = _load_cfg(args, target)
    # layout policy: explicit flag > [arrange] config > built-in default
    arr = getattr(cfg, "arrange", None) or {}
    result = arrmod.plan_groups(
        target, groups,
        margin=(getattr(args, "margin", None)
                or arr.get("group_margin") or arrmod.GROUP_MARGIN_MIL),
        group_gap=(getattr(args, "group_gap", None)
                   or arr.get("group_gap") or arrmod.GROUP_GAP_MIL),
        row_width=(getattr(args, "row_width", None)
                   or arr.get("row_width") or arrmod.ROW_WIDTH_MIL),
        page_width=(getattr(args, "page_width", None) or arr.get("page_width")))
    moves, unplaced = result["moves"], result["unplaced"]
    do_apply = bool(getattr(args, "apply", False))
    base = {
        "symbols": result["symbols"], "clean": result["clean"],
        "groups": result["groups"], "unplaced": unplaced,
        "moves": [{"designator": m.ref, "from": list(m.frm), "to": list(m.to)}
                  for m in moves],
    }
    if not args.json:
        for g in result["groups"]:
            _emit(f"  group {g['group']}: {g['anchors']} part(s)")
        _emit(f"arrange --groups: {len(moves)} move(s) across "
              f"{len(result['groups'])} group(s)")
        if unplaced:
            _emit("  not placed (unknown/unattached): " + ", ".join(unplaced))
    if not moves or not do_apply:
        if args.json:
            _emit(_dumps(_stamp({**base, "applied": False})))
        elif moves:
            _emit(f"dry-run: {len(moves)} move(s) planned — re-run with --apply")
        return EXIT["FINDINGS"] if unplaced else EXIT["OK"]

    from ..pipeline import arrange_apply
    ops = [m.to_op(carry=True) for m in moves]
    sources = _draw_symbol_sources(args, cfg)

    # NET-PRESERVATION GATE — the --groups contract made mechanical: dry-apply
    # the rigid moves to a temp copy and require net EQUIVALENCE before
    # anything is written. Label-on-pin sheets pass by construction; a sheet
    # wired ACROSS group boundaries can split/merge nets when bundles
    # separate — refuse that loudly instead of stretching wires silently.
    check = arrange_apply.check_net_preservation(target, ops, sources=sources)
    if not check.equivalent and not getattr(args, "allow_net_changes", False):
        for ln in check.lines:
            sys.stderr.write(f"  {ln}\n")
        raise _ExitWith(
            EXIT["OPLIST"],
            "REFUSED: arrange --groups would change the netlist (see the "
            "lines above); nothing written. Wires crossing group boundaries "
            "cannot move rigidly — generate a label-on-pin fix with "
            "`--propose-labels OUT.json` (then plan/draw it and re-run), "
            "fix the group map, or pass --allow-net-changes to accept")

    allow_open = bool(getattr(args, "allow_open", False))
    outcome = arrange_apply.apply_moves(
        target, ops, sources=sources, backup_depth=_backup_depth(cfg),
        allow_open=allow_open, journal_cmd="arrange-groups",
        note=getattr(args, "note", None))
    code = outcome.exit_code
    frames_refreshed = 0
    if code == EXIT["OK"] and getattr(args, "frames", False):
        frames_refreshed = arrange_apply.refresh_frames(
            target, sources=sources, allow_open=allow_open, warn=_warn)
    if args.json:
        _emit(_dumps(_stamp({
            **base, "applied": code == EXIT["OK"],
            "frames_refreshed": frames_refreshed,
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in outcome.findings],
        })))
        return (EXIT["FINDINGS"] if unplaced else EXIT["OK"]) \
            if code == EXIT["OK"] else code
    if code == EXIT["OK"]:
        _emit(f"arrange --groups: applied {len(moves)} move(s) to {target.name}"
              + (f" — refreshed {frames_refreshed} frame(s)"
                 if frames_refreshed else ""))
        return EXIT["FINDINGS"] if unplaced else EXIT["OK"]
    _emit("status: REFUSED — the re-layout would change connectivity "
          "(nothing written); see the findings above")
    return code


# --------------------------------------------------------------------------- #
# undo (rotated-backup swap / walk / transaction restore)
# --------------------------------------------------------------------------- #
def _undo_exit(exc) -> int:
    """Exit code for an :class:`..pipeline.undo.UndoRefused`.

    "Nothing to restore" is the not-found class (4); refusing to overwrite an
    edit the user would not get back is an op-list-class refusal (6).
    """
    return EXIT["NOT_FOUND"] if exc.missing else EXIT["OPLIST"]


def _cmd_undo(args: argparse.Namespace) -> int:
    """`undo <target>` — restore the target from its rotated draw backups.

    `akcli draw --apply` leaves a stack `<name>.bak, .bak2, …` under the
    workspace's `.akcli/backups/` (legacy pre-0.12 stacks beside the file are
    still found).
    Default `undo --apply` swaps the target with `.bak`, so undo-twice is a redo
    (existing behaviour, byte-identical). `--steps N` walks back N snapshots
    while shifting the stack so a single redo still undoes the last step;
    `--list` prints the stack (mtimes/sizes) without touching anything.
    Dry-run by default (like draw).
    """
    target = _require_path(args.path, "target .kicad_sch")
    if getattr(args, "run", None):
        return _undo_run(args, target, args.run)
    if getattr(args, "list", False):
        return _undo_list(args, target)
    steps = getattr(args, "steps", None)
    if steps is None:
        steps = 1
    elif steps < 1:
        raise _ExitWith(EXIT["USAGE"], "ERROR: --steps must be a positive integer")
    if steps == 1:
        return _undo_swap(args, target)
    return _undo_walk(args, target, steps)


def _undo_run(args: argparse.Namespace, target, run_id: str) -> int:
    """``undo <target> --run <id>`` — all-or-nothing multi-file restore."""
    from ..pipeline import undo as undo_engine

    do_apply = bool(getattr(args, "apply", False))
    try:
        restore = undo_engine.restore_run(
            target, run_id, apply=do_apply,
            allow_open=bool(getattr(args, "allow_open", False)),
            note=getattr(args, "note", None))
    except undo_engine.UndoRefused as exc:
        raise _ExitWith(_undo_exit(exc), str(exc))
    entries = restore.entries
    payload = {
        "target": str(target), "run_id": run_id, "applied": do_apply,
        "files": [{"file": m.file, "backup": m.backup,
                   "restored": do_apply} for m in entries],
    }
    if args.json:
        _emit(_dumps(_stamp(payload)))
    else:
        verb = "restored" if do_apply else "would restore"
        _emit(f"undo --run {run_id}: {verb} {len(entries)} file(s):")
        for m in entries:
            _emit(f"  {m.file}")
        if not do_apply:
            _emit("status: dry-run — nothing written (re-run with --apply)")
        else:
            _emit(f"status: APPLIED — run {run_id} swapped (same command again = redo)")
    return EXIT["OK"]


def _undo_list(args: argparse.Namespace, target) -> int:
    """`undo --list` — show the rotated-backup stack with sizes and mtimes."""
    from ..pipeline import undo as undo_engine

    entries = [{"level": e.level, "backup": e.path.name, "path": str(e.path),
                "size": e.size, "mtime": e.mtime}
               for e in undo_engine.list_backups(target)]
    if args.json:
        _emit(_dumps(_stamp({"target": str(target), "depth": len(entries),
                             "backups": entries})))
        return EXIT["OK"]
    if not entries:
        _emit(f"undo: no backups for {target.name} "
              "(created by `akcli draw --apply`)")
        return EXIT["OK"]
    _emit(f"undo stack for {target.name} ({len(entries)} snapshot(s), newest first):")
    for e in entries:
        _emit(f"  [{e['level']}] {e['backup']:<24} {e['size']:>8} B  {e['mtime']}")
    return EXIT["OK"]


def _undo_swap(args: argparse.Namespace, target) -> int:
    """Single-step undo: swap the target with the NEWEST backup on the stack."""
    from ..pipeline import undo as undo_engine

    try:
        out = undo_engine.swap(
            target, apply=bool(getattr(args, "apply", False)),
            allow_open=bool(getattr(args, "allow_open", False)),
            note=getattr(args, "note", None))
    except undo_engine.UndoRefused as exc:
        raise _ExitWith(_undo_exit(exc), str(exc))
    bak, summary = out.backup, out.summary
    if not out.applied:
        if args.json:
            _emit(_dumps(_stamp({"applied": False, "target": str(target),
                                 "backup": str(bak), "steps": 1,
                                 "summary": summary})))
        else:
            _emit(f"undo (dry-run): would restore {bak.name}\n  {summary}\n"
                  "re-run with --apply to swap (undo again = redo)")
        return EXIT["OK"]
    if args.json:
        _emit(_dumps(_stamp({"applied": True, "target": str(target),
                             "backup": str(bak), "steps": 1,
                             "summary": summary})))
    else:
        _emit(f"undo: restored {target.name} from backup — {summary}\n"
              f"previous content kept at {bak.name} (undo again = redo)")
    return EXIT["OK"]


def _undo_walk(args: argparse.Namespace, target, steps: int) -> int:
    """Multi-step undo: restore the snapshot ``steps`` back, keeping redo alive."""
    from ..pipeline import undo as undo_engine

    try:
        out = undo_engine.walk(
            target, steps, apply=bool(getattr(args, "apply", False)),
            allow_open=bool(getattr(args, "allow_open", False)),
            note=getattr(args, "note", None))
    except undo_engine.UndoRefused as exc:
        raise _ExitWith(_undo_exit(exc), str(exc))
    steps, bak, summary = out.steps, out.backup, out.summary
    if not out.applied:
        if args.json:
            _emit(_dumps(_stamp({"applied": False, "target": str(target),
                                 "backup": str(bak),
                                 "steps": steps, "summary": summary})))
        else:
            _emit(f"undo (dry-run): would walk back {steps} step(s) to "
                  f"{bak.name}\n  {summary}\n"
                  "re-run with --apply (a single `undo` afterwards redoes the last step)")
        return EXIT["OK"]
    if args.json:
        _emit(_dumps(_stamp({"applied": True, "target": str(target),
                             "backup": str(bak),
                             "steps": steps, "summary": summary})))
    else:
        _emit(f"undo: walked back {steps} step(s), restored {target.name} — {summary}\n"
              f"stack reversed; a single `undo` redoes the last step")
    return EXIT["OK"]


# --------------------------------------------------------------------------- #
# new (blank-sheet bootstrap)
# --------------------------------------------------------------------------- #
def _cmd_new(args: argparse.Namespace) -> int:
    """`new <file.kicad_sch>` — bootstrap a minimal blank schematic.

    Writes a valid empty sheet so `draw` has something to append to (a blank
    sheet no longer has to be hand-written). Refuses an existing file without
    `--force`. Standard status line; `--json` envelope. The document itself is
    rendered by the writer (:mod:`..writers.blank`), which shares the one
    s-expression quoting authority.
    """
    from ..writers.blank import KICAD_PAPERS, blank_sch

    target = _require_path(args.path, "output .kicad_sch")
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: new writes a .kicad_sch file")
    paper = getattr(args, "paper", None) or "A4"
    if paper not in KICAD_PAPERS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown paper {paper!r} "
                        f"(one of: {', '.join(KICAD_PAPERS)})")
    if target.exists() and not getattr(args, "force", False):
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: {target} exists (use --force to overwrite)")
    text = blank_sch(paper, getattr(args, "title", None))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    status_line = (f"status: CREATED — wrote {target.name} "
                   f"(blank {paper} sheet; `akcli draw` to add parts)")
    if args.json:
        _emit(_dumps(_stamp({"created": True, "target": str(target),
                             "paper": paper,
                             "title": getattr(args, "title", None) or None,
                             "status": "created"})))
    else:
        _emit(status_line)
        _project_hint(target)
    return EXIT["OK"]


def _project_hint(target: Path) -> None:
    """stderr nudge when a fresh sheet is not part of a KiCad project.

    A lone ``.kicad_sch`` is everything *akcli* needs, which is exactly why
    this gap stayed invisible: KiCad opening it prompts to create a project,
    finds no library tables and no board. One line, stderr (a log, not data),
    text mode only — the JSON caller is a program that did not ask.
    """
    directory = target.parent if str(target.parent) else Path(".")
    try:
        if any(directory.glob("*.kicad_pro")):
            return
    except OSError:
        return
    sys.stderr.write(
        f"note: no .kicad_pro next to {target.name} — KiCad will open this as "
        f"a loose sheet; run `akcli project init {directory}` to make it a "
        "real project (adds the project file, lib tables and a blank board)\n")


def _cmd_ops(args: argparse.Namespace) -> int:
    """`ops list` / `ops template <op>` / `ops from-intent` — the authoring kit."""
    from .. import ops as opsmod

    action = getattr(args, "action", None)
    if action == "from-intent":
        return _cmd_ops_from_intent(args)
    if action == "list":
        try:
            caps = opsmod.load_capabilities()["ops"]  # packaged mirror + repo fallback
        except Exception:
            caps = {}
        if args.json:
            _emit(_dumps({
                "protocol_version": opsmod.PROTOCOL_VERSION,
                # "altium" matches ops.capabilities.json's executor key (an
                # informational column: Altium is import-only, there is no
                # Altium write executor)
                "ops": [{"name": name,
                         "required": list(opsmod._OP_REQUIRED.get(name, [])),
                         "kicad": (caps.get(name) or {}).get("kicad"),
                         "altium": (caps.get(name) or {}).get("altium")}
                        for name in sorted(opsmod.OP_NAMES)],
                "macros": [{"name": name,
                            "required": list(opsmod.MACRO_REQUIRED.get(name, []))}
                           for name in sorted(opsmod.MACRO_OPS)],
            }))
            return EXIT["OK"]
        lines = []
        for name in sorted(opsmod.OP_NAMES):
            required = ", ".join(opsmod._OP_REQUIRED.get(name, []))
            support = ""
            entry = caps.get(name)
            if entry:
                support = "  [kicad:" + ("yes" if entry.get("kicad") else "no")                           + " altium:" + ("yes" if entry.get("altium") else "no") + "]"
            lines.append(f"{name:26} required: {required or '-'}{support}")
        lines.append("-- macros (expanded to core ops before plan/draw; "
                     "label-on-pin connectivity) --")
        for name in sorted(opsmod.MACRO_OPS):
            required = ", ".join(opsmod.MACRO_REQUIRED.get(name, []))
            lines.append(f"{name:26} required: {required or '-'}")
        _emit("\n".join(lines))
        return EXIT["OK"]
    if action == "template":
        name = getattr(args, "opname", None)
        if not name:
            raise _ExitWith(EXIT["USAGE"], "ERROR: ops template needs an op name")
        try:
            op = opsmod.op_template(name, include_optional=not getattr(args, "required_only", False))
        except KeyError:
            raise _ExitWith(
                EXIT["USAGE"],
                f"ERROR: unknown op {name!r}"
                f"{_did_you_mean(name, opsmod.OP_NAMES | opsmod.MACRO_OPS)} "
                "(see `akcli ops list`)",
            )
        doc = {
            "protocol_version": opsmod.PROTOCOL_VERSION,
            "target_format": "kicad",
            "target_file": "<board.kicad_sch>",
            "ops": [op],
        }
        _emit(_dumps(doc))
        return EXIT["OK"]
    if action == "validate":
        opsfile = getattr(args, "opsfile", None)
        if not opsfile:
            raise _ExitWith(EXIT["USAGE"], "ERROR: ops validate needs an op-list file")
        # target-free structural validation: envelope + per-op fields (macros
        # included — prepare_oplist validates BEFORE it expands) + the
        # expansion itself, exactly the checks plan/draw run before touching a
        # target
        errors: list[dict] = []
        oplist = None
        try:
            prepared = opsmod.prepare_oplist(opsmod.load_oplist(opsfile))
            oplist = prepared.doc
            errors.extend(
                {"op_index": e.op_index, "code": e.code, "message": e.message}
                for e in prepared.errors)
        except AkcliError as exc:
            errors.append({"op_index": None, "code": exc.code,
                           "message": exc.message or str(exc)})
        valid = not errors
        if args.json:
            _emit(_dumps({
                "protocol_version": opsmod.PROTOCOL_VERSION,
                "valid": valid,
                "ops_sha256": _ops_sha256(opsfile),
                "op_count": len((oplist or {}).get("ops", []) or []),
                "errors": errors,
            }))
        else:
            if valid:
                _emit(f"ops validate: OK — "
                      f"{len((oplist or {}).get('ops', []) or [])} op(s)")
            else:
                for e in errors:
                    idx = "" if e["op_index"] is None else f"[{e['op_index']}] "
                    _emit(f"ERROR: {idx}{e['code']}: {e['message']}")
        return EXIT["OK"] if valid else EXIT["OPLIST"]
    raise _ExitWith(EXIT["USAGE"],
                    "ERROR: use `akcli ops list`, `akcli ops template <op>`, "
                    "`akcli ops validate <oplist.json>` or "
                    "`akcli ops from-intent <design.json>`")


def _cmd_ops_from_intent(args: argparse.Namespace) -> int:
    """`ops from-intent <design.json>` — design document -> complete op-list.

    The design doc is the ``check --intent`` nets table plus ``components``
    (+ optional ``layout``); the generator owns all geometry and emits only
    the existing op vocabulary (``place_component`` + label-on-pin
    ``add_net_label`` + ``add_no_connect``), so the output flows through the
    ordinary ``plan``/``draw`` pipeline — and the SAME design file is the
    ``check --intent`` assertion baseline for the drawn sheet. Symbol
    resolution uses the exact ``plan``/``draw`` chain (``--symbols`` >
    config ``[paths]`` > lib-table fallback for the needed lib_ids).
    """
    design_path = getattr(args, "design", None)
    if not design_path:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: ops from-intent needs a design JSON file "
                        "(see schemas/design-intent.schema.json)")
    from .. import intentgen
    from ..writers.lib_cache import _coerce_sources

    design = intentgen.load_design(design_path)   # AkcliError -> cli.main
    cfg = _load_cfg(args, None)
    lib_ids = sorted({c.lib_id for c in design.components.values()})
    sources = _draw_symbol_sources(
        args, cfg, lib_ids=lib_ids,
        project_dir=Path(design_path).resolve().parent)
    oplist = intentgen.generate(design, _coerce_sources(sources))
    summary = (f"from-intent: {len(design.components)} component(s), "
               f"{len(design.nets)} net(s) -> {len(oplist['ops'])} op(s)")
    out = getattr(args, "out", None)
    if out:
        Path(out).write_text(_dumps(oplist) + "\n", encoding="utf-8",
                             newline="\n")
        sys.stderr.write(
            f"{summary} -> {out}\n"
            f"next: `akcli plan <target.kicad_sch> --ops {out}` "
            "(then draw --apply; assert with `akcli check <target> "
            f"--intent {design_path}`)\n")
    else:
        _emit(_dumps(oplist))
        sys.stderr.write(summary + "\n")
    return EXIT["OK"]


# --------------------------------------------------------------------------- #
# plan / draw (KiCad op-list executor)
# --------------------------------------------------------------------------- #
def _draw_results_text(results: list, findings: list) -> str:
    """Render per-op results + connectivity findings as a human summary."""
    lines = [f"# ops ({len(results)})"]
    for r in results:
        uuids = f" -> {r.created_uuids}" if r.created_uuids else ""
        if r.status == "ok":
            lines.append(f"  ok    [{r.op_index}] {r.op}{uuids}")
        else:
            lines.append(f"  ERROR [{r.op_index}] {r.op}: {r.error_code}: {r.message}")
            if getattr(r, "remediation", None):
                lines.append(f"        hint: {r.remediation}")
    lines.append(f"# connectivity ({len(findings)})")
    if not findings:
        lines.append("  (clean)")
    for f in findings:
        lines.append(f"  {f.severity.value.upper()} [{f.code}] {f.message}")
    return "\n".join(lines)


def _ops_sha256(ops_path: str | None) -> str | None:
    """sha256 of the op-list file bytes (journal + hook provenance), or None."""
    if not ops_path:
        return None
    import hashlib
    from pathlib import Path
    try:
        return hashlib.sha256(Path(ops_path).read_bytes()).hexdigest()
    except OSError:
        return None


def _draw_result_payload(*, applied: bool, status: str, ops: list[dict],
                         connectivity: list, net_diff, preview=None,
                         project_root=None, txn=None) -> dict:
    """The ONE author of the plan/draw ``--json`` payload.

    Every plan/draw exit path (normal run AND structural op-list refusal)
    routes through here, so the shape can never fork from
    ``draw-result.schema.json`` in just one branch. ``project_root`` /
    ``evaluation_scope`` appear ONLY when the net gate ran over a sheet tree
    (absent, not null, on the flat path — flat output stays byte-identical).
    """
    payload = {
        "schema_version": _contract.SCHEMA_VERSIONS["draw-result"],
        "applied": applied,
        "status": status,
        "ops": ops,
        "connectivity": [
            {"code": f.code, "severity": f.severity.value, "message": f.message}
            for f in connectivity
        ],
        "net_diff": net_diff,
        "preview": preview,
    }
    if project_root is not None:
        payload["project_root"] = project_root
        payload["evaluation_scope"] = "tree"
    if txn is not None:
        # Multi-sheet transaction summary (absent — not null — on the
        # single-file path, whose payload stays byte-identical).
        payload["run_id"] = txn["run_id"]
        payload["files"] = txn["files"]
    return payload


def _oplist_errors_exit(args: argparse.Namespace, errs: list) -> int:
    """Report a structurally invalid op-list (nothing was staged or written)."""
    if args.json:
        # Same draw-result shape as a normal run, so an agent parses ONE
        # schema on every plan/draw exit path (status "refused", the
        # structural errors as per-op error results with remediation).
        # OpError's document-level sentinel is op_index -1; the schema
        # (and the per-op shape) require >= 0, so clamp it to 0.
        from ..errors import remediation_for
        from ..writers import kicad as kwriter
        results = [
            kwriter.OpResult(
                op_index=max(e.op_index, 0), op=None, status="error",
                error_code=e.code, message=e.message,
                remediation=remediation_for(e.code)).to_dict()
            for e in errs
        ]
        _emit(_dumps(_draw_result_payload(
            applied=False, status="refused", ops=results,
            connectivity=[], net_diff=None)))
    for e in errs:
        sys.stderr.write(f"ERROR: [{e.op_index}] {e.code}: {e.message}\n")
    return EXIT["OPLIST"]


def _run_draw(args: argparse.Namespace, do_apply: bool) -> int:
    """Shared plan/draw driver: validate + (dry-)apply an op-list to a .kicad_sch."""
    target = _require_path(args.target, "target .kicad_sch")
    if not getattr(args, "ops", None):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing --ops <oplist.json>")

    from ..ops import load_oplist, prepare_oplist
    from ..pipeline.draw import DrawRequest
    from ..pipeline.draw import run as run_pipeline

    # FileNotFound / malformed-JSON / bad-macro / bad-group failures raise
    # AkcliError and surface via cli.main — which renders them as a
    # machine-readable {"error": {...}} envelope under --json (never a bare
    # non-JSON stdout). prepare_oplist owns the order: validate the author's
    # ops (so a macro's own fields are checked), expand macros (propagating
    # each op's `group` tag), resolve group-local coordinates, validate the
    # result — with error indices mapped back onto the author's op-list.
    prepared = prepare_oplist(load_oplist(args.ops))
    if prepared.errors:
        return _oplist_errors_exit(args, list(prepared.errors))
    oplist = prepared.doc

    cfg = _load_cfg(args, target)
    # lib_ids let the lib-table fallback resolve a nickname (Device:R) that
    # neither --symbols nor the config [paths] provide
    sources = _draw_symbol_sources(args, cfg, lib_ids=_oplist_lib_ids(oplist),
                                   project_dir=target.parent)

    outcome = run_pipeline(DrawRequest(
        target=target, oplist=oplist, sources=sources,
        warn=_warn, log=_log_sink(args),
        do_apply=do_apply,
        backup_depth=_backup_depth(cfg),
        allow_open=bool(getattr(args, "allow_open", False)),
        strict_nets=bool(getattr(args, "strict_nets", False)),
        no_net_diff=bool(getattr(args, "no_net_diff", False)),
        render=getattr(args, "render", None),
        root=(Path(args.root) if getattr(args, "root", None) else None),
        flat=bool(getattr(args, "flat", False)),
        no_erc=bool(getattr(args, "no_erc", False)),
    ))
    if outcome.refusal is not None:
        for ln in outcome.refusal.lines:
            sys.stderr.write(f"  {ln}\n")
        raise _ExitWith(outcome.refusal.exit_code, outcome.refusal.message)

    gate = outcome.gate
    results, findings = outcome.results, outcome.findings
    net_lines, txn_report = gate.net_lines, outcome.txn_report
    code = outcome.exit_code
    show_diff = not getattr(args, "no_net_diff", False)
    if not do_apply:
        hint = " (re-run with --apply)" if getattr(args, "command", "") == "draw" else ""
        status = "dry-run"
        status_line = f"status: dry-run — nothing written{hint}"
    elif code == EXIT["OK"]:
        status = "applied"
        if txn_report is not None and txn_report.get("backup"):
            written = [f["file"].rsplit("/", 1)[-1]
                       for f in txn_report["files"] if f["written"]]
            status_line = (
                f"status: APPLIED — wrote {len(written)} file(s) "
                f"({', '.join(written)}) in one transaction (backup "
                f".akcli/backups/{txn_report['backup']}; `akcli undo "
                f"{target.name} --run {txn_report['run_id']} --apply` reverts)")
        else:
            from ..writers.backups import backup_label
            status_line = (f"status: APPLIED — wrote {target.name} "
                           f"(backup {backup_label(target)}; `akcli undo` reverts)")
    else:
        status = "refused"
        status_line = "status: REFUSED — nothing written (fix the errors above)"

    if args.json:
        _emit(_dumps(_draw_result_payload(
            applied=bool(do_apply and code == EXIT["OK"]),
            status=status,
            ops=[r.to_dict() for r in results],
            connectivity=findings,
            net_diff=None if (net_lines is None or not show_diff) else {
                "equivalent": gate.net_equiv, "risk": gate.net_risk,
                "lines": net_lines,
            },
            preview=gate.preview,
            project_root=(gate.tree_root.as_posix()
                          if gate.tree_root is not None else None),
            txn=txn_report)))
    else:
        _emit(_draw_results_text(results, findings))
        if show_diff and net_lines is not None:
            _emit("Net changes:")
            if not net_lines:
                _emit("  (none)")
            for ln in net_lines:
                _emit(f"  {ln}")
        if gate.preview is not None:
            _emit(f"preview: {gate.preview['path']} ({gate.preview['bytes']} bytes)")
        _emit(status_line)

    _journal_draw(args, target, status, oplist, gate, txn_report)
    return code


def _journal_draw(args: argparse.Namespace, target, status: str, oplist: dict,
                  gate, txn_report) -> None:
    """Record the write in the workspace journal (CLI-side provenance).

    The verb the user typed and the op-list's sha256 are CLI facts, not engine
    facts, which is why the plan/draw entry is written here rather than inside
    the pipeline.
    """
    from .. import journal as _journal
    from ..writers.backups import backup_label

    net_diff = (None if gate.net_lines is None
                else {"equivalent": gate.net_equiv, "risk": gate.net_risk})
    if txn_report is not None:
        _journal.record(
            target, getattr(args, "command", "draw") or "draw", status,
            ops_sha256=_ops_sha256(args.ops),
            op_count=len(oplist.get("ops", []) or []),
            net_diff=net_diff,
            note=getattr(args, "note", None),
            run_id=txn_report["run_id"],
            files=[f["file"] for f in txn_report["files"] if f["dirty"]],
            backup=(f".akcli/backups/{txn_report['backup']}"
                    if status == "applied" and txn_report.get("backup")
                    else None),
        )
    else:
        _journal.record(
            target, getattr(args, "command", "draw") or "draw", status,
            ops_sha256=_ops_sha256(args.ops),
            op_count=len(oplist.get("ops", []) or []),
            net_diff=net_diff,
            note=getattr(args, "note", None),
            backup=(backup_label(target) if status == "applied" else None),
        )


def _cmd_plan(args: argparse.Namespace) -> int:
    """Validate + dry-run an op-list (per-op preview + connectivity); never writes."""
    return _run_draw(args, do_apply=False)


def _cmd_draw(args: argparse.Namespace) -> int:
    """Apply an op-list to a .kicad_sch (dry-run unless --apply)."""
    do_apply = bool(getattr(args, "apply", False))
    if do_apply and getattr(args, "dry_run", False):
        # --dry-run is an explicit safety request: it wins over --apply.
        sys.stderr.write("WARNING: both --apply and --dry-run given; "
                         "honoring --dry-run (nothing will be written)\n")
        do_apply = False
    return _run_draw(args, do_apply=do_apply)


def register(sub, common) -> None:
    p = sub.add_parser("arrange", parents=[common],
                       help="nudge free (unwired/unlabeled) components until "
                            "no symbols overlap (dry-run unless --apply)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to arrange")
    p.add_argument("--apply", action="store_true",
                   help="write the moves (default: preview only)")
    p.add_argument("--groups", metavar="FILE", nargs="?", const="@properties",
                   help="relocate functional blocks: a TOML/JSON map of "
                        "group-name -> [refdes, ...] (net-preserving rigid "
                        "moves). Bare --groups derives the map from the "
                        "sheet's hidden Group properties")
    p.add_argument("--frames", action="store_true",
                   help="with --groups: redraw each group's border frame + "
                        "title after packing (keyed uuids replace stale "
                        "frames in place)")
    p.add_argument("--group-gap", dest="group_gap", type=float, metavar="MIL",
                   help="channel between group blocks, both axes with "
                        "--page-width (default 1200; [arrange] group_gap)")
    p.add_argument("--row-width", dest="row_width", type=float, metavar="MIL",
                   help="wrap a group's shelf past this width (default 4000)")
    p.add_argument("--page-width", dest="page_width", type=float, metavar="MIL",
                   help="with --groups: pack group blocks side by side in 2D, "
                        "wrapping past this width (default: single column; "
                        "[arrange] page_width)")
    p.add_argument("--allow-net-changes", dest="allow_net_changes",
                   action="store_true",
                   help="with --groups --apply: write even when the rigid "
                        "re-layout would change net membership (default: "
                        "refuse — the re-layout must be net-preserving)")
    p.add_argument("--propose-labels", dest="propose_labels",
                   metavar="OUT.json",
                   help="with --groups: write a label-on-pin op-list draft "
                        "covering every named net that spans groups, so the "
                        "re-pack stops depending on cross-group wires (apply "
                        "it via plan/draw, then re-run arrange)")
    p.add_argument("--grid", type=float, metavar="MIL",
                   help="nudge step in mils (default 100)")
    p.add_argument("--margin", type=float, metavar="MIL",
                   help="clearance between symbols/bundles in mils "
                        "(default 50; 200 with --groups)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra symbol source for the write pipeline")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="write even when a KiCad GUI lock file is present "
                        "(File>Revert in KiCad afterwards)")
    p.add_argument("--note", metavar="TEXT",
                   help="free-form intent note recorded in the workspace "
                        "journal (why this edit was made)")
    p.set_defaults(handler=_cmd_arrange)

    p = sub.add_parser("new", parents=[common],
                       help="bootstrap a minimal blank .kicad_sch that draw "
                            "can immediately append to")
    p.add_argument("path", nargs="?", help="the .kicad_sch to create")
    p.add_argument("--paper", metavar="SIZE", default="A4",
                   help="paper size (A4|A3|... default A4)")
    p.add_argument("--title", metavar="T", help="title_block title")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing file")
    p.set_defaults(handler=_cmd_new)

    p = sub.add_parser("undo", parents=[common],
                       help="restore a .kicad_sch from its rotated draw backups "
                            "(.akcli/backups/<name>.bak..bakN; undo twice = redo)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to restore")
    p.add_argument("--apply", action="store_true",
                   help="actually swap (default is a dry-run preview)")
    p.add_argument("--steps", type=int, metavar="N",
                   help="walk back N snapshots (default 1; a single undo afterwards "
                        "redoes the last step)")
    p.add_argument("--list", action="store_true",
                   help="show the backup stack (mtimes/sizes) and exit")
    p.add_argument("--run", metavar="RUN_ID",
                   help="restore ALL files of a multi-sheet draw transaction "
                        "(all-or-nothing; same command again = redo) from "
                        ".akcli/backups/runs/<RUN_ID>/")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="swap even when a KiCad GUI lock file is present "
                        "(File>Revert in KiCad afterwards)")
    p.add_argument("--note", metavar="TEXT",
                   help="free-form intent note recorded in the workspace "
                        "journal (why this edit was made)")
    p.set_defaults(handler=_cmd_undo)

    p = sub.add_parser("ops", parents=[common],
                       help="op-list authoring kit (list ops, emit op templates)")
    ops_sub = p.add_subparsers(dest="action", metavar="<action>")
    pol = ops_sub.add_parser("list", parents=[common],
                             help="list the op vocabulary with required fields")
    pol.set_defaults(handler=_cmd_ops, action="list")
    pot = ops_sub.add_parser("template", parents=[common],
                             help="emit a fill-in JSON op-list skeleton for one op")
    pot.add_argument("opname", nargs="?", help="op name, e.g. place_component")
    pot.add_argument("--required-only", action="store_true",
                     help="omit optional fields from the skeleton")
    pot.set_defaults(handler=_cmd_ops, action="template")
    pov = ops_sub.add_parser("validate", parents=[common],
                             help="validate an op-list file structurally "
                                  "(no target needed; exit 6 on any problem)")
    pov.add_argument("opsfile", nargs="?", help="op-list JSON file")
    pov.set_defaults(handler=_cmd_ops, action="validate")
    pof = ops_sub.add_parser("from-intent", parents=[common],
                             help="generate a complete op-list from a design "
                                  "document (the check --intent pin->net "
                                  "table + components + layout hints)")
    pof.add_argument("design", nargs="?",
                     help="design JSON file "
                          "(see schemas/design-intent.schema.json)")
    pof.add_argument("-o", "--out", metavar="OUT.json",
                     help="write the op-list here (default: stdout)")
    pof.add_argument("--symbols", metavar="PATH", action="append",
                     help="extra .kicad_sym / template .kicad_sch symbol "
                          "source (repeatable)")
    pof.set_defaults(handler=_cmd_ops, action="from-intent")
    p.set_defaults(handler=_cmd_ops)

    p = sub.add_parser("plan", parents=[common],
                       help="validate + dry-run an op-list against a .kicad_sch (never writes)")
    p.add_argument("target", nargs="?", help="target .kicad_sch file")
    p.add_argument("--ops", metavar="FILE", help="op-list JSON file")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.add_argument("--no-net-diff", action="store_true",
                   help="skip the before/after net connectivity diff")
    p.add_argument("--root", metavar="FILE",
                   help="root .kicad_sch of the sheet tree to evaluate the net "
                        "diff over (auto-discovered when exactly one root "
                        "instantiates the target)")
    p.add_argument("--flat", action="store_true",
                   help="evaluate the target file alone, ignoring any parent "
                        "sheet tree (the truncated single-file net view)")
    p.add_argument("--render", metavar="OUT.svg",
                   help="render the WOULD-BE sheet (dry-applied to a temp "
                        "copy) to an SVG preview — look before you --apply")
    p.add_argument("--note", metavar="TEXT",
                   help="free-form intent note recorded in the workspace "
                        "journal (why this edit was made)")
    p.set_defaults(handler=_cmd_plan)

    p = sub.add_parser("draw", parents=[common],
                       help="apply an op-list to a .kicad_sch (dry-run unless --apply)")
    p.add_argument("target", nargs="?", help="target .kicad_sch file")
    p.add_argument("--ops", metavar="FILE", help="op-list JSON file")
    p.add_argument("--apply", action="store_true",
                   help="actually write (default is dry-run: verify only)")
    p.add_argument("--dry-run", action="store_true",
                   help="verify only, do not write (default)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.add_argument("--no-net-diff", action="store_true",
                   help="skip the before/after net connectivity diff")
    p.add_argument("--root", metavar="FILE",
                   help="root .kicad_sch of the sheet tree to evaluate the net "
                        "diff over (auto-discovered when exactly one root "
                        "instantiates the target)")
    p.add_argument("--flat", action="store_true",
                   help="evaluate the target file alone, ignoring any parent "
                        "sheet tree (the truncated single-file net view)")
    p.add_argument("--strict-nets", action="store_true",
                   help="with --apply: refuse to write when the net diff shows "
                        "a split/merge touching a named net")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="write even when a KiCad GUI lock file (~<name>.lck) is "
                        "present — you accept that a GUI save may overwrite the "
                        "edit; File>Revert in KiCad after applying")
    p.add_argument("--no-erc", dest="no_erc", action="store_true",
                   help="skip the advisory post-apply kicad-cli ERC run "
                        "(akcli's own connectivity gate still runs)")
    p.add_argument("--render", metavar="OUT.svg",
                   help="render the resulting sheet (dry-applied to a temp "
                        "copy) to an SVG preview — look before you --apply")
    p.add_argument("--note", metavar="TEXT",
                   help="free-form intent note recorded in the workspace "
                        "journal (why this edit was made)")
    p.set_defaults(handler=_cmd_draw)
