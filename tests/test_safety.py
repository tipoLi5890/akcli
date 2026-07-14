"""Tests for safety limits and safe IO helpers (safety.py)."""

from __future__ import annotations

import os

import pytest

from akcli import safety
from akcli.errors import AkcliError


def test_limits_are_positive_ints():
    for name in (
        "MAX_FILE_BYTES", "MAX_SECTORS", "MAX_RECORDS", "MAX_DIR_ENTRIES",
        "MAX_DECODED_BYTES", "MAX_SEXPR_DEPTH", "MAX_ATOM_BYTES", "MAX_NODES",
    ):
        val = getattr(safety, name)
        assert isinstance(val, int) and val > 0


def test_max_atom_below_ten_megabytes():
    # malformed corpus includes a 10 MB atom that must be rejected.
    assert safety.MAX_ATOM_BYTES < 10 * 1024 * 1024


def test_safe_path_allows_inside(tmp_path):
    base = tmp_path
    target = base / "sub" / "f.txt"
    resolved = safety.safe_path(base, "sub/f.txt")
    assert resolved == target.resolve()


def test_safe_path_rejects_escape(tmp_path):
    with pytest.raises(AkcliError) as ei:
        safety.safe_path(tmp_path, "../../etc/passwd")
    assert ei.value.code == "PATH_OUTSIDE_ROOT"


def test_safe_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported here")
    with pytest.raises(AkcliError) as ei:
        safety.safe_path(tmp_path, "link/secret")
    assert ei.value.code == "PATH_OUTSIDE_ROOT"


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "out.txt"
    safety.atomic_write_with_backup(target, "hello")
    assert target.read_text() == "hello"
    # no stray temp files left behind
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_backs_up_existing(tmp_path):
    target = tmp_path / "out.txt"
    backup = tmp_path / "bak"
    target.write_text("v1")
    safety.atomic_write_with_backup(target, b"v2", backup_dir=backup)
    assert target.read_text() == "v2"
    assert (backup / "out.txt.bak").read_text() == "v1"


def test_run_subprocess_missing_tool():
    with pytest.raises(AkcliError) as ei:
        safety.run_subprocess(["definitely-not-a-real-tool-xyz"], timeout=5)
    assert ei.value.code == "KICAD_CLI_MISSING"


def test_run_subprocess_runs_echo():
    # `python3` should be discoverable on every CI runner.
    import sys

    proc = safety.run_subprocess([sys.executable, "-c", "print('ok')"], timeout=30)
    assert proc.returncode == 0
    assert b"ok" in proc.stdout
