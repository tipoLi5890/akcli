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
stable test/patch seams (``_load_expected``).

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
from typing import NoReturn

from . import __version__
from .commands import calc as _calc_cmd
from .commands import capabilities as _capabilities_cmd
from .commands import schema_cmd as _schema_cmd
from .commands import checks as _checks_cmd
from .commands import doc as _doc_cmd
from .commands import doctor as _doctor_cmd
from .commands import drawing as _drawing_cmd
from .commands import fab as _fab_cmd
from .commands import gate as _gate_cmd
from .commands import jlc as _jlc_cmd
from .commands import library as _library_cmd
from .commands import log as _log_cmd
from .commands import project as _project_cmd
from .commands import query as _query_cmd
from .commands import render as _render_cmd
from .commands import release as _release_cmd
from .commands import review as _review_cmd
from .commands import sim as _sim_cmd
from .commands import view as _view_cmd
from .commands import _shared as _shared_mod
from .commands._shared import _ExitWith
from . import contract as _contract
from .errors import (
    ERROR_CODES,
    EXIT,
    AkcliError,
    as_error,
    remediation_for,
    to_exit,
)
from .ops import PROTOCOL_VERSION

# Stable seams: tests reference/patch these on the ``cli`` module directly.
from .commands.checks import _load_expected  # noqa: F401,E402


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


