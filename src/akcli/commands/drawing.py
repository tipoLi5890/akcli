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
from pathlib import Path

from ..errors import EXIT, AkcliError
from ._shared import (
    _did_you_mean,
    _draw_exit,
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


def _load_groups_file(path: str) -> dict[str, list[str]]:
    """Parse a ``--groups`` map: ``{group_name: [refdes, ...]}`` (TOML or JSON).

    TOML uses a top-level ``[groups]`` table (or bare ``name = [...]`` tables);
    JSON is either ``{"groups": {...}}`` or the bare mapping. Order is preserved
    (groups stack down the page in file order). Malformed input is a USAGE error.
    """
    import json
    from pathlib import Path as _Path

    p = _Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise _ExitWith(EXIT["NOT_FOUND"], f"ERROR: groups file not found: {path}")
    except OSError as exc:  # a directory, mode 000, ... — never a traceback
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: groups file unreadable: {path}: "
                        f"{exc.strerror or exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ExitWith(EXIT["USAGE"],
                        f"ERROR: groups file is not UTF-8 text: {path} "
                        f"(byte 0x{raw[exc.start]:02x} at offset {exc.start})")
    data: object
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: invalid groups JSON: {e}")
    else:
        import tomllib
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise _ExitWith(EXIT["USAGE"], f"ERROR: invalid groups TOML: {e}")
    if isinstance(data, dict) and isinstance(data.get("groups"), dict):
        data = data["groups"]
    if not isinstance(data, dict) or not data:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: groups file must map group-name -> [refdes, ...]")
    out: dict[str, list[str]] = {}
    for gname, refs in data.items():
        if (not isinstance(refs, list)
                or not all(isinstance(r, str) and r for r in refs)):
            raise _ExitWith(EXIT["USAGE"],
                            f"ERROR: group {gname!r} must be a list of designators")
        out[str(gname)] = list(refs)
    return out


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
    from ..writers import kicad as kwriter
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": target.name,
              "ops": [m.to_op() for m in moves]}
    cfg = _load_cfg(args, target)
    findings: list = []
    results = kwriter.apply(oplist, str(target), apply=True,
                            sources=_draw_symbol_sources(args, cfg),
                            verify_out=findings, backup_dir=_backups_dir(target),
                            backup_depth=_backup_depth(cfg),
                            allow_open=bool(getattr(args, "allow_open", False)))
    code = _draw_exit(results, findings)
    from .. import journal as _journal
    _journal.record(target, "arrange",
                    "applied" if code == EXIT["OK"] else "refused",
                    op_count=len(moves),
                    note=getattr(args, "note", None),
                    backup=(_backup_label(target) if code == EXIT["OK"] else None))
    if args.json:
        _emit(_dumps(_stamp({
            **base, "applied": code == EXIT["OK"],
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings
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


def _propose_group_labels(target, groups: dict, out_path: str) -> None:
    """Emit a label-on-pin op-list draft that makes a re-pack net-safe.

    ``arrange --groups`` moves every bundle (component + its power
    satellites) rigidly but INDEPENDENTLY — so any net that relies on
    geometry BETWEEN two bundles (a connecting wire, pin-tip coincidence)
    can split when they move, same group or not. The fix is the label-on-pin
    discipline: every NAMED net touching >= 2 grouped components gets an
    ``add_net_label`` on each grouped member pin, making connectivity
    name-backed and move-invariant (the exact property the net-preservation
    gate demands). The draft NEVER touches the file — apply it with
    ``plan``/``draw`` like any op-list, then re-run ``arrange --groups``.
    Unnamed multi-bundle nets are reported (they need a human-chosen name).
    """
    from .doc import _natkey
    from ..readers import kicad as kreader
    sch = kreader.read_sch(str(target))
    ref_group: dict[str, str] = {}
    for gname, refs in groups.items():
        for ref in refs:
            if not ref.startswith("#"):
                ref_group[ref] = gname
    label_ops: list[dict] = []
    unnamed: list[str] = []
    spanning = 0
    for net in sorted(sch.nets, key=lambda n: n.name or ""):
        grouped = [(r, p) for r, p in net.members if r in ref_group]
        if len({r for r, _p in grouped}) < 2:   # single-bundle nets are safe
            continue
        if not net.is_named or not net.name:
            ref, pin = grouped[0]
            unnamed.append(net.name or f"<unnamed@{ref}.{pin}>")
            continue
        spanning += 1
        for r, pin in sorted(grouped, key=lambda m: (_natkey(m[0]), _natkey(m[1]))):
            label_ops.append({"op": "add_net_label", "name": net.name,
                              "at": f"{r}.{pin}"})
    labeled_nets = {op["name"] for op in label_ops}
    delete_ops = _redundant_wire_deletes(target, sch, ref_group, labeled_nets)
    doc = {"protocol_version": 1, "target_format": "kicad",
           "target_file": target.name, "ops": label_ops + delete_ops}
    Path(out_path).write_text(_dumps(doc) + "\n", encoding="utf-8",
                              newline="\n")
    sys.stderr.write(
        f"propose-labels: {len(label_ops)} label op(s) for {spanning} "
        f"multi-bundle net(s) + {len(delete_ops)} wire/junction/satellite "
        f"fix op(s) -> {out_path}\n")
    if unnamed:
        sys.stderr.write(
            "propose-labels: NOT covered (unnamed multi-bundle nets — name "
            "them first): " + ", ".join(unnamed) + "\n")


def _redundant_wire_deletes(target, sch, ref_group: dict, labeled: set) -> list:
    """``delete_object`` ops for wire clusters the new labels make redundant.

    A stretched wire is the remaining hazard after labeling: rigid bundle
    moves drag cross-bundle wires into diagonals that can sweep across
    unrelated pins and MERGE nets. A wire cluster (segments transitively
    connected by shared endpoints, tee-touches and junctions) is deleted only
    when it is provably redundant: every component pin it touches sits on a
    net that just received pin labels — so removing the copper cannot lose
    connectivity, it can only remove the failure mode. Junctions inside a
    deleted cluster go with it.
    """
    from ..readers import sexpr as _sexpr
    from ..safety import read_text_guarded as _read_text_guarded

    def key(x: float, y: float) -> tuple:
        return (round(x * 10), round(y * 10))

    # pin position -> (ref, pin_number); (ref, pin) -> net name
    pos_pin: dict = {}
    for c in sch.components:
        for pin in c.pins:
            pos_pin[key(pin.x_mil, pin.y_mil)] = (c.designator, pin.number)
    pin_net: dict = {}
    for net in sch.nets:
        for r, p in net.members:
            pin_net[(r, p)] = net.name if net.is_named else None

    root = _sexpr.parse(_read_text_guarded(target))
    segs: list = []          # (uuid, (x0,y0), (x1,y1))
    for wire in root.find_all("wire"):
        pts = wire.find("pts")
        un = wire.find("uuid")
        if pts is None or un is None or len(un.children or []) < 2:
            continue
        xy = pts.find_all("xy")
        if len(xy) < 2:
            continue
        from ..checks.layout import child_mil
        p0 = (child_mil(xy[0], 1), child_mil(xy[0], 2))
        p1 = (child_mil(xy[-1], 1), child_mil(xy[-1], 2))
        segs.append((str(un.children[1].value), p0, p1))

    # union-find over segments: shared endpoints AND endpoint-on-segment tees
    parent = list(range(len(segs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    def on_seg(pt: tuple, a: tuple, b: tuple) -> bool:
        (px, py), (ax, ay), (bx, by) = pt, a, b
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        if abs(cross) > 0.5:                     # not collinear (0.05 mil·mil)
            return False
        dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
        return 0 <= dot <= (bx - ax) ** 2 + (by - ay) ** 2

    by_point: dict = {}
    for i, (_u, p0, p1) in enumerate(segs):
        for pt in (p0, p1):
            by_point.setdefault(key(*pt), []).append(i)
    for indices in by_point.values():
        for i in indices[1:]:
            union(indices[0], i)
    for i, (_u, p0, p1) in enumerate(segs):
        for j, (_u2, q0, q1) in enumerate(segs):
            if i != j and (on_seg(q0, p0, p1) or on_seg(q1, p0, p1)):
                union(i, j)

    clusters: dict = {}
    for i in range(len(segs)):
        clusters.setdefault(find(i), []).append(i)

    junctions: list = []     # (uuid, keyed position)
    for jn in root.find_all("junction"):
        at, un = jn.find("at"), jn.find("uuid")
        if at is None or un is None or len(un.children or []) < 2:
            continue
        from ..checks.layout import child_mil
        junctions.append((str(un.children[1].value), key(child_mil(at, 1), child_mil(at, 2))))

    deletes: list = []
    deleted_idx: set = set()
    for members in clusters.values():
        touched = []
        pts: set = set()
        for i in members:
            _u, p0, p1 = segs[i]
            pts.add(key(*p0))
            pts.add(key(*p1))
        for pk in pts:
            if pk in pos_pin:
                touched.append(pos_pin[pk])
        real = [(r, p) for r, p in touched if not r.startswith("#")]
        if len({r for r, _p in real}) < 2:
            continue                             # intra-bundle copper stays
        if any(pin_net.get(t) not in labeled for t in real):
            continue                             # some pin is NOT name-backed
        for i in sorted(members, key=lambda n: segs[n][0]):
            deletes.append({"op": "delete_object", "uuid": segs[i][0]})
            deleted_idx.add(i)
        for juuid, jpos in junctions:
            if jpos in pts:
                deletes.append({"op": "delete_object", "uuid": juuid})

    # `#` satellites (PWR_FLAG, mid-wire power ports) connect by TOUCH. One
    # that neither sits on a real pin nor TERMINATES a surviving wire (it
    # rides mid-segment, or its copper is being deleted) will strand when
    # bundles move — relocate it onto the first real pin of its net, where
    # pin-tip coincidence makes it ride that host's bundle through any
    # re-pack. Endpoint-terminating ports stay: moving them would leave the
    # wire dangling.
    sat_moves: list = []
    real_tips = {key(q.x_mil, q.y_mil)
                 for c in sch.components if not c.designator.startswith("#")
                 for q in c.pins}
    real_pin_pos = {(c.designator, q.number): (q.x_mil, q.y_mil)
                    for c in sch.components
                    if not c.designator.startswith("#") for q in c.pins}
    surviving_ends = {key(*pt)
                      for i, (_u, p0, p1) in enumerate(segs)
                      if i not in deleted_idx for pt in (p0, p1)}
    for net in sch.nets:
        real = sorted((m for m in net.members if m in real_pin_pos),
                      key=lambda m: (m[0], m[1]))
        if not real:
            continue
        for r, p in net.members:
            if not r.startswith("#"):
                continue
            comp = next(c for c in sch.components if c.designator == r)
            pin = next((q for q in comp.pins if q.number == p), None)
            if pin is None:
                continue
            tip = key(pin.x_mil, pin.y_mil)
            if tip in real_tips or tip in surviving_ends:
                continue          # pin-coincident, or it anchors a live wire
            hx, hy = real_pin_pos[real[0]]
            sat_moves.append({
                "op": "move_component", "designator": r,
                "x_mil": comp.x_mil + (hx - pin.x_mil),
                "y_mil": comp.y_mil + (hy - pin.y_mil)})
    return sat_moves + deletes


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
        groups = _load_groups_file(args.groups)
    if getattr(args, "propose_labels", None):
        _propose_group_labels(target, groups, args.propose_labels)
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
    from ..writers import kicad as kwriter
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": target.name,
              "ops": [m.to_op(carry=True) for m in moves]}

    # NET-PRESERVATION GATE — the --groups contract made mechanical: dry-apply
    # the rigid moves to a temp copy and require net EQUIVALENCE before
    # anything is written. Label-on-pin sheets pass by construction; a sheet
    # wired ACROSS group boundaries can split/merge nets when bundles
    # separate — refuse that loudly instead of stretching wires silently.
    import os
    import shutil
    from ..netdiff import diff as _net_diff
    from ..netdiff import format_summary as _net_summary
    from ..readers import kicad as kreader
    before_nets = kreader.read_sch(str(target)).nets
    # next to the target so hierarchical child sheets still resolve
    tmp = target.parent / f".{target.name}.groupsdiff.{os.getpid()}.tmp"
    shutil.copy2(target, tmp)
    try:
        kwriter.apply(oplist, str(tmp), apply=True,
                      sources=_draw_symbol_sources(args, cfg),
                      verify_out=[], backup_dir=None, allow_open=True)
        after_nets = kreader.read_sch(str(tmp)).nets
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    nd = _net_diff(before_nets, after_nets)
    if not nd.equivalent and not getattr(args, "allow_net_changes", False):
        for ln in _net_summary(nd):
            sys.stderr.write(f"  {ln}\n")
        raise _ExitWith(
            EXIT["OPLIST"],
            "REFUSED: arrange --groups would change the netlist (see the "
            "lines above); nothing written. Wires crossing group boundaries "
            "cannot move rigidly — generate a label-on-pin fix with "
            "`--propose-labels OUT.json` (then plan/draw it and re-run), "
            "fix the group map, or pass --allow-net-changes to accept")

    findings: list = []
    results = kwriter.apply(oplist, str(target), apply=True,
                            sources=_draw_symbol_sources(args, cfg),
                            verify_out=findings, backup_dir=_backups_dir(target),
                            backup_depth=_backup_depth(cfg),
                            allow_open=bool(getattr(args, "allow_open", False)))
    code = _draw_exit(results, findings)
    from .. import journal as _journal
    _journal.record(target, "arrange-groups",
                    "applied" if code == EXIT["OK"] else "refused",
                    op_count=len(moves),
                    note=getattr(args, "note", None),
                    backup=(_backup_label(target) if code == EXIT["OK"] else None))
    frames_refreshed = 0
    if code == EXIT["OK"] and getattr(args, "frames", False):
        frames_refreshed = _refresh_frames(args, target, cfg)
    if args.json:
        _emit(_dumps(_stamp({
            **base, "applied": code == EXIT["OK"],
            "frames_refreshed": frames_refreshed,
            "connectivity": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in findings],
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


def _refresh_frames(args: argparse.Namespace, target, cfg) -> int:
    """Redraw every group frame after a re-layout (``arrange --groups --frames``).

    Keyed frame uuids replace stale borders in place. Applied on top of the
    arrange write with no extra backup rotation, so a single ``akcli undo``
    reverts the arrange together with its frame refresh. Returns the number
    of frames drawn (0 = nothing to frame / refresh refused).
    """
    from ..groupframe import plan_frames
    from ..writers import kicad as kwriter
    ops_list = plan_frames(target)
    if not ops_list:
        return 0
    oplist = {"protocol_version": 1, "target_format": "kicad",
              "target_file": target.name, "ops": ops_list}
    findings: list = []
    results = kwriter.apply(oplist, str(target), apply=True,
                            sources=_draw_symbol_sources(args, cfg),
                            verify_out=findings, backup_dir=None,
                            allow_open=bool(getattr(args, "allow_open", False)))
    if _draw_exit(results, findings) != EXIT["OK"]:
        sys.stderr.write("WARNING: frame refresh failed; frames left unchanged\n")
        return 0
    from .. import journal as _journal
    _journal.record(target, "groups-frame", "applied", op_count=len(ops_list))
    return len(ops_list) // 2


_BACKUP_SCAN_MAX = 99          # matches the config backup_depth ceiling


def _backups_dir(target) -> Path:
    """Where new rotated backups go: ``.akcli/backups/`` in the workspace."""
    from .. import journal as _journal
    return _journal.backups_dir(target)


def _backup_label(target) -> str:
    """Human/journal label for the level-1 backup (workspace-relative posix)."""
    from .. import journal as _journal
    return f"{_journal.DIR_NAME}/{_journal.BACKUP_DIR_NAME}/{target.name}.bak"


def _backup_stack(target) -> list:
    """Every rotated backup for ``target``, newest first: ``[(path, level), …]``.

    Gap-TOLERANT: a missing level (e.g. a crash between the rotation renames
    and the fresh ``.bak`` copy) must not hide the deeper snapshots that still
    exist — ``undo --list``/``--steps`` walk whatever is really on disk, in
    level order.

    Backups live in ``.akcli/backups/``; when that directory holds none for
    this target, fall back to the legacy pre-0.12 location (``<name>.bak``
    next to the file) so `undo` keeps working on an existing workspace.
    """
    # ONE naming authority: the writer both rotates and names the backups, so
    # `undo` scans with the writer's own function (a local copy of the rule
    # drifting from it would silently hide snapshots).
    from ..writers.kicad import backup_name

    def _scan(base: Path) -> list:
        return [
            (p, level)
            for level in range(1, _BACKUP_SCAN_MAX + 1)
            if (p := base / backup_name(target.name, level)).exists()
        ]

    return _scan(_backups_dir(target)) or _scan(target.parent)


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
    """Single-step undo: swap the target with the NEWEST backup on the stack.

    Normally that is ``<name>.bak`` (undo twice = redo); after a crash left a
    level-1 gap, the stack is gap-tolerant and the swap falls back to the
    newest snapshot that really exists (e.g. ``.bak2``).
    """
    stack = _backup_stack(target)
    if not stack:
        bak = _backups_dir(target) / (target.name + ".bak")
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no backup at {bak} (created by `akcli draw --apply`)")
    bak = stack[0][0]
    from ..readers import kicad as kreader
    cur = kreader.read_sch(str(target))
    old = kreader.read_sch(str(bak))
    summary = _undo_summary(cur, old)
    if not getattr(args, "apply", False):
        if args.json:
            _emit(_dumps(_stamp({"applied": False, "target": str(target),
                                 "backup": str(bak), "steps": 1,
                                 "summary": summary})))
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
    from .. import journal as _journal
    _journal.record(target, "undo", "applied", steps=1, backup=bak.name,
                    note=getattr(args, "note", None))
    if args.json:
        _emit(_dumps(_stamp({"applied": True, "target": str(target),
                             "backup": str(bak), "steps": 1,
                             "summary": summary})))
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
        bak = _backups_dir(target) / (target.name + ".bak")
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
            _emit(_dumps(_stamp({"applied": False, "target": str(target),
                                 "backup": str(positions[steps]),
                                 "steps": steps, "summary": summary})))
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
    from .. import journal as _journal
    _journal.record(target, "undo", "applied", steps=steps,
                    backup=positions[steps].name,
                    note=getattr(args, "note", None))
    if args.json:
        _emit(_dumps(_stamp({"applied": True, "target": str(target),
                             "backup": str(positions[steps]),
                             "steps": steps, "summary": summary})))
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

    # ONE quoting authority (writers.kicad._q): a fourth hand-rolled escaper
    # here missed control characters, so `new --title $'A\nB'` wrote a file
    # eeschema refused to open while every akcli gate reported success — the
    # exact bug _q's docstring records fixing on the writer side.
    from ..writers.kicad import _q as _sexpr_quote

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
            f'\t\t(title {_sexpr_quote(title)})',
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
    return EXIT["OK"]


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
            oplist = opsmod.load_oplist(opsfile)
            oplist, errs = opsmod.prepare_oplist(oplist)
            errors.extend(
                {"op_index": e.op_index, "code": e.code, "message": e.message}
                for e in errs)
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
                         connectivity: list, net_diff, preview=None) -> dict:
    """The ONE author of the plan/draw ``--json`` payload.

    Every plan/draw exit path (normal run AND structural op-list refusal)
    routes through here, so the shape can never fork from
    ``draw-result.schema.json`` in just one branch.
    """
    return {
        "schema_version": "1.0",   # draw-result.schema.json
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


def _render_preview(tmp_path, out_path) -> dict | None:
    """Render the dry-applied temp copy to ``out_path`` (SVG).

    The agent's look-before-apply channel: ``plan/draw --render OUT.svg``
    shows the WOULD-BE sheet without touching the target. Non-fatal by
    policy — a broken preview must never block a valid draw (same contract
    as "net diff unavailable"), so any failure is a stderr warning + ``None``.
    """
    try:
        from pathlib import Path
        from .. import render_svg
        from ..readers import kicad as kreader
        sch = kreader.read_sch(str(tmp_path))
        prims = kreader.read_primitives(str(tmp_path))
        # previews are agent-facing: always include the coordinate grid so
        # the next op-list's numbers can be read straight off the image
        svg = render_svg.render(sch, prims, grid=True)
        out = Path(out_path)
        out.write_text(svg, encoding="utf-8", newline="\n")
        return {"path": str(out), "bytes": len(svg.encode("utf-8"))}
    except Exception as e:  # noqa: BLE001 — advisory surface, never fatal
        sys.stderr.write(
            f"WARNING: preview render unavailable: {type(e).__name__}: {e}\n")
        return None


def _run_draw(args: argparse.Namespace, do_apply: bool) -> int:
    """Shared plan/draw driver: validate + (dry-)apply an op-list to a .kicad_sch."""
    target = _require_path(args.target, "target .kicad_sch")
    if not getattr(args, "ops", None):
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing --ops <oplist.json>")

    from ..ops import load_oplist, prepare_oplist
    from ..writers import kicad as kwriter

    # FileNotFound / malformed-JSON / bad-macro / bad-group failures raise
    # AkcliError and surface via cli.main — which renders them as a
    # machine-readable {"error": {...}} envelope under --json (never a bare
    # non-JSON stdout). prepare_oplist owns the order: validate the author's
    # ops (so a macro's own fields are checked), expand macros (propagating
    # each op's `group` tag), resolve group-local coordinates, validate the
    # result — with error indices mapped back onto the author's op-list.
    oplist, errs = prepare_oplist(load_oplist(args.ops))
    if errs:
        if args.json:
            # Same draw-result shape as a normal run, so an agent parses ONE
            # schema on every plan/draw exit path (status "refused", the
            # structural errors as per-op error results with remediation).
            # OpError's document-level sentinel is op_index -1; the schema
            # (and the per-op shape) require >= 0, so clamp it to 0.
            from ..errors import remediation_for
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

    cfg = _load_cfg(args, target)
    # lib_ids let the lib-table fallback resolve a nickname (Device:R) that
    # neither --symbols nor the config [paths] provide
    sources = _draw_symbol_sources(args, cfg, lib_ids=_oplist_lib_ids(oplist),
                                   project_dir=target.parent)

    strict = do_apply and getattr(args, "strict_nets", False)
    # Connectivity diff (advisory unless --strict-nets): dry-apply the op-list
    # to a temp copy and compare netlists, so both dry-run and apply report the
    # net effect BEFORE the target is touched. When the diff cannot be computed
    # at all, --strict-nets fails CLOSED (a silently skipped gate is no gate).
    # The SAME temp dry-apply also feeds the --render preview (one apply, two
    # consumers) — with --no-net-diff the preview still gets its own dry-apply.
    want_diff = not getattr(args, "no_net_diff", False) or strict
    render_out = getattr(args, "render", None)
    net_lines = None
    net_risk = False
    net_equiv = True
    net_diff_err = None
    preview = None
    if want_diff or render_out:
        from ..netdiff import diff as net_diff
        from ..netdiff import format_summary as net_summary
        from ..netdiff import has_risk as net_has_risk
        from ..readers import kicad as kreader
        import os
        import shutil
        # the copy lives NEXT TO the target (never a TemporaryDirectory): a
        # hierarchical root must still resolve its child sheets on read-back
        tmp = target.parent / f".{target.name}.netdiff.{os.getpid()}.tmp"
        dry_ok = False
        had_exc = False
        try:
            before_nets = kreader.read_sch(str(target)).nets if want_diff else None
            shutil.copy2(target, tmp)
            tmp_findings: list = []
            tmp_results = kwriter.apply(oplist, str(tmp), apply=True,
                                        sources=sources,
                                        verify_out=tmp_findings,
                                        backup_dir=None)
            dry_ok = _draw_exit(tmp_results, tmp_findings) == EXIT["OK"]
            if want_diff and dry_ok:
                after_nets = kreader.read_sch(str(tmp)).nets
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
                sys.stderr.write("WARNING: preview render unavailable: "
                                 f"{type(e).__name__}: {e}\n")
        finally:
            # preview renders INSIDE the temp's lifetime (only OUT.svg persists);
            # a refused op-list leaves the temp pristine, so rendering it would
            # show the before-state as if it were the plan — skip it honestly.
            if render_out and dry_ok:
                preview = _render_preview(tmp, render_out)
            elif render_out and not had_exc:
                sys.stderr.write(
                    "WARNING: preview skipped (op-list did not dry-apply "
                    "cleanly; fix the errors below)\n")
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
        # write a rotated <name>.bak under .akcli/backups/ on apply (the atomic
        # write already guarantees the original is never corrupted; this is extra
        # safety and the stack `akcli undo` walks). Depth from [project] backup_depth.
        backup_dir=(_backups_dir(target) if do_apply else None),
        backup_depth=_backup_depth(cfg),
        allow_open=bool(getattr(args, "allow_open", False)),
    )

    if do_apply and _draw_exit(results, findings) == EXIT["OK"]:
        _log(args, 1, f"wrote {target}")
        if kwriter.gui_lock_path(target).exists():
            sys.stderr.write(
                "WARNING: this file is open in the KiCad GUI — use File>Revert "
                "there NOW; a GUI save would overwrite this edit from memory\n")
        # advisory secondary ERC via kicad-cli, if installed (never fatal —
        # but a broken integration is surfaced on stderr, not swallowed);
        # --no-erc skips it honestly instead of leaving the agent to wonder
        # whether the external run happened
        try:
            from ..drivers import kicad_cli
            if getattr(args, "no_erc", False):
                _log(args, 1, "advisory kicad-cli ERC skipped (--no-erc)")
            elif kicad_cli.available():
                rep = kicad_cli.erc(str(target))
                if rep is not None:
                    _log(args, 1, f"kicad-cli erc: exit {rep.get('exit_code')}")
        except AkcliError as exc:  # pragma: no cover - advisory only
            sys.stderr.write(f"note: advisory kicad-cli ERC skipped: {exc}\n")
        except OSError as exc:  # pragma: no cover - advisory only
            sys.stderr.write(f"note: advisory kicad-cli ERC failed: {exc}\n")

    code = _draw_exit(results, findings)
    show_diff = not getattr(args, "no_net_diff", False)
    if not do_apply:
        hint = " (re-run with --apply)" if getattr(args, "command", "") == "draw" else ""
        status = "dry-run"
        status_line = f"status: dry-run — nothing written{hint}"
    elif code == EXIT["OK"]:
        status = "applied"
        status_line = (f"status: APPLIED — wrote {target.name} "
                       f"(backup {_backup_label(target)}; `akcli undo` reverts)")
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
                "equivalent": net_equiv, "risk": net_risk, "lines": net_lines,
            },
            preview=preview)))
    else:
        _emit(_draw_results_text(results, findings))
        if show_diff and net_lines is not None:
            _emit("Net changes:")
            if not net_lines:
                _emit("  (none)")
            for ln in net_lines:
                _emit(f"  {ln}")
        if preview is not None:
            _emit(f"preview: {preview['path']} ({preview['bytes']} bytes)")
        _emit(status_line)

    from .. import journal as _journal
    _journal.record(
        target, getattr(args, "command", "draw") or "draw", status,
        ops_sha256=_ops_sha256(args.ops),
        op_count=len(oplist.get("ops", []) or []),
        net_diff=(None if net_lines is None
                  else {"equivalent": net_equiv, "risk": net_risk}),
        note=getattr(args, "note", None),
        backup=(_backup_label(target) if status == "applied" else None),
    )

    return code


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
