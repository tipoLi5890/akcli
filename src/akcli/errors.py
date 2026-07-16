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
        # Altium live bridge transport
        "BRIDGE_BUSY",
        "BRIDGE_TIMEOUT",
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
# 6 op-list/verify failure · 7 external tool missing · 8 query miss (the file
# parsed fine but the named net/component does not exist in it).
EXIT: dict[str, int] = {
    "OK": 0,
    "FINDINGS": 1,
    "USAGE": 2,
    "PARSE": 3,
    "NOT_FOUND": 4,
    "UNSUPPORTED_FORMAT": 5,
    "OPLIST": 6,
    "TOOL_MISSING": 7,
    "QUERY_MISS": 8,
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
    # Altium live bridge: a held lock behaves like TARGET_LOCKED (6); a
    # response time-out behaves like an unavailable external tool (7).
    "BRIDGE_BUSY": EXIT["OPLIST"],
    "BRIDGE_TIMEOUT": EXIT["TOOL_MISSING"],
    # binary fetch / auto-download integrity -> 7
    "BINFETCH_DOWNLOAD": EXIT["TOOL_MISSING"],
    "BINFETCH_CHECKSUM": EXIT["TOOL_MISSING"],
}


# --- Remediation hints (agent contract) --------------------------------------
# Actionable next step per error code — a failure tells the agent what to DO,
# not just what went wrong. One table for every surface (per-op ``OpResult``,
# the CLI top level, findings), covering EVERY member of ``ERROR_CODES`` so no
# error path is hint-less; ``test_errors`` asserts full coverage.
REMEDIATION: dict[str, str] = {
    # Altium / OLE2-CFBF reader — corrupt container/records
    "ALTIUM_BAD_MAGIC":
        "not an OLE2/CFBF file; re-export from Altium (File > Save As) or "
        "check you passed the right path",
    "ALTIUM_FAT_CYCLE":
        "the OLE2 container is corrupt (FAT chain cycle); re-export the "
        "document from Altium and retry",
    "ALTIUM_OOB_SECTOR":
        "the OLE2 container is corrupt (sector out of bounds); re-export the "
        "document from Altium and retry",
    "ALTIUM_BAD_SECTOR_SHIFT":
        "the OLE2 header is corrupt; re-export the document from Altium and "
        "retry",
    "ALTIUM_ALLOC_GUARD":
        "the file exceeds a safety allocation cap; if it is a genuine design "
        "file, report it (the caps live in akcli.safety)",
    "ALTIUM_MALFORMED":
        "the Altium records are corrupt or truncated; re-export from Altium "
        "and retry (`akcli read --strict` distinguishes empty from broken)",
    "ALTIUM_UNSUPPORTED":
        "the file is valid but uses a feature akcli does not decode yet; "
        "ask for a different export (e.g. text-record .SchLib, or KiCad "
        "format) — do NOT treat the file as corrupt",
    # KiCad S-expression reader
    "KICAD_SEXPR_DEPTH":
        "the S-expression nests deeper than the safety cap; if it is a "
        "genuine KiCad file, report it (caps live in akcli.safety)",
    "KICAD_SEXPR_UNTERMINATED":
        "the file ends mid-expression (truncated?); restore from backup or "
        "re-save from KiCad",
    "KICAD_SEXPR_TOOBIG":
        "the file exceeds a safety size cap; if it is a genuine KiCad file, "
        "report it (caps live in akcli.safety)",
    # writer / op-list / verify
    "SYMBOL_NOT_FOUND":
        "pass the defining library via --symbols <lib.kicad_sym> (or config "
        "[paths]); check spelling with `akcli pins <lib_id> --symbols ...`",
    "OFF_GRID":
        "snap the coordinate to the 50-mil grid (multiples of 50, "
        "e.g. 1025 -> 1000 or 1050); `akcli pins` prints exact pin coordinates",
    "NON_ORTHOGONAL_WIRE":
        "wires must be axis-aligned; route a diagonal as an L: "
        "[[x1,y1],[x2,y1],[x2,y2]]",
    "BAD_ANGLE":
        "rotation must be one of 0, 90, 180, 270",
    "OVERLAP":
        "another object occupies this position; shift by >= 100 mil or run "
        "`akcli arrange` after placing",
    "OP_UNSUPPORTED":
        "see `akcli ops list` for the vocabulary and `akcli ops template <op>` "
        "for the exact field shape; `akcli ops validate <file>` checks the "
        "whole document",
    "HIERARCHICAL_UNSUPPORTED":
        "this op cannot target a hierarchical sub-sheet; apply it to the "
        "sheet file itself",
    "TARGET_LOCKED":
        "close the file in the KiCad GUI first (or pass --allow-open and "
        "File>Revert in KiCad afterwards)",
    "PROTOCOL_MISMATCH":
        "regenerate the op-list against this build: "
        "`akcli ops template <op>` stamps the supported protocol_version",
    "VERIFY_FAILED":
        "nothing was written; fix the connectivity findings above and re-run",
    # safety / IO
    "PATH_OUTSIDE_ROOT":
        "the path escapes the working root; use a path inside the project "
        "(no .. traversal or absolute paths outside it)",
    # external tooling
    "KICAD_CLI_TIMEOUT":
        "the kicad-cli run timed out; retry, or skip the advisory run with "
        "--no-erc (akcli's own gates still apply)",
    "KICAD_CLI_MISSING":
        "install KiCad (bundles kicad-cli) or set AKCLI_KICAD_CLI; "
        "`akcli doctor` shows what is discovered",
    # Altium live bridge transport
    "BRIDGE_BUSY":
        "another bridge request is in flight; wait and retry (single-flight "
        "transport)",
    "BRIDGE_TIMEOUT":
        "the running Altium instance did not answer in time; check the "
        "DelphiScript listener is running, then retry",
    # binary fetch / auto-download integrity
    "BINFETCH_DOWNLOAD":
        "the download failed; check network access and retry (or install the "
        "tool manually — `akcli doctor` verifies it)",
    "BINFETCH_CHECKSUM":
        "the downloaded file failed its checksum; delete the cached copy and "
        "retry — never use the mismatched binary",
    # config
    "BAD_CONFIG":
        "fix akcli.toml (see examples/akcli.toml.example); `akcli doctor` "
        "validates config discovery",
}