# Set by main() before parsing so _Parser.error can honor --json even though
# argparse aborts before the parsed namespace exists.
_JSON_REQUESTED = False


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose failures stay machine-readable under ``--json``.

    Plain argparse prints usage text to stderr and exits 2 — the one error
    class an agent hits most (wrong flag, wrong choice, unknown command) was
    also the only one that ignored ``--json``. ``add_subparsers`` defaults
    ``parser_class`` to ``type(self)``, so every subparser inherits this.
    """

    def error(self, message: str) -> NoReturn:
        if _JSON_REQUESTED:
            _emit_json_error("USAGE", f"{self.prog}: {message}", EXIT["USAGE"])
        self.print_usage(sys.stderr)
        self.exit(EXIT["USAGE"], f"{self.prog}: error: {message}\n")


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
    parser = _Parser(
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
    _render_cmd.register(sub, common)
    _doc_cmd.register(sub, common)
    _view_cmd.register(sub, common)
    _checks_cmd.register(sub, common)
    _review_cmd.register(sub, common)
    _drawing_cmd.register(sub, common)
    _calc_cmd.register(sub, common)
    _jlc_cmd.register(sub, common)
    _library_cmd.register(sub, common)
    _project_cmd.register(sub, common)
    _fab_cmd.register(sub, common)
    _release_cmd.register(sub, common)
    _gate_cmd.register(sub, common)
    _doctor_cmd.register(sub, common)
    _sim_cmd.register(sub, common)
    _capabilities_cmd.register(sub, common)
    _schema_cmd.register(sub, common)
    _log_cmd.register(sub, common)

    return parser


def main(argv: list[str] | None = None) -> int:
    global _JSON_REQUESTED
    raw = sys.argv[1:] if argv is None else list(argv)
    _JSON_REQUESTED = "--json" in raw
    # Reset BEFORE parsing: argparse failures reach _emit_json_error via
    # _Parser.error during parse_args, and a stale touched-flag from a prior
    # in-process main() call would silently swallow the JSON envelope.
    _shared_mod._reset_stdout_tracking()
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
        # ``akcli`` / ``akcli --json`` with no subcommand: argparse never
        # errors here (the subcommand is optional), so this was the ONE
        # non-zero exit that bypassed _emit_json_error and left stdout empty.
        if getattr(args, "json", False):
            _emit_json_error(
                "USAGE",
                "no command given; `akcli capabilities --json` lists every "
                "command and flag",
                EXIT["USAGE"])
        parser.print_help(sys.stderr)
        return EXIT["USAGE"]

    try:
        _validate_config_override(getattr(args, "config", None))
        return handler(args)
    except _ExitWith as exc:
        if getattr(args, "json", False) and exc.code != EXIT["OK"]:
            # Recover a real ERROR_CODES member when the raiser wrapped one
            # into the message ("ERROR: TARGET_LOCKED: ..."): the agent gets
            # the specific code + its remediation, not the EXIT category.
            msg = exc.msg.removeprefix("ERROR: ")
            code = _exit_name(exc.code)
            head, sep, rest = msg.partition(": ")
            if sep and head in ERROR_CODES:
                code, msg = head, rest
            _emit_json_error(code, msg, exc.code)
        if exc.msg:
            sys.stderr.write(exc.msg + "\n")
        return exc.code
    except AkcliError as exc:
        if getattr(args, "debug", False):
            raise
        if getattr(args, "json", False):
            _emit_json_error(exc.code, exc.message, to_exit(exc))
        sys.stderr.write(as_error(exc) + "\n")
        return to_exit(exc)
    except BrokenPipeError:
        # NOTE: must precede the OSError arm below — BrokenPipeError IS an
        # OSError, and a closed downstream pipe (``| head``) is a success.
        return EXIT["OK"]
    except FileNotFoundError as exc:
        if getattr(args, "debug", False):
            raise
        if getattr(args, "json", False):
            _emit_json_error("FILE_NOT_FOUND", str(exc.filename or exc),
                             to_exit(exc))
        sys.stderr.write(f"ERROR: file not found: {exc.filename or exc}\n")
        return to_exit(exc)
    except OSError as exc:
        # The rest of the OSError family a bad input path produces:
        # IsADirectoryError (POSIX "you passed a directory"), PermissionError
        # (mode 000; also how Windows reports a directory read),
        # NotADirectoryError, and plain IO errors. These are NOT bugs, so they
        # must never surface as INTERNAL + a traceback — they classify as
        # "that path is not a usable file" (exit 4), same as a missing file.
        if getattr(args, "debug", False):
            raise
        where = getattr(exc, "filename", None)
        message = f"{where}: {exc.strerror or exc}" if where else str(exc)
        if getattr(args, "json", False):
            _emit_json_error("FILE_UNREADABLE", message, to_exit(exc))
        sys.stderr.write(f"ERROR: FILE_UNREADABLE: {message}\n")
        return to_exit(exc)
    except UnicodeDecodeError as exc:
        # Undecodable bytes reached a text consumer no reader guard covers.
        # That is a corrupt/wrong-format INPUT (exit 3), not an akcli bug.
        if getattr(args, "debug", False):
            raise
        message = (f"input is not valid {exc.encoding.upper()} text "
                   f"(byte 0x{exc.object[exc.start]:02x} at offset {exc.start})")
        if getattr(args, "json", False):
            _emit_json_error("PARSE", message, to_exit(exc))
        sys.stderr.write(f"ERROR: PARSE: {message}\n")
        return to_exit(exc)
    except Exception as exc:  # noqa: BLE001 — last-resort envelope guard
        # An unexpected exception (a bug, or input mangled in a way no reader
        # guard anticipated) must still honour the CLI contract: stdout parses
        # as JSON under --json, stderr carries the traceback for the report.
        # Everything classifiable was classified above; INTERNAL now means
        # exactly "unknown", which is what its remediation promises.
        if getattr(args, "debug", False):
            raise
        import traceback
        if getattr(args, "json", False):
            _emit_json_error(
                "INTERNAL", f"unexpected {type(exc).__name__}: {exc}",
                EXIT["PARSE"])
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write(
            f"ERROR: INTERNAL: unexpected {type(exc).__name__}: {exc} "
            "(re-run with --debug to raise; please report this)\n")
        return EXIT["PARSE"]


def _validate_config_override(path: str | None) -> None:
    """Reject a bad ``-C/--config`` for EVERY command, before the handler runs.

    Checked here rather than in ``commands._shared._load_cfg`` because only
    some commands consult the config at all — ``read`` never does — and ``-C``
    must not be enforced by some subcommands and ignored by others. The rule
    itself lives in :func:`config.load_explicit_config`.
    """
    if not path:
        return
    from . import config as _config

    _config.load_explicit_config(path)


def _exit_name(code: int) -> str:
    """EXIT table name for a numeric exit code (e.g. 2 -> ``USAGE``)."""
    for name, value in EXIT.items():
        if value == code:
            return name
    return "USAGE"


def _emit_json_error(code: str, message: str, exit_code: int) -> None:
    """Under ``--json``, a failure is STILL machine-readable on stdout.

    The agent contract is "if I passed --json, stdout parses as JSON on every
    non-OK exit path that would otherwise leave stdout empty" — the plain
    ``ERROR:`` line keeps going to stderr for humans. ``remediation`` carries
    the actionable next step (``errors.REMEDIATION``). ``code`` is an
    ``ERROR_CODES`` member when an ``AkcliError`` carried one, otherwise the
    ``EXIT`` table name (``USAGE``, ``UNSUPPORTED_FORMAT``, ...) or
    ``FILE_NOT_FOUND``. Skipped when the handler already emitted data, so a
    failure after partial output never yields two JSON documents.

    ``akcli_version`` rides alongside the envelope's ``schema_version``: a bug
    report pasted from an agent transcript is otherwise a code and a message
    with no way to tell WHICH akcli produced them.
    """
    if _shared_mod._stdout_touched():
        return
    payload = {
        "schema_version": _contract.SCHEMA_VERSIONS["envelope"],
        "akcli_version": __version__,
        "error": {
            "code": code,
            "message": message,
            "exit": exit_code,
            "remediation": remediation_for(code),
        },
    }
    sys.stdout.write(_shared_mod._dumps(payload) + "\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
