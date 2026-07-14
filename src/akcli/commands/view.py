"""`akcli view` — the local web dashboard (calculators + live schematic watch).

``view calc`` / ``view live <sch>`` / ``view <sch.kicad_sch>`` all serve from
one localhost server: /calc always, /live when a schematic is watched. Heavy
imports stay LAZY per handler.
"""

from __future__ import annotations

import argparse

from ..errors import EXIT
from ._shared import _ExitWith, _require_path


def _cmd_view(args: argparse.Namespace) -> int:
    """`view calc` / `view live <sch>` / `view <sch>` — the unified dashboard.

    One server hosts both pages: /calc always, /live when a schematic is
    watched. `view <sch.kicad_sch>` is shorthand for `view live <sch>`.
    """
    from ..webui import server

    what, path = args.what, args.path
    if what.lower().endswith(".kicad_sch") and not path:
        what, path = "live", what
    if what not in ("calc", "live"):
        raise _ExitWith(EXIT["USAGE"],
                        "ERROR: view expects `calc`, `live <sch>`, or a .kicad_sch path")
    port = args.port if args.port is not None else server.DEFAULT_PORT
    if what == "calc":
        return server.serve(port=port, open_browser=not args.no_browser,
                            max_steps=args.max_steps)
    path = _require_path(path, "schematic to watch")
    if not str(path).lower().endswith(".kicad_sch"):
        raise _ExitWith(EXIT["USAGE"], "ERROR: view live watches a .kicad_sch")
    return server.serve(port=port, open_browser=not args.no_browser,
                        target=path, state_dir=args.state_dir,
                        max_steps=args.max_steps)


def register(sub, common) -> None:
    p = sub.add_parser("view", parents=[common],
                       help="local web dashboard: /calc (calculators) + /live (watch a .kicad_sch)")
    p.add_argument("what",
                   help="`calc`, `live`, or directly a .kicad_sch to watch")
    p.add_argument("path", nargs="?",
                   help="the .kicad_sch to watch (view live only)")
    p.add_argument("--port", type=int,
                   help="listen port (default 8765; auto-increments if busy; "
                        "localhost only)")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open the browser automatically")
    p.add_argument("--state-dir", metavar="DIR",
                   help="view live: persist the step timeline here "
                            "(default: fresh temp dir per run)")
    p.add_argument("--max-steps", type=int, default=500, metavar="N",
                   help="keep at most N timeline steps, deleting the oldest "
                        "SVGs (default 500; 0 = unlimited)")
    p.set_defaults(handler=_cmd_view)
