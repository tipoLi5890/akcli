"""`akcli log` — query the workspace write journal (`.akcli/journal.jsonl`).

The read side of :mod:`akcli.journal`: what was edited, when, by which
command, applied or refused, with which op-list hash and net-diff verdict.
Lets an agent (or a harness hook) answer "was there a plan for this op-list
before the --apply" and "what did the last session do here" without
re-deriving state from backups.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..errors import EXIT
from ._shared import _dumps, _emit, _stamp


def _cmd_log(args: argparse.Namespace) -> int:
    from .. import journal

    where = Path(getattr(args, "path", None) or ".")
    target = None
    if where.exists() and where.is_file():
        target = where.name
    entries = journal.read_entries(where, target=target,
                                   limit=getattr(args, "limit", None))
    if getattr(args, "cmd", None):
        entries = [e for e in entries if e.get("cmd") == args.cmd]

    if args.json:
        _emit(_dumps(_stamp({
            "journal_version": journal.JOURNAL_VERSION,
            "journal": str(journal.journal_path(where)),
            "returned": len(entries),
            "entries": entries,
        })))
        return EXIT["OK"]

    if not entries:
        _emit(f"log: no journal entries at {journal.journal_path(where)}")
        return EXIT["OK"]
    for e in entries:
        extra = []
        if e.get("op_count") is not None:
            extra.append(f"{e['op_count']} op(s)")
        nd = e.get("net_diff")
        if isinstance(nd, dict):
            extra.append("nets equivalent" if nd.get("equivalent")
                         else ("NET RISK" if nd.get("risk") else "nets changed"))
        if e.get("backup"):
            extra.append(f"backup {e['backup']}")
        if e.get("ops_sha256"):
            extra.append(f"ops {e['ops_sha256'][:12]}")
        detail = f"  ({', '.join(extra)})" if extra else ""
        _emit(f"{e.get('ts', '?'):<20} {e.get('cmd', '?'):<16} "
              f"{e.get('target', '?'):<24} {e.get('status', '?')}{detail}")
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "log", parents=[common],
        help="show the workspace write journal (what plan/draw/undo did here)")
    p.add_argument("path", nargs="?",
                   help="workspace directory or an edited file (default: .)")
    p.add_argument("--limit", type=int, metavar="N",
                   help="show only the newest N entries")
    p.add_argument("--cmd", metavar="NAME",
                   help="filter by command (plan, draw, arrange, undo, ...)")
    p.set_defaults(handler=_cmd_log)
