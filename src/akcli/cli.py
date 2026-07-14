"""argparse dispatch + exit codes for the ``akcli`` CLI (SPEC §3.1).

Subcommands ``read net nets component check diff pinmap export plan draw
relink-symbols`` (and more) are live. ``plan``/``draw`` drive the KiCad op-list
executor (``draw`` writes only on ``--apply``) and report a before/after net
connectivity diff (``--no-net-diff`` opts out; ``draw --apply --strict-nets``
refuses splits/merges of named nets). Every handler does its heavy imports
LAZILY (inside the handler) so ``akcli --help`` / ``--version`` run from a
clean checkout with only the Foundation modules present.

Structure
---------
This module is a thin dispatcher: ``build_parser()`` builds the global flags
and delegates subcommand registration to one module per family under
``.commands`` (``query checks drawing calc jlc view``); shared helpers live in
``.commands._shared``. ``main()`` backfills global-flag defaults and maps
handler exceptions to exit codes. A handful of names are re-exported here as
stable test/patch seams (``_load_expected``, ``_easyeda_enrich``).

Conventions
-----------
* **stdout = data, stderr = logs.** Machine-readable output goes to stdout;
  diagnostics/verbose logs go to stderr.
* Global flags (``--json -C/--config -v/-q --no-color --debug``) are accepted by
  every subcommand.
* ``check``/``diff``/``pinmap`` are lint-style: exit ``1`` when actionable findings
  (severity ≥ WARNING) are present, ``0`` when clean; ``--exit-zero`` forces ``0``.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import calc as _calc_cmd
from .commands import checks as _checks_cmd
from .commands import doctor as _doctor_cmd
from .commands import drawing as _drawing_cmd
from .commands import fab as _fab_cmd
from .commands import jlc as _jlc_cmd
from .commands import library as _library_cmd
from .commands import query as _query_cmd
from .commands import release as _release_cmd
from .commands import sim as _sim_cmd
from .commands import view as _view_cmd
from .commands._shared import _ExitWith
from .errors import EXIT, AkcliError, as_error, to_exit
from .ops import PROTOCOL_VERSION

# Stable seams: tests reference/patch these on the ``cli`` module directly.
from .commands.checks import _load_expected  # noqa: F401,E402
from .commands.jlc import _easyeda_enrich  # noqa: F401,E402


# --------------------------------------------------------------------------- #
# parser construction
# --------------------------------------------------------------------------- #
# Global flags use ``SUPPRESS`` defaults so they can appear EITHER before or after the
# subcommand: the shared parent is attached to both the top-level parser and every
# subparser, and SUPPRESS stops the subparser's copy from clobbering a value parsed
# before the subcommand. ``main()`` backfills the real defaults after parsing.
_GLOBAL_DEFAULTS = {
    "config": None, "verbose": 0, "quiet": False,
    "json": False, "no_color": False, "debug": False,
}


def _global_flags() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--config", metavar="PATH", default=argparse.SUPPRESS,
                        help="path to akcli.toml (overrides discovery)")
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="increase verbosity (-v, -vv)")
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="suppress non-error logs")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="disable ANSI color")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="re-raise exceptions with a full traceback")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_flags()
    parser = argparse.ArgumentParser(
        prog="akcli",
        description="AI-native KiCad design agent: draw and edit .kicad_sch from "
                    "JSON op-lists (net-diff safety rails, one-command undo), run "
                    "ERC/design/intent checks, simulate on bundled ngspice, source "
                    "parts, and import Altium .SchDoc/.SchLib/.PcbDoc.",
        epilog=(
            "typical workflow:\n"
            "  akcli read board.kicad_sch            # inspect components + nets\n"
            "  akcli nets board.kicad_sch --intent-snapshot intent.json\n"
            "  akcli ops list && akcli ops template add_wire   # author an op-list\n"
            "  akcli plan board.kicad_sch --ops edit.json      # dry-run + net diff\n"
            "  akcli draw board.kicad_sch --ops edit.json --apply\n"
            "  akcli check board.kicad_sch --intent intent.json  # assert intent held\n"
            "  akcli undo board.kicad_sch --apply    # revert the last write\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],   # accept global flags before the subcommand too
    )
    parser.add_argument("--version", action="store_true",
                        help="print package + protocol version and exit")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # One module per command family registers its subparsers + handlers.
    _query_cmd.register(sub, common)
    _view_cmd.register(sub, common)
    _checks_cmd.register(sub, common)
    _drawing_cmd.register(sub, common)
    _calc_cmd.register(sub, common)
    _jlc_cmd.register(sub, common)
    _library_cmd.register(sub, common)
    _fab_cmd.register(sub, common)
    _release_cmd.register(sub, common)
    _doctor_cmd.register(sub, common)
    _sim_cmd.register(sub, common)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Backfill global-flag defaults (they use argparse.SUPPRESS so a value given before
    # the subcommand isn't clobbered by the subparser's copy).
    for _attr, _default in _GLOBAL_DEFAULTS.items():
        if not hasattr(args, _attr):
            setattr(args, _attr, _default)

    if getattr(args, "version", False):
        print(f"akcli {__version__} (protocol {PROTOCOL_VERSION})")
        return EXIT["OK"]

    handler = getattr(args, "handler", None)
    if not getattr(args, "command", None) or handler is None:
        parser.print_help(sys.stderr)
        return EXIT["USAGE"]

    try:
        return handler(args)
    except _ExitWith as exc:
        if exc.msg:
            sys.stderr.write(exc.msg + "\n")
        return exc.code
    except AkcliError as exc:
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(as_error(exc) + "\n")
        return to_exit(exc)
    except FileNotFoundError as exc:
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(f"ERROR: file not found: {exc.filename or exc}\n")
        return EXIT["NOT_FOUND"]
    except BrokenPipeError:
        return EXIT["OK"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
