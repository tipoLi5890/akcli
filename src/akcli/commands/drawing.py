"""`akcli` write-side commands: ``new plan draw arrange undo ops``.

The blank-sheet bootstrap (``new``), the KiCad op-list executor (``draw`` writes
only on ``--apply``) with its before/after net-connectivity diff safety rails,
the auto-arrange nudger, the multi-level ``undo`` (rotated-backup swap/walk with
``--list``/``--steps``), and the ``ops`` authoring kit. Heavy imports stay LAZY
per handler.
"""

from __future__ import annotations

import argparse
import sys

from ..errors import EXIT
from ._shared import (
    _did_you_mean,
    _draw_exit,
    _draw_symbol_sources,
    _dumps,
    _emit,
    _ExitWith,
    _load_cfg,
    _log,
    _require_path,
)


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
    `akcli undo` reverts an arrange like any other write.
    """
    target = _require_path(args.path)
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: arrange works on .kicad_sch")
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
            _emit(_dumps({**base, "applied": False}))
        elif moves:
            _emit(f"dry-run: {len(moves)} move(s) planned — re-run with --apply")
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    from ..writers import kicad as kwriter
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": target.name,
              "ops": [m.to_op() for m in moves]}
    cfg = _load_cfg(args, target)
    findings: list = []
    results = kwriter.apply(oplist, str(target), apply=True,
                            sources=_draw_symbol_sources(args, cfg),
                            verify_out=findings, backup_dir=target.parent,
                            backup_depth=_backup_depth(cfg),
                            allow_open=bool(getattr(args, "allow_open", False)))
    code = _draw_exit(results, findings)
    if args.json:
        _emit(_dumps({
            **base, "applied": code == EXIT["OK"],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
            ],
        }))
        return (EXIT["FINDINGS"] if stuck else EXIT["OK"]) \
            if code == EXIT["OK"] else code
    if code == EXIT["OK"]:
        _emit(f"arrange: applied {len(moves)} move(s) to {target.name}"
              + (f" — {len(stuck)} overlap(s) left for manual fixing"
                 if stuck else ""))
        return EXIT["FINDINGS"] if stuck else EXIT["OK"]
    return code


def _bak_name(name: str, level: int) -> str:
    """Rotated-backup filename: level 1 is ``<name>.bak``, 2 ``.bak2``, …"""
    return f"{name}.bak" if level <= 1 else f"{name}.bak{level}"


_BACKUP_SCAN_MAX = 99          # matches the config backup_depth ceiling


def _backup_stack(target) -> list:
    """Every rotated backup for ``target``, newest first: ``[(path, level), …]``.

    Gap-TOLERANT: a missing level (e.g. a crash between the rotation renames
    and the fresh ``.bak`` copy) must not hide the deeper snapshots that still
    exist — ``undo --list``/``--steps`` walk whatever is really on disk, in
    level order.
    """
    return [
        (p, level)
        for level in range(1, _BACKUP_SCAN_MAX + 1)
        if (p := target.parent / _bak_name(target.name, level)).exists()
    ]


def _undo_summary(cur, old) -> str:
    """One-line parts/nets delta between the current file and a restore target."""
    from ..checks import diff as diffmod
    rep = diffmod.run(cur, old)
    return (f"{len(cur.components)} parts/{len(cur.nets)} nets -> "
            f"{len(old.components)} parts/{len(old.nets)} nets "
            f"(+{len(rep.added_components)} −{len(rep.removed_components)} "
            f"components, {len(rep.member_changed_nets)} nets change membership)")


def _refuse_if_gui_open(args: argparse.Namespace, target) -> None:
    """Refuse a file swap while the KiCad GUI holds ``target`` open (no --allow-open)."""
    from ..writers import kicad as kwriter
    if getattr(args, "allow_open", False):
        return
    lck = kwriter.gui_lock_path(target)
    if lck.exists():
        raise _ExitWith(
            EXIT["OPLIST"],
            f"ERROR: TARGET_LOCKED: {target.name} appears open in the KiCad GUI "
            f"(found {lck.name}); close it first, or pass --allow-open and "
            "File>Revert in KiCad afterwards")


def _cmd_undo(args: argparse.Namespace) -> int:
    """`undo <target>` — restore the target from its rotated draw backups.

    `akcli draw --apply` leaves a stack `<name>.bak, .bak2, …` beside the file.
    Default `undo --apply` swaps the target with `.bak`, so undo-twice is a redo
    (existing behaviour, byte-identical). `--steps N` walks back N snapshots
    while shifting the stack so a single redo still undoes the last step;
    `--list` prints the stack (mtimes/sizes) without touching anything.
    Dry-run by default (like draw).
    """
    target = _require_path(args.path, "target .kicad_sch")
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


def _undo_list(args: argparse.Namespace, target) -> int:
    """`undo --list` — show the rotated-backup stack with sizes and mtimes."""
    import datetime as _dt
    stack = _backup_stack(target)
    entries = []
    for p, level in stack:
        st = p.stat()
        entries.append({
            "level": level, "backup": p.name, "path": str(p),
            "size": st.st_size,
            "mtime": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    if args.json:
        _emit(_dumps({"target": str(target), "depth": len(entries),
                      "backups": entries}))
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
    """Single-step undo: swap the target with the NEWEST backup on the stack.

    Normally that is ``<name>.bak`` (undo twice = redo); after a crash left a
    level-1 gap, the stack is gap-tolerant and the swap falls back to the
    newest snapshot that really exists (e.g. ``.bak2``).
    """
    stack = _backup_stack(target)
    if not stack:
        bak = target.parent / (target.name + ".bak")
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no backup at {bak} (created by `akcli draw --apply`)")
    bak = stack[0][0]
    from ..readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(bak))
    summary = _undo_summary(cur, old)
    if not getattr(args, "apply", False):
        if args.json:
            _emit(_dumps({"applied": False, "target": str(target),
                          "backup": str(bak), "steps": 1, "summary": summary}))
        else:
            _emit(f"undo (dry-run): would restore {bak.name}\n  {summary}\n"
                  "re-run with --apply to swap (undo again = redo)")
        return EXIT["OK"]
    _refuse_if_gui_open(args, target)
    import shutil as _shutil
    tmp = target.parent / (target.name + ".undo-tmp")
    _shutil.copy2(bak, tmp)
    _shutil.copy2(target, bak)
    tmp.replace(target)
    if args.json:
        _emit(_dumps({"applied": True, "target": str(target),
                      "backup": str(bak), "steps": 1, "summary": summary}))
    else:
        _emit(f"undo: restored {target.name} from backup — {summary}\n"
              f"previous content kept at {bak.name} (undo again = redo)")
    return EXIT["OK"]


def _undo_walk(args: argparse.Namespace, target, steps: int) -> int:
    """Multi-step undo: restore the snapshot ``steps`` back, keeping redo alive.

    Reverses the top ``steps+1`` entries of ``[file, .bak, .bak2, …]``: the file
    becomes the deepest snapshot and ``.bak`` becomes the one just newer than it,
    so a following single-step `undo` swaps that pair and redoes the last step.
    Steps beyond the available stack are clamped to walk back as far as it goes.
    """
    stack = _backup_stack(target)
    if not stack:
        bak = target.parent / (target.name + ".bak")
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no backup at {bak} (created by `akcli draw --apply`)")
    steps = min(steps, len(stack))
    # positions[0] = the live file, positions[i] = the level-i backup
    positions = [target] + [p for p, _lvl in stack[:steps]]
    from ..readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(positions[steps]))
    summary = _undo_summary(cur, old)
    if not getattr(args, "apply", False):
        if args.json:
            _emit(_dumps({"applied": False, "target": str(target),
                          "backup": str(positions[steps]), "steps": steps,
                          "summary": summary}))
        else:
            _emit(f"undo (dry-run): would walk back {steps} step(s) to "
                  f"{positions[steps].name}\n  {summary}\n"
                  "re-run with --apply (a single `undo` afterwards redoes the last step)")
        return EXIT["OK"]
    _refuse_if_gui_open(args, target)
    # Reverse the [target, .bak, ...] chain by swapping end-pairs through an
    # ON-DISK temp: every snapshot lives in SOME file at every instant, so a
    # crash mid-undo can at worst leave one pair part-swapped (with the third
    # copy in <name>.undo-tmp) — it can never hold the only copy in RAM.
    import os
    import shutil
    lo, hi = 0, len(positions) - 1
    while lo < hi:
        a, b = positions[lo], positions[hi]
        tmp = a.parent / (a.name + ".undo-tmp")
        shutil.copy2(a, tmp)
        shutil.copy2(b, a)
        os.replace(tmp, b)
        lo += 1
        hi -= 1
    if args.json:
        _emit(_dumps({"applied": True, "target": str(target),
                      "backup": str(positions[steps]), "steps": steps,
                      "summary": summary}))
    else:
        _emit(f"undo: walked back {steps} step(s), restored {target.name} — {summary}\n"
              f"stack reversed; a single `undo` redoes the last step")
    return EXIT["OK"]


# --------------------------------------------------------------------------- #
# new (blank-sheet bootstrap)
# --------------------------------------------------------------------------- #
# KiCad standard paper sizes (eeschema "paper" token). Portrait names only; the
# format matches what `read`/`draw` accept and what eeschema writes.
_KICAD_PAPERS = (
    "A0", "A1", "A2", "A3", "A4", "A5",
    "A", "B", "C", "D", "E",
    "USLetter", "USLegal", "USLedger",
)
# Mirror the KiCad 8 header the reader/writer already round-trip (see fixtures).
_NEW_SCH_VERSION = 20231120
_NEW_SCH_GENERATOR = "eeschema"
_NEW_SCH_GENERATOR_VERSION = "8.0"


def _blank_sch(paper: str, title: str | None) -> str:
    """Render a minimal valid ``.kicad_sch`` the reader and ``draw`` accept.

    version/generator/uuid/paper plus an empty ``lib_symbols`` and a root
    ``sheet_instances`` page — the smallest document eeschema opens without a
    repair prompt and that ``draw`` can immediately append symbols/wires to.
    """
    import uuid as _uuid
    lines = [
        "(kicad_sch",
        f"\t(version {_NEW_SCH_VERSION})",
        f'\t(generator "{_NEW_SCH_GENERATOR}")',
        f'\t(generator_version "{_NEW_SCH_GENERATOR_VERSION}")',
        f'\t(uuid "{_uuid.uuid4()}")',
        f'\t(paper "{paper}")',
    ]
    if title:
        lines += [
            "\t(title_block",
            f'\t\t(title "{_sexpr_escape(title)}")',
            "\t)",
        ]
    lines += [
        "\t(lib_symbols)",
        '\t(sheet_instances',
        '\t\t(path "/"',
        '\t\t\t(page "1")',
        "\t\t)",
        "\t)",
        ")",
        "",
    ]
    return "\n".join(lines)


def _sexpr_escape(text: str) -> str:
    """Escape a bare string for a KiCad s-expression double-quoted token."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _cmd_new(args: argparse.Namespace) -> int:
    """`new <file.kicad_sch>` — bootstrap a minimal blank schematic.

    Writes a valid empty sheet so `draw` has something to append to (a blank
    sheet no longer has to be hand-written). Refuses an existing file without
    `--force`. Standard status line; `--json` envelope.
    """
    target = _require_path(args.path, "output .kicad_sch")
    if not str(target).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: new writes a .kicad_sch file")
    paper = getattr(args, "paper", None) or "A4"
    if paper not in _KICAD_PAPERS:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: unknown paper {paper!r} "
                        f"(one of: {', '.join(_KICAD_PAPERS)})")
    if target.exists() and not getattr(args, "force", False):
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: {target} exists (use --force to overwrite)")
    text = _blank_sch(paper, getattr(args, "title", None))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    status_line = (f"status: CREATED — wrote {target.name} "
                   f"(blank {paper} sheet; `akcli draw` to add parts)")
    if args.json:
        _emit(_dumps({"created": True, "target": str(target), "paper": paper,
                      "title": getattr(args, "title", None) or None,
                      "status": "created"}))
    else:
        _emit(status_line)
    return EXIT["OK"]


