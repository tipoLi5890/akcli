"""Tests for the error-code registry and exit-code table (errors.py)."""

from __future__ import annotations

import pytest

from akcli.errors import (
    ERROR_CODES,
    EXIT,
    AkcliError,
    as_error,
    exit_for_code,
    fail,
    to_exit,
)


def test_required_codes_present():
    required = {
        "ALTIUM_BAD_MAGIC", "ALTIUM_FAT_CYCLE", "ALTIUM_OOB_SECTOR",
        "ALTIUM_BAD_SECTOR_SHIFT", "ALTIUM_ALLOC_GUARD", "ALTIUM_MALFORMED",
        "ALTIUM_UNSUPPORTED",
        "KICAD_SEXPR_DEPTH", "KICAD_SEXPR_UNTERMINATED", "KICAD_SEXPR_TOOBIG",
        "SYMBOL_NOT_FOUND", "BAD_ANGLE", "NON_ORTHOGONAL_WIRE", "OFF_GRID",
        "OVERLAP", "VERIFY_FAILED", "OP_UNSUPPORTED", "HIERARCHICAL_UNSUPPORTED",
        "PROTOCOL_MISMATCH", "PATH_OUTSIDE_ROOT", "KICAD_CLI_TIMEOUT",
        "KICAD_CLI_MISSING", "BAD_CONFIG",
        # binary-fetch integrity codes
        "BINFETCH_DOWNLOAD", "BINFETCH_CHECKSUM",
    }
    assert required <= ERROR_CODES
    assert len(ERROR_CODES) == len(required)


def test_exit_table_values():
    assert EXIT["OK"] == 0
    assert EXIT["FINDINGS"] == 1
    assert EXIT["USAGE"] == 2
    assert EXIT["PARSE"] == 3
    assert EXIT["NOT_FOUND"] == 4
    assert EXIT["UNSUPPORTED_FORMAT"] == 5
    assert EXIT["OPLIST"] == 6
    assert EXIT["TOOL_MISSING"] == 7


def test_fail_raises_akclierror_with_code():
    with pytest.raises(AkcliError) as ei:
        fail("ALTIUM_BAD_MAGIC", "bad header")
    assert ei.value.code == "ALTIUM_BAD_MAGIC"
    assert "bad header" in ei.value.message


def test_fail_rejects_unknown_code():
    with pytest.raises(AkcliError) as ei:
        fail("NOT_A_REAL_CODE", "x")
    assert ei.value.code == "ALTIUM_MALFORMED"


def test_exit_mapping_categories():
    assert exit_for_code("ALTIUM_FAT_CYCLE") == EXIT["PARSE"]
    assert exit_for_code("KICAD_SEXPR_DEPTH") == EXIT["PARSE"]
    assert exit_for_code("OP_UNSUPPORTED") == EXIT["OPLIST"]
    assert exit_for_code("PROTOCOL_MISMATCH") == EXIT["OPLIST"]
    assert exit_for_code("BAD_CONFIG") == EXIT["USAGE"]
    assert exit_for_code("PATH_OUTSIDE_ROOT") == EXIT["USAGE"]
    assert exit_for_code("KICAD_CLI_MISSING") == EXIT["TOOL_MISSING"]


def test_every_code_has_exit_mapping():
    for code in ERROR_CODES:
        assert isinstance(exit_for_code(code), int)


def test_to_exit_for_filenotfound():
    assert to_exit(FileNotFoundError(2, "no", "x")) == EXIT["NOT_FOUND"]
    assert to_exit(ValueError("?")) == EXIT["PARSE"]
    assert to_exit(AkcliError("KICAD_CLI_TIMEOUT")) == EXIT["TOOL_MISSING"]


def test_as_error_line_format():
    e = AkcliError("OP_UNSUPPORTED", "no such op")
    assert e.as_error_line() == "ERROR: OP_UNSUPPORTED: no such op"
    assert as_error(e) == "ERROR: OP_UNSUPPORTED: no such op"
    assert as_error(ValueError("boom")).startswith("ERROR: ValueError")
    assert e.exit_code == EXIT["OPLIST"]
