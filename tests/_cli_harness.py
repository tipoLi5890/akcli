"""Shared run-the-CLI-in-process helpers for the test suite.

One implementation of the capture-stdout/parse-JSON harness (instead of a
per-file copy that drifts): tests import ``run_cli``/``json_of``/
``blank_sheet`` from here.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from akcli.cli import main


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``akcli`` in-process; returns ``(exit_code, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def json_of(argv: list[str], expect: int | tuple[int, ...] | None = None) -> dict:
    """Run ``akcli`` and parse stdout as one JSON document.

    ``expect`` pins the exit code(s); ``None`` accepts any (the caller
    asserts on payload content instead).
    """
    rc, out, _ = run_cli(argv)
    if expect is not None:
        codes = (expect,) if isinstance(expect, int) else expect
        assert rc in codes, f"{argv} -> exit {rc} (want {codes})"
    return json.loads(out)


def blank_sheet(tmp_path: Path, name: str = "board.kicad_sch") -> Path:
    """``akcli new`` a blank sheet under ``tmp_path`` and return its path."""
    target = tmp_path / name
    rc, _, err = run_cli(["new", str(target)])
    assert rc == 0, f"akcli new failed: {err}"
    return target
