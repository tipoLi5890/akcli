"""`akcli fab` — manufacturing policy: profile check + finding explainer.

``fab check board.kicad_pcb --profile p.toml [--order o.toml]`` runs the
versioned vendor policy against the deep-read board; ``fab explain CODE``
prints the rule, the fix direction, and the profile's evidence sources.
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
    profile_path = getattr(args, "profile", None)
    if not profile_path:
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: missing --profile (a fab profile TOML; rules "
                        "are versioned policy, never builtin constants)")
    profile = fab.load_profile(profile_path)
    pcb = kreader.read_pcb(str(board_path))
    findings = fab.check(pcb, profile)

    order = None
    if getattr(args, "order", None):
        order = fab.load_order(args.order)
        findings.extend(fab.check_order(order, profile))

    if args.json:
        _emit(_dumps(_stamp({
            "board": str(board_path),
            "profile": {"id": profile.get("id"),
                        "vendor": profile.get("vendor"),
                        "path": str(profile_path)},
            "order": str(getattr(args, "order", None)) if order is not None else None,
            "vias": len(pcb.vias),
            "findings": [
                {"code": f.code, "severity": f.severity.value,
                 "message": f.message, "pos": f.pos, "anchors": f.anchors}
                for f in findings
            ],
        })))
        return _findings_exit(findings, args)

    _emit(f"fab check: {board_path.name} against {profile.get('id')} "
          f"({profile.get('vendor', '?')})")
    _emit(f"  vias: {len(pcb.vias)}  pads: {len(pcb.pads)}  "
          f"tracks: {len(pcb.tracks)}")
    if not findings:
        _emit("no findings — board fits the profile's free process envelope")
        return EXIT["OK"]
    for f in findings:
        _emit(f"{f.severity.value.upper():<8} {f.code}: {f.message}")
    _emit(f"{len(findings)} finding(s) — `akcli fab explain <CODE>` for the "
          "rule + evidence")
    return _findings_exit(findings, args)


def _cmd_fab_explain(args: argparse.Namespace) -> int:
    from .. import fab

    code = getattr(args, "code", None)
    if not code:
        raise _ExitWith(EXIT["USAGE"], "ERROR: missing finding code "
                                       "(e.g. FAB_VIA_IN_PAD)")
    profile = None
    if getattr(args, "profile", None):
        profile = fab.load_profile(args.profile)
    text = fab.explain(code, profile)
    if text is None:
        raise _ExitWith(EXIT["USAGE"], f"ERROR: unknown finding code {code!r}")
    _emit(text)
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
    pc.add_argument("--profile", metavar="FILE",
                    help="fab profile TOML (id/vendor/source/stackup/via/cost)")
    pc.add_argument("--order", metavar="FILE",
                    help="order manifest TOML (declared purchase intent: "
                         "delivery format, finish, via covering, ...)")
    _add_exit_policy_flags(pc)
    pc.set_defaults(handler=_cmd_fab_check)

    pe = fab_sub.add_parser(
        "explain", parents=[common],
        help="explain a fab finding code: rule, fix direction, evidence")
    pe.add_argument("code", nargs="?", help="finding code, e.g. FAB_VIA_IN_PAD")
    pe.add_argument("--profile", metavar="FILE",
                    help="also cite this profile's sources")
    pe.set_defaults(handler=_cmd_fab_explain)
