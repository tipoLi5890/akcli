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
        "KICAD_SEXPR_BAD_NUMBER",
        "KICAD_BAD_ENCODING",
        "INTERNAL",
        # writer / op-list / verify
        "SYMBOL_NOT_FOUND",
        "BAD_ANGLE",
        "NON_ORTHOGONAL_WIRE",
        "OFF_GRID",
        "OVERLAP",
        "VERIFY_FAILED",
        "OP_UNSUPPORTED",
        "HIERARCHICAL_UNSUPPORTED",
        # design-doc -> op-list generation (`akcli ops from-intent`): the
        # design document is well-formed but asks for something the generator
        # cannot honestly draw (pin addressed by name, wildcard members, ...)
        "DESIGN_UNSUPPORTED",
        "PROTOCOL_MISMATCH",
        "TARGET_LOCKED",
        "GROUP_UNKNOWN",
        "GROUP_NO_ORIGIN",
        # power-port / hierarchy document integrity (raised by the writer AND
        # emitted as a connectivity Finding code by ``verify``)
        "POWER_PORT_STACKED",
        "SHEET_FILE_MISSING",
        # sheet-tree root resolution (hierarchy-aware net-diff/gate scope)
        "PROJECT_ROOT_AMBIGUOUS",
        "PROJECT_ROOT_MISMATCH",
        # project scaffolding (`akcli project new` / `project init`)
        "PROJECT_EXISTS",
        "PROJECT_FILE_INVALID",
        "PROJECT_FILE_MISSING",
        # multi-sheet op-list envelope + write transaction
        "SHEET_UNKNOWN",
        "INSTANCE_REFS_REQUIRED",
        "TXN_ROLLBACK_FAILED",
        # safety / IO
        "PATH_OUTSIDE_ROOT",
        "FILE_UNREADABLE",
        # external tooling
        "KICAD_CLI_TIMEOUT",
        "KICAD_CLI_MISSING",
        "NGSPICE_MISSING",
        "NGSPICE_FAILED",
        # sim deck build / sim.json (raised before ngspice is ever loaded)
        "SIM_NO_GROUND",
        "SIM_NODE_COLLISION",
        "SIM_NODE_COUNT_MISMATCH",
        "SIM_VALUE_INVALID",
        "SIM_OP_ASSERT_UNSUPPORTED",
        # networked surface (jlc)
        "NETWORK",
        # RESERVED transport codes: the Altium live driver was removed (Altium is
        # import-only) and nothing raises these today — they stay registered
        # because the published error/exit tables are frozen.
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
    "KICAD_SEXPR_BAD_NUMBER": EXIT["PARSE"],
    # not decodable as UTF-8 at all -> the same "corrupt input" class as the
    # Altium container's ALTIUM_BAD_MAGIC: the bytes are not this format.
    "KICAD_BAD_ENCODING": EXIT["PARSE"],
    "INTERNAL": EXIT["PARSE"],
    # op-list / verify failures -> 6
    "SYMBOL_NOT_FOUND": EXIT["OPLIST"],
    "BAD_ANGLE": EXIT["OPLIST"],
    "NON_ORTHOGONAL_WIRE": EXIT["OPLIST"],
    "OFF_GRID": EXIT["OPLIST"],
    "OVERLAP": EXIT["OPLIST"],
    "VERIFY_FAILED": EXIT["OPLIST"],
    "OP_UNSUPPORTED": EXIT["OPLIST"],
    "HIERARCHICAL_UNSUPPORTED": EXIT["OPLIST"],
    "PROJECT_ROOT_AMBIGUOUS": EXIT["OPLIST"],
    "PROJECT_ROOT_MISMATCH": EXIT["USAGE"],
    # project scaffolding: refusing to overwrite is an ARGUMENT problem (the
    # caller wanted `project init`, or meant `--force`) -> 2; a corrupt
    # .kicad_pro is a parse failure -> 3; nothing to complete is -> 4.
    "PROJECT_EXISTS": EXIT["USAGE"],
    "PROJECT_FILE_INVALID": EXIT["PARSE"],
    "PROJECT_FILE_MISSING": EXIT["NOT_FOUND"],
    "SHEET_UNKNOWN": EXIT["OPLIST"],
    "INSTANCE_REFS_REQUIRED": EXIT["OPLIST"],
    "TXN_ROLLBACK_FAILED": EXIT["OPLIST"],
    # generation refusals are op-list-class failures: nothing was written and
    # the document (not the environment) is what must change -> 6
    "DESIGN_UNSUPPORTED": EXIT["OPLIST"],
    "PROTOCOL_MISMATCH": EXIT["OPLIST"],
    "TARGET_LOCKED": EXIT["OPLIST"],
    "GROUP_UNKNOWN": EXIT["OPLIST"],
    "GROUP_NO_ORIGIN": EXIT["OPLIST"],
    "POWER_PORT_STACKED": EXIT["OPLIST"],
    "SHEET_FILE_MISSING": EXIT["OPLIST"],
    # sim deck build: the schematic/spec cannot be turned into an honest deck;
    # nothing ran, so this is an op-list-class refusal (6), never a tool error.
    "SIM_NO_GROUND": EXIT["OPLIST"],
    "SIM_NODE_COLLISION": EXIT["OPLIST"],
    "SIM_NODE_COUNT_MISMATCH": EXIT["OPLIST"],
    "SIM_VALUE_INVALID": EXIT["OPLIST"],
    # the path exists but the process cannot read bytes out of it (a directory
    # passed where a file was expected, mode 000, an IO error) -> 4, the same
    # "the path you gave me is not a usable file" class as a missing file.
    "FILE_UNREADABLE": EXIT["NOT_FOUND"],
    # usage / config errors -> 2
    "PATH_OUTSIDE_ROOT": EXIT["USAGE"],
    "BAD_CONFIG": EXIT["USAGE"],
    # a sim.json that asks for something the engine cannot do -> 2 (config)
    "SIM_OP_ASSERT_UNSUPPORTED": EXIT["USAGE"],
    # external tooling -> 7
    "KICAD_CLI_TIMEOUT": EXIT["TOOL_MISSING"],
    "KICAD_CLI_MISSING": EXIT["TOOL_MISSING"],
    "NGSPICE_MISSING": EXIT["TOOL_MISSING"],
    "NGSPICE_FAILED": EXIT["TOOL_MISSING"],
    "NETWORK": EXIT["TOOL_MISSING"],
    # reserved bridge transport: a held lock behaves like TARGET_LOCKED (6); a
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
    "INTERNAL":
        "an unexpected exception escaped a command handler (this is an "
        "akcli bug) — re-run with --debug for the raw traceback and "
        "report it with the input file",
    "KICAD_SEXPR_BAD_NUMBER":
        "a numeric field in the file is not a valid number (corrupt merge, "
        "disk damage or hand edit) — open the file at the quoted token and "
        "repair it, or restore from backup",
    "KICAD_BAD_ENCODING":
        "the file is not valid UTF-8 text, so it is not a KiCad document at "
        "all (binary file, wrong path, or disk damage) — check the path, or "
        "restore/re-save the file from KiCad",
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
    "DESIGN_UNSUPPORTED":
        "fix the design document as the message says (pins are addressed by "
        "NUMBER, every referenced REF needs a components entry, one pin "
        "belongs to one net, no wildcards/bus-vector names); "
        "`akcli pins <lib_id>` lists a symbol's pin numbers",
    "HIERARCHICAL_UNSUPPORTED":
        "this op cannot target a hierarchical sub-sheet; apply it to the "
        "sheet file itself",
    "TARGET_LOCKED":
        "close the file in the KiCad GUI first (or pass --allow-open and "
        "File>Revert in KiCad afterwards); in an agent+GUI editing loop, gate "
        "every write batch on `akcli library check-lock .` (exit 6 while any "
        "KiCad file is open) instead of discovering the lock mid-apply",
    "PROTOCOL_MISMATCH":
        "regenerate the op-list against this build: "
        "`akcli ops template <op>` stamps the supported protocol_version",
    "VERIFY_FAILED":
        "nothing was written; fix the connectivity findings above and re-run",
    "GROUP_UNKNOWN":
        "the op tags a group the envelope's 'groups' map does not declare; "
        "add the entry with an origin ([x_mil, y_mil]) or drop the 'group' tag",
    "GROUP_NO_ORIGIN":
        "declare the group's origin as 'origin': [x_mil, y_mil] (50-mil grid "
        "recommended); grouped op coordinates are relative to it",
    "POWER_PORT_STACKED":
        "two power symbols sit on the same coordinate (one hides the other and "
        "their nets merge silently); keep one port per point — move the second "
        "(50-mil grid) or drop it, and `akcli draw` with a "
        "`delete_object` op (kind 'symbol') to clear a stacked leftover",
    "SHEET_FILE_MISSING":
        "the (sheet ...) points at a child .kicad_sch that does not exist next "
        "to this file; create it first with `akcli new <file>` (then re-run), "
        "or set `create: true` on the add_sheet op to have the same draw write "
        "a blank child, or fix the 'file' field — nets/check/render refuse the "
        "whole design while the child is missing",
    "PROJECT_ROOT_AMBIGUOUS":
        "several root sheets instantiate this file, so the tree to evaluate "
        "cannot be guessed; pass --root FILE to name the root (or --flat to "
        "evaluate this file alone, accepting the truncated net view)",
    "PROJECT_EXISTS":
        "the target directory already holds the project file(s) this would "
        "write; run `akcli project init DIR` to add only what is MISSING "
        "(never overwriting), or pass --force to overwrite (the previous "
        "bytes are kept under .akcli/backups/)",
    "PROJECT_FILE_INVALID":
        "the existing .kicad_pro is not valid JSON (or the named root file is "
        "not a kicad_sch document), so akcli will not report a healthy "
        "project over it; fix or remove the file, then re-run "
        "`akcli project init`",
    "PROJECT_FILE_MISSING":
        "no .kicad_pro/.kicad_sch was found where one is required; run "
        "`akcli project new DIR` to scaffold a project from scratch, or point "
        "the command at the project directory (`akcli project init DIR`)",
    "PROJECT_ROOT_MISMATCH":
        "the --root file's sheet tree does not contain the target; point "
        "--root at the schematic whose (sheet ...) chain reaches the target, "
        "or drop --root to auto-discover it",
    "SHEET_UNKNOWN":
        "the op's 'sheet' tag names an entry the envelope's 'sheets' map does "
        "not declare (or the declared file does not exist next to the "
        "target); declare it as \"sheets\": {\"<name>\": {\"file\": "
        "\"<child.kicad_sch>\"}} — `akcli new` the child first if needed, or "
        "set `create: true` on the add_sheet op that references it and the "
        "same draw writes the blank child",
    "INSTANCE_REFS_REQUIRED":
        "this sheet file is instantiated more than once, so a single "
        "designator would annotate BOTH instances with the same reference "
        "(a duplicate-refdes design); pass per-instance references — "
        "place_component / set_component_parameters: "
        "\"instance_designators\": {\"<path>\": \"R101\", ...} — the error "
        "message lists the instance paths to use",
    "TXN_ROLLBACK_FAILED":
        "a multi-file commit failed partway AND the automatic rollback could "
        "not restore every file; nothing is lost — the full pre-write backup "
        "set is under .akcli/backups/runs/<run_id>/ and the journal records "
        "the transaction; run `akcli undo <root> --run <run_id> --apply` to "
        "restore, then re-run the draw",
    # safety / IO
    "PATH_OUTSIDE_ROOT":
        "the path escapes the working root; use a path inside the project "
        "(no .. traversal or absolute paths outside it)",
    "FILE_UNREADABLE":
        "the path exists but its bytes cannot be read — check it is a file "
        "(not a directory) and that you have read permission on it",
    # external tooling
    "KICAD_CLI_TIMEOUT":
        "the kicad-cli run timed out; retry, or skip the advisory run with "
        "--no-erc (akcli's own gates still apply)",
    "KICAD_CLI_MISSING":
        "install KiCad (bundles kicad-cli) or set AKCLI_KICAD_CLI; "
        "`akcli doctor` shows what is discovered",
    # reserved transport codes (no live driver; see ERROR_CODES note)
    "BRIDGE_BUSY":
        "another bridge request is in flight; wait and retry (single-flight "
        "transport)",
    "BRIDGE_TIMEOUT":
        "the running Altium instance did not answer in time; check the "
        "DelphiScript listener is running, then retry",
    # ngspice engine
    "NGSPICE_MISSING":
        "no libngspice found — install KiCad (its bundled libngspice is "
        "auto-discovered) or point AKCLI_NGSPICE at the library; "
        "`akcli doctor --require ngspice` verifies. `sim --deck-only` still "
        "works without it",
    "NGSPICE_FAILED":
        "the engine ran but failed; inspect the deck with `sim --deck-only` "
        "(unresolved components / bad Sim.* fields are the usual cause)",
    # sim deck build / sim.json
    "SIM_NO_GROUND":
        "no net became SPICE node 0; pass `--gnd <net>` naming the reference "
        "net (`akcli nets <sch>` lists them), or add a GND power symbol",
    "SIM_NODE_COLLISION":
        "two different nets map to one ngspice node (node names are "
        "case-insensitive, and ngspice has no sheet hierarchy); rename one net "
        "so the names differ in more than case/punctuation, and give per-sheet "
        "local labels distinct names (MID_A/MID_B) or promote them to global "
        "labels when they really are one net",
    "SIM_NODE_COUNT_MISMATCH":
        "the element would be emitted with the wrong number of SPICE nodes "
        "for its device letter (R/C/L/D/V/I/B take 2, Q takes 3) — usually a "
        "2-terminal equivalent model on a part with more pads; add "
        "`\"pin_order\": [\"<pin>\", \"<pin>\"]` to the Sim.Pins field or the "
        "spec.models entry naming exactly which pads are the SPICE terminals",
    "SIM_VALUE_INVALID":
        "the component value is not a value SPICE can take; write it as a bare "
        "engineering value (10k, 4R7, 100nF) and move ratings/tolerance into "
        "another field, or set Sim.Device/Sim.Params (or a spec.models entry) "
        "to say explicitly what to simulate — `Sim.Enable=0` skips the part",
    "SIM_OP_ASSERT_UNSUPPORTED":
        "ngspice cannot run `.meas` against an operating-point analysis; "
        "declare a `dc` sweep (e.g. \"dc\": \"Vsupply 3.3 3.3 1\" with "
        "\"meas\": \"FIND v(mid) AT=3.3\") or a `tran` analysis and assert "
        "against that — an `op` analysis with no asserts is still fine",
    # networked surface (jlc)
    "NETWORK":
        "the jlc endpoint is unreachable; check network/proxy or set "
        "AKCLI_JLC_BASE_URL to a mirror — cached responses may still serve",
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
    """Map any exception onto a process exit code from the ``EXIT`` table.

    Used by ``cli.main`` for BOTH the structured ``AkcliError`` path and the
    unstructured fallbacks, so the non-``AkcliError`` branches below are live
    classification rules, not decoration: an OS-level "I cannot read that
    path" failure is exit 4 (``NOT_FOUND``) and a decoding failure is exit 3
    (``PARSE``). Only a genuinely unknown exception falls through to the
    ``INTERNAL``/parse default.
    """
    if isinstance(exc, AkcliError):
        return exit_for_code(exc.code)
    if isinstance(exc, BrokenPipeError):
        # a closed downstream pipe (``| head``) is not our failure
        return EXIT["OK"]
    if isinstance(exc, OSError):
        # FileNotFoundError / IsADirectoryError / NotADirectoryError /
        # PermissionError and any other errno-carrying IO failure
        return EXIT["NOT_FOUND"]
    if isinstance(exc, UnicodeDecodeError):
        return EXIT["PARSE"]
    return EXIT["PARSE"]


def as_error(exc: BaseException) -> str:
    """Render any exception as the agent-facing ``ERROR: CODE: message`` line."""
    if isinstance(exc, AkcliError):
        return exc.as_error_line()
    return f"ERROR: {type(exc).__name__}: {exc}"