def _cmd_ops(args: argparse.Namespace) -> int:
    """`ops list` / `ops template <op>` — the op-list authoring kit."""
    from .. import ops as opsmod

    action = getattr(args, "action", None)
    if action == "list":
        try:
            caps = opsmod.load_capabilities()["ops"]  # packaged mirror + repo fallback
        except Exception:
            caps = {}
        if args.json:
            _emit(_dumps({
                "protocol_version": opsmod.PROTOCOL_VERSION,
                "ops": [{"name": name,
                         "required": list(opsmod._OP_REQUIRED.get(name, [])),
                         "kicad": (caps.get(name) or {}).get("kicad"),
                         "altium_live": (caps.get(name) or {}).get("altium")}
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
                support = "  [kicad:" + ("yes" if entry.get("kicad") else "no")                           + " altium-live:" + ("yes" if entry.get("altium") else "no") + "]"
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
    raise _ExitWith(EXIT["USAGE"], "ERROR: use `akcli ops list` or `akcli ops template <op>`")


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
    lines.append(f"# connectivity ({len(findings)})")
    if not findings:
        lines.append("  (clean)")
    for f in findings:
        lines.append(f"  {f.severity.value.upper()} [{f.code}] {f.message}")
    return "\n".join(lines)


def _run_draw(args: argparse.Namespace, do_apply: bool) -> int:
    """Shared plan/draw driver: validate + (dry-)apply an op-list to a .kicad_sch."""
    target = _require_path(args.target, "target .kicad_sch")
    if not getattr(args, "ops", None):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing --ops <oplist.json>")

    from ..ops import expand_macros, load_oplist, validate_oplist
    from ..writers import kicad as kwriter

    oplist = load_oplist(args.ops)               # FileNotFound -> exit 4 via main
    oplist = expand_macros(oplist)               # macro ops -> core ops (exit 6 on bad args)
    errs = validate_oplist(oplist)
    if errs:
        for e in errs:
            sys.stderr.write(f"ERROR: [{e.op_index}] {e.code}: {e.message}\n")
        return EXIT["OPLIST"]

    cfg = _load_cfg(args, target)
    sources = _draw_symbol_sources(args, cfg)

    strict = do_apply and getattr(args, "strict_nets", False)
    # Connectivity diff (advisory unless --strict-nets): dry-apply the op-list
    # to a temp copy and compare netlists, so both dry-run and apply report the
    # net effect BEFORE the target is touched. When the diff cannot be computed
    # at all, --strict-nets fails CLOSED (a silently skipped gate is no gate).
    net_lines = None
    net_risk = False
    net_equiv = True
    net_diff_err = None
    if not getattr(args, "no_net_diff", False) or strict:
        from ..netdiff import diff as net_diff
        from ..netdiff import format_summary as net_summary
        from ..netdiff import has_risk as net_has_risk
        from ..readers import kicad as kreader
        import os
        import shutil
        # the copy lives NEXT TO the target (never a TemporaryDirectory): a
        # hierarchical root must still resolve its child sheets on read-back
        tmp = target.parent / f".{target.name}.netdiff.{os.getpid()}.tmp"
        try:
            before_nets = kreader.read_sch(str(target)).nets
            shutil.copy2(target, tmp)
            tmp_findings: list = []
            tmp_results = kwriter.apply(oplist, str(tmp), apply=True,
                                        sources=sources,
                                        verify_out=tmp_findings,
                                        backup_dir=None)
            if _draw_exit(tmp_results, tmp_findings) == EXIT["OK"]:
                after_nets = kreader.read_sch(str(tmp)).nets
                nd = net_diff(before_nets, after_nets)
                net_lines = net_summary(nd)      # [] iff nd.equivalent
                net_risk = net_has_risk(nd)
                net_equiv = nd.equivalent
            # else: the op-list itself fails — the per-op results below explain
            # it, and the real apply refuses on its own (nothing is written),
            # so a "(none)" net diff here would be misleading
        except Exception as e:                   # noqa: BLE001
            net_diff_err = f"{type(e).__name__}: {e}"
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    if net_diff_err is not None:
        if strict:
            raise _ExitWith(EXIT["OPLIST"],
                            "REFUSED: --strict-nets: net diff unavailable "
                            f"({net_diff_err}); nothing written")
        sys.stderr.write(f"WARNING: net diff unavailable: {net_diff_err}\n")

    if strict and net_risk:
        for ln in net_lines or []:
            sys.stderr.write(f"  {ln}\n")
        raise _ExitWith(EXIT["OPLIST"],
                        "REFUSED: --strict-nets: net split/merge touches a "
                        "named net; nothing written")

    findings: list = []
    results = kwriter.apply(
        oplist, str(target), apply=do_apply, sources=sources, verify_out=findings,
        # write a rotated <name>.bak next to the target on apply (the atomic write
        # already guarantees the original is never corrupted; this is extra safety
        # and the stack `akcli undo` walks). Depth from [project] backup_depth.
        backup_dir=(target.parent if do_apply else None),
        backup_depth=_backup_depth(cfg),
        allow_open=bool(getattr(args, "allow_open", False)),
    )

    if do_apply and _draw_exit(results, findings) == EXIT["OK"]:
        _log(args, 1, f"wrote {target}")
        if kwriter.gui_lock_path(target).exists():
            sys.stderr.write(
                "WARNING: this file is open in the KiCad GUI — use File>Revert "
                "there NOW; a GUI save would overwrite this edit from memory\n")
        # advisory secondary ERC via kicad-cli, if installed (never fatal)
        try:
            from ..drivers import kicad_cli
            if kicad_cli.available():
                rep = kicad_cli.erc(str(target))
                if rep is not None:
                    _log(args, 1, f"kicad-cli erc: exit {rep.get('exit_code')}")
        except Exception:  # pragma: no cover - advisory only
            pass

    code = _draw_exit(results, findings)
    show_diff = not getattr(args, "no_net_diff", False)
    if not do_apply:
        hint = " (re-run with --apply)" if getattr(args, "command", "") == "draw" else ""
        status = "dry-run"
        status_line = f"status: dry-run — nothing written{hint}"
    elif code == EXIT["OK"]:
        status = "applied"
        status_line = (f"status: APPLIED — wrote {target.name} "
                       f"(backup {target.name}.bak; `akcli undo` reverts)")
    else:
        status = "refused"
        status_line = "status: REFUSED — nothing written (fix the errors above)"

    if args.json:
        payload = {
            "applied": bool(do_apply and code == EXIT["OK"]),
            "status": status,
            "ops": [r.to_dict() for r in results],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
            ],
            "net_diff": None if (net_lines is None or not show_diff) else {
                "equivalent": net_equiv, "risk": net_risk, "lines": net_lines,
            },
        }
        _emit(_dumps(payload))
    else:
        _emit(_draw_results_text(results, findings))
        if show_diff and net_lines is not None:
            _emit("Net changes:")
            if not net_lines:
                _emit("  (none)")
            for ln in net_lines:
                _emit(f"  {ln}")
        _emit(status_line)

    return code


def _cmd_plan(args: argparse.Namespace) -> int:
    """Validate + dry-run an op-list (per-op preview + connectivity); never writes."""
    return _run_draw(args, do_apply=False)


def _cmd_draw(args: argparse.Namespace) -> int:
    """Apply an op-list to a .kicad_sch (dry-run unless --apply)."""
    return _run_draw(args, do_apply=bool(getattr(args, "apply", False)))


def register(sub, common) -> None:
    p = sub.add_parser("arrange", parents=[common],
                       help="nudge free (unwired/unlabeled) components until "
                            "no symbols overlap (dry-run unless --apply)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to arrange")
    p.add_argument("--apply", action="store_true",
                   help="write the moves (default: preview only)")
    p.add_argument("--grid", type=float, metavar="MIL",
                   help="nudge step in mils (default 100)")
    p.add_argument("--margin", type=float, metavar="MIL",
                   help="required clearance between symbols (default 50)")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra symbol source for the write pipeline")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="write even when a KiCad GUI lock file is present "
                        "(File>Revert in KiCad afterwards)")
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
                            "(<name>.bak..bakN; undo twice = redo)")
    p.add_argument("path", nargs="?", help="the .kicad_sch to restore")
    p.add_argument("--apply", action="store_true",
                   help="actually swap (default is a dry-run preview)")
    p.add_argument("--steps", type=int, metavar="N",
                   help="walk back N snapshots (default 1; a single undo afterwards "
                        "redoes the last step)")
    p.add_argument("--list", action="store_true",
                   help="show the backup stack (mtimes/sizes) and exit")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="swap even when a KiCad GUI lock file is present "
                        "(File>Revert in KiCad afterwards)")
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
    p.set_defaults(handler=_cmd_ops)

    p = sub.add_parser("plan", parents=[common],
                       help="validate + dry-run an op-list against a .kicad_sch (never writes)")
    p.add_argument("target", nargs="?", help="target .kicad_sch file")
    p.add_argument("--ops", metavar="FILE", help="op-list JSON file")
    p.add_argument("--symbols", metavar="PATH", action="append",
                   help="extra .kicad_sym / template .kicad_sch symbol source (repeatable)")
    p.add_argument("--no-net-diff", action="store_true",
                   help="skip the before/after net connectivity diff")
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
    p.add_argument("--strict-nets", action="store_true",
                   help="with --apply: refuse to write when the net diff shows "
                        "a split/merge touching a named net")
    p.add_argument("--allow-open", dest="allow_open", action="store_true",
                   help="write even when a KiCad GUI lock file (~<name>.lck) is "
                        "present — you accept that a GUI save may overwrite the "
                        "edit; File>Revert in KiCad after applying")
    p.set_defaults(handler=_cmd_draw)
