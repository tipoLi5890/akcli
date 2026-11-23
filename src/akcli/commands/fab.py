"""`akcli fab` — manufacturing policy: profile check + finding explainer.

``fab check board.kicad_pcb --profile <FILE|ID> [--order o.toml]
[--project p.kicad_pro]`` runs the versioned vendor policy against the
deep-read board; ``fab explain CODE`` prints the rule, the fix direction, and
the profile's evidence sources; ``fab profiles`` lists the profiles bundled
with this install. ``--profile`` takes a profile file or a bundled id — a file
always wins (``fab.resolve_profile``). The board's own DRC minimums are read
from the ``.kicad_pro`` beside it (``--project`` overrides the discovery) and
compared with the profile's.
"""

from __future__ import annotations

import argparse

from ..errors import EXIT
from ._shared import (
    _add_exit_policy_flags,
    _dumps,
    _emit,
    _ExitWith,
    _findings_exit,
    _require_path,
    _stamp,
)


def _cmd_fab_check(args: argparse.Namespace) -> int:
    from .. import fab
    from ..readers import kicad as kreader

    board_path = _require_path(getattr(args, "path", None), "board .kicad_pcb")
    profile_arg = getattr(args, "profile", None)
    if not profile_arg:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: missing --profile (a fab profile TOML file, "
                        "or a bundled profile id — see `akcli fab profiles`; "
                        "rules are versioned, sourced policy, never builtin "
                        "constants)")
    profile, origin = fab.resolve_profile(profile_arg)
    pcb = kreader.read_pcb(str(board_path))

    # The order is read BEFORE the board rules run: its via_covering decides
    # which vias the tenting cap applies to.
    order = None
    if getattr(args, "order", None):
        order = fab.load_order(args.order)

    project = _project_rules(fab, board_path, getattr(args, "project", None))
    findings, facts = fab.evaluate(pcb, profile, order, origin=origin,
                                   rules=project.rules)
    if order is not None:
        findings.extend(fab.check_order(order, profile))
    outline = facts["outline"]
    tenting = facts["tenting"]

    if args.json:
        _emit(_dumps(_stamp({
            "board": str(board_path),
            "profile": {"id": profile.get("id"),
                        "vendor": profile.get("vendor"),
                        "path": origin.path,          # null when bundled
                        "bundled": origin.bundled,
                        "sha256": origin.sha256},
            "order": str(getattr(args, "order", None)) if order is not None else None,
            "vias": len(pcb.vias),
            # --- facts the findings were derived from (null for a board
            #     fab check could not read) -------------------------------
            "via_types": facts["via_types"],
            "outline": outline,     # {method, closed, area_mm2, bbox, ...}
            "tenting": tenting,     # {board, vias, order_via_covering, ...}
            "project": {
                "path": project.path,                 # null: none found
                "status": ("absent" if project.path is None else
                           "unreadable" if project.rules is None else "read"),
                "error": project.error,
                "rules": facts["project_rules"],      # compared rows, or null
            },
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message, "pos": f.pos, "anchors": f.anchors}
                for f in findings
            ],
        })))
        return _findings_exit(findings, args)

    _emit(f"fab check: {board_path.name} against {profile.get('id')} "
          f"({profile.get('vendor', '?')}"
          f"{', bundled profile' if origin.bundled else ''})")
    _emit(f"  vias: {len(pcb.vias)}  pads: {len(pcb.pads)}  "
          f"tracks: {len(pcb.tracks)}")
    for line in _fact_lines(facts, project, order is not None):
        _emit("  " + line)
    if not findings:
        _emit("no findings — board fits the profile's free process envelope")
        return EXIT["OK"]
    for f in findings:
        _emit(f"{f.severity.value.upper():<8} {f.code}: {f.message}")
    _emit(f"{len(findings)} finding(s) — `akcli fab explain <CODE>` for the "
          "rule + evidence")
    return _findings_exit(findings, args)


def _project_rules(fab, board_path, project_arg):
    """The board's own DRC minimums for the rule-drift comparison.

    Discovered beside the board (``fab.find_project``) unless ``--project``
    names the file. A project that is merely absent — or found but unreadable
    — is a skip the output states; a ``--project`` the user named and that
    cannot be used is an error, never a silent skip.
    """
    from ..errors import fail

    if not project_arg:
        return fab.read_project_rules(fab.find_project(board_path))
    path = _require_path(project_arg, "project .kicad_pro")
    if not path.is_file():
        raise _ExitWith(EXIT["NOT_FOUND"],
                        f"ERROR: no such project file: {path}")
    project = fab.read_project_rules(path)
    if project.rules is None:
        fail("BAD_CONFIG", f"--project {path}: {project.error}")
    return project