# Generic hints for the --json error envelope when no ERROR_CODES member is
# available (usage-style failures carry only an EXIT-table category). Keyed by
# EXIT names + the envelope's FILE_NOT_FOUND pseudo-code — deliberately a
# SEPARATE table so the REMEDIATION <-> ERROR_CODES 1:1 audit stays exact.
EXIT_REMEDIATION: dict[str, str] = {
    "FINDINGS":
        "findings at or above the --fail-on threshold; inspect the report "
        "(waive or fix), or tune --fail-on",
    "USAGE":
        "check the exact flags with `akcli <command> --help` or "
        "`akcli capabilities --json`",
    "PARSE":
        "the input file is corrupt or truncated; restore from backup or "
        "re-export it",
    "NOT_FOUND":
        "check the path (file or directory does not exist)",
    "FILE_NOT_FOUND":
        "check the path (file or directory does not exist)",
    "UNSUPPORTED_FORMAT":
        "the file is valid but not a format this command accepts; "
        "`akcli read` reports what it is",
    "OPLIST":
        "nothing was written; `akcli ops validate <file>` explains the "
        "op-list, and the per-op errors above say what to fix",
    "TOOL_MISSING":
        "a required external tool or the network is unavailable; "
        "`akcli doctor` shows what is discovered",
    "QUERY_MISS":
        "the file parsed fine but the named entity does not exist in it; "
        "list candidates with `akcli nets`/`akcli read --summary`",
}


def remediation_for(code: str | None) -> str | None:
    """Actionable next-step hint for an ERROR code or EXIT-name pseudo-code."""
    return REMEDIATION.get(code or "") or EXIT_REMEDIATION.get(code or "")


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
