"""Error-code registry, exit-code table and the single ``AkcliError`` exception.

This module is the single source of truth for:

* ``ERROR_CODES`` — the frozen set of machine-readable error codes raised anywhere
  in the package. A raw traceback must never reach the agent (unless ``--debug``);
  readers/writers map their failures onto one of these codes.
* ``EXIT`` — the process exit-code table (see SPEC §8).
* ``AkcliError`` — the one exception type carrying a structured ``code`` + ``message``.
* ``fail`` / ``to_exit`` / ``as_error`` — helpers used by the CLI top level.
"""

from __future__ import annotations

from typing import NoReturn

# --- Frozen ERROR-code registry (SPEC §3.1) ---------------------------------
ERROR_CODES: frozenset[str] = frozenset(
    {
        # Altium / OLE2-CFBF reader
        "ALTIUM_BAD_MAGIC",
        "ALTIUM_FAT_CYCLE",
        "ALTIUM_OOB_SECTOR",
        "ALTIUM_BAD_SECTOR_SHIFT",
        "ALTIUM_ALLOC_GUARD",
        "ALTIUM_MALFORMED",
        "ALTIUM_UNSUPPORTED",
        # KiCad S-expression reader
        "KICAD_SEXPR_DEPTH",
        "KICAD_SEXPR_UNTERMINATED",
        "KICAD_SEXPR_TOOBIG",
        # writer / op-list / verify
        "SYMBOL_NOT_FOUND",
        "BAD_ANGLE",
        "NON_ORTHOGONAL_WIRE",
        "OFF_GRID",
        "OVERLAP",
        "VERIFY_FAILED",
        "OP_UNSUPPORTED",
        "HIERARCHICAL_UNSUPPORTED",
        "PROTOCOL_MISMATCH",
        "TARGET_LOCKED",
        # safety / IO
        "PATH_OUTSIDE_ROOT",
        # external tooling
        "KICAD_CLI_TIMEOUT",
        "KICAD_CLI_MISSING",
        # binary fetch / auto-download integrity
        "BINFETCH_DOWNLOAD",
        "BINFETCH_CHECKSUM",
        # config
        "BAD_CONFIG",
    }
)

# --- Exit-code table (SPEC §8) ----------------------------------------------
# 0 success/no findings · 1 check findings present · 2 usage/arg error ·
# 3 parse error (corrupt OLE2/sexpr) · 4 file not found · 5 unsupported format ·
# 6 op-list/verify failure · 7 external tool missing.
EXIT: dict[str, int] = {
    "OK": 0,
    "FINDINGS": 1,
    "USAGE": 2,
    "PARSE": 3,
    "NOT_FOUND": 4,
    "UNSUPPORTED_FORMAT": 5,
    "OPLIST": 6,
    "TOOL_MISSING": 7,
}

# Map each ERROR code onto the exit-code category it should surface as.
_CODE_EXIT: dict[str, int] = {
    # corrupt/malformed parse input -> 3
    "ALTIUM_BAD_MAGIC": EXIT["PARSE"],
    "ALTIUM_FAT_CYCLE": EXIT["PARSE"],
    "ALTIUM_OOB_SECTOR": EXIT["PARSE"],
    "ALTIUM_BAD_SECTOR_SHIFT": EXIT["PARSE"],
    "ALTIUM_ALLOC_GUARD": EXIT["PARSE"],
    "ALTIUM_MALFORMED": EXIT["PARSE"],
    # a well-formed file using a feature we don't decode yet -> 5 (not "corrupt")
    "ALTIUM_UNSUPPORTED": EXIT["UNSUPPORTED_FORMAT"],
    "KICAD_SEXPR_DEPTH": EXIT["PARSE"],
    "KICAD_SEXPR_UNTERMINATED": EXIT["PARSE"],
    "KICAD_SEXPR_TOOBIG": EXIT["PARSE"],
    # op-list / verify failures -> 6
    "SYMBOL_NOT_FOUND": EXIT["OPLIST"],
    "BAD_ANGLE": EXIT["OPLIST"],
    "NON_ORTHOGONAL_WIRE": EXIT["OPLIST"],
    "OFF_GRID": EXIT["OPLIST"],
    "OVERLAP": EXIT["OPLIST"],
    "VERIFY_FAILED": EXIT["OPLIST"],
    "OP_UNSUPPORTED": EXIT["OPLIST"],
    "HIERARCHICAL_UNSUPPORTED": EXIT["OPLIST"],
    "PROTOCOL_MISMATCH": EXIT["OPLIST"],
    "TARGET_LOCKED": EXIT["OPLIST"],
    # usage / config errors -> 2
    "PATH_OUTSIDE_ROOT": EXIT["USAGE"],
    "BAD_CONFIG": EXIT["USAGE"],
    # external tooling -> 7
    "KICAD_CLI_TIMEOUT": EXIT["TOOL_MISSING"],
    "KICAD_CLI_MISSING": EXIT["TOOL_MISSING"],
    # binary fetch / auto-download integrity -> 7
    "BINFETCH_DOWNLOAD": EXIT["TOOL_MISSING"],
    "BINFETCH_CHECKSUM": EXIT["TOOL_MISSING"],
}


class AkcliError(Exception):
    """Single structured exception type carrying a frozen ``code`` + message.

    ``str(err)`` renders ``"CODE: message"`` (or just ``"CODE"``); use
    :meth:`as_error_line` for the agent-facing ``"ERROR: CODE: message"`` form.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)

    @property
    def exit_code(self) -> int:
        return exit_for_code(self.code)

    def as_error_line(self) -> str:
        return "ERROR: " + (f"{self.code}: {self.message}" if self.message else self.code)


def exit_for_code(code: str) -> int:
    """Return the process exit code for a given ERROR code (default: parse error)."""
    return _CODE_EXIT.get(code, EXIT["PARSE"])


def fail(code: str, msg: str = "") -> NoReturn:
    """Raise an :class:`AkcliError`. ``code`` must be a member of ``ERROR_CODES``."""
    if code not in ERROR_CODES:
        raise AkcliError("ALTIUM_MALFORMED", f"internal: unknown error code {code!r}")
    raise AkcliError(code, msg)


def to_exit(exc: BaseException) -> int:
    """Map any exception onto a process exit code from the ``EXIT`` table."""
    if isinstance(exc, AkcliError):
        return exit_for_code(exc.code)
    if isinstance(exc, FileNotFoundError):
        return EXIT["NOT_FOUND"]
    if isinstance(exc, (IsADirectoryError, PermissionError)):
        return EXIT["NOT_FOUND"]
    return EXIT["PARSE"]


def as_error(exc: BaseException) -> str:
    """Render any exception as the agent-facing ``ERROR: CODE: message`` line."""
    if isinstance(exc, AkcliError):
        return exc.as_error_line()
    return f"ERROR: {type(exc).__name__}: {exc}"