def _fact_lines(facts: dict, project, has_order: bool) -> list[str]:
    """Text-mode summary of what the board file established — so an area that
    is only a bounding box, or a tenting cap that was not applied, is said
    even when no finding fires."""
    lines: list[str] = []
    outline = facts.get("outline")
    if outline is not None:
        if outline["method"] == "outline":
            cut = (f", {outline['cutouts']} cutout(s) subtracted"
                   if outline["cutouts"] else "")
            lines.append(f"outline: {outline['area_mm2'] / 100:.2f} cm² "
                         f"(Edge.Cuts closed{cut})")
        elif outline["method"] == "bbox":
            lines.append(f"outline: {outline['area_mm2'] / 100:.2f} cm² is "
                         "the BOUNDING BOX — Edge.Cuts did not close ("
                         + "; ".join(outline["problems"]) + ")")
        else:
            lines.append("outline: nothing on Edge.Cuts")
    types = facts.get("via_types")
    if types and sum(types.values()):
        lines.append("via types: " + ", ".join(
            f"{name} {count}" for name, count in types.items()))
        tenting = facts["tenting"]
        board = tenting["board"]
        if board["source"] is None:
            said = "not stated by the board file"
        else:
            said = ", ".join(
                f"{side} {'tented' if board[side] else 'open' if board[side] is False else 'unknown'}"
                for side in ("front", "back")) + f" ({board['source']})"
        counts = tenting["vias"]
        lines.append(f"tenting: {said}; vias tented {counts['tented']}, "
                     f"untented {counts['untented']}, unknown "
                     f"{counts['unknown']}"
                     + (f", not exposed {counts['not_exposed']}"
                        if counts["not_exposed"] else "")
                     + (f"; order declares via_covering "
                        f"{tenting['order_via_covering']!r}"
                        if has_order and tenting["order_via_covering"] else ""))
    if project.path is not None:
        if project.rules is None:
            lines.append(f"project rules: NOT compared — {project.error}")
        else:
            rows = facts.get("project_rules") or []
            lines.append(f"project rules: {project.path} — "
                         f"{sum(1 for r in rows if r['project_mm'] is not None)}"
                         f" of {len(rows)} profile minimum(s) compared")
    return lines


def _cmd_fab_explain(args: argparse.Namespace) -> int:
    from .. import fab

    code = getattr(args, "code", None)
    if not code:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing finding code "
                                       "(e.g. FAB_VIA_IN_PAD)")
    profile = None
    if getattr(args, "profile", None):
        profile, _origin = fab.resolve_profile(args.profile)
    text = fab.explain(code, profile)
    if text is None:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: unknown finding code {code!r}")
    _emit(text)
    return EXIT["OK"]


def _cmd_fab_profiles(args: argparse.Namespace) -> int:
    """List the fab profiles bundled with this install."""
    from .. import fab

    rows = []
    for profile, origin in fab.bundled_profiles():
        got = fab.retrieved_at(profile)
        rows.append({
            "id": profile.get("id"),
            "vendor": profile.get("vendor"),
            "file": origin.name,
            "sha256": origin.sha256,
            "source": {
                "urls": list((profile.get("source") or {}).get("urls") or []),
                "retrieved_at": got.isoformat() if got else None,
            },
        })

    if args.json:
        _emit(_dumps(_stamp({"profiles": rows})))
        return EXIT["OK"]

    if not rows:
        _emit("no fab profiles are bundled with this install — pass "
              "--profile a profile TOML file")
        return EXIT["OK"]
    _emit(f"bundled fab profiles ({len(rows)}):")
    for row in rows:
        _emit(f"  {row['id']}  ({row['vendor'] or '?'}; retrieved "
              f"{row['source']['retrieved_at'] or '?'}; {row['file']})")
        for url in row["source"]["urls"]:
            _emit(f"      source: {url}")
    _emit("use one with `akcli fab check <board.kicad_pcb> --profile <id>`")
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser("fab", parents=[common],
                       help="manufacturing policy: check a board against a "
                            "versioned fab profile")
    fab_sub = p.add_subparsers(dest="fab_command", metavar="<subcommand>")
    p.set_defaults(handler=_cmd_fab_check, path=None, profile=None)

    pc = fab_sub.add_parser(
        "check", parents=[common],
        help="check a .kicad_pcb against a fab profile (+ optional order "
             "manifest)")
    pc.add_argument("path", nargs="?", help="board .kicad_pcb")
    pc.add_argument("--profile", metavar="FILE|ID",
                    help="fab profile: a TOML file (id/vendor/source/stackup/"
                         "via/cost) or the id of a bundled profile (`akcli "
                         "fab profiles`); an existing file wins")
    pc.add_argument("--order", metavar="FILE",
                    help="order manifest TOML (declared purchase intent: "
                         "delivery format, finish, via covering, ...)")
    pc.add_argument("--project", metavar="FILE",
                    help="the board's .kicad_pro, whose design rules are "
                         "compared with the profile's minimums (default: the "
                         "project file beside the board, if any)")
    _add_exit_policy_flags(pc)
    pc.set_defaults(handler=_cmd_fab_check)

    pe = fab_sub.add_parser(
        "explain", parents=[common],
        help="explain a fab finding code: rule, fix direction, evidence")
    pe.add_argument("code", nargs="?", help="finding code, e.g. FAB_VIA_IN_PAD")
    pe.add_argument("--profile", metavar="FILE|ID",
                    help="also cite this profile's sources (file or bundled "
                         "profile id)")
    pe.set_defaults(handler=_cmd_fab_explain)

    pl = fab_sub.add_parser(
        "profiles", parents=[common],
        help="list the fab profiles bundled with this install (id, vendor, "
             "sources, retrieval date)")
    pl.set_defaults(handler=_cmd_fab_profiles)
