"""Tests for :mod:`akcli.drivers.kicad_cli` — optional ERC wrapper (SPEC §3.7).

``kicad-cli`` is an **optional** secondary verifier; the primary gate is the pure
Python :mod:`..writers.connectivity`. The behavioural tests therefore ``skipif`` the
tool is not installed (the dev/CI mac has no KiCad), while the *graceful-degradation*
tests — that absence is non-fatal — run everywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from akcli.drivers import kicad_cli

FIX = Path(__file__).parent / "fixtures" / "kicad"
V8 = FIX / "board_v8.kicad_sch"

_HAVE = kicad_cli.find() is not None
needs_cli = pytest.mark.skipif(not _HAVE, reason="kicad-cli not installed")


def _force_absent(monkeypatch):
    """Blind every rung of the discovery ladder."""
    monkeypatch.delenv("KICAD_CLI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(kicad_cli, "_FALLBACKS", ())
    monkeypatch.setattr(kicad_cli, "_WIN_GLOB", "/nonexistent/*/kicad-cli")


# --------------------------------------------------------------------------- #
# graceful degradation (runs with or without kicad-cli)
# --------------------------------------------------------------------------- #
def test_available_matches_find():
    assert isinstance(kicad_cli.available(), bool)
    assert kicad_cli.available() == (kicad_cli.find() is not None)


def test_find_env_override_wins(monkeypatch, tmp_path):
    stub = tmp_path / "kicad-cli"
    stub.write_text("#!/bin/sh\n")
    monkeypatch.setenv("KICAD_CLI", str(stub))
    assert kicad_cli.find() == str(stub)
    # env set but pointing nowhere: explicit None, never a silent fallback
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing"))
    assert kicad_cli.find() is None


def test_find_fallback_rung(monkeypatch, tmp_path):
    _force_absent(monkeypatch)
    bundle = tmp_path / "bundled-kicad-cli"
    bundle.write_text("")
    monkeypatch.setattr(kicad_cli, "_FALLBACKS", (str(bundle),))
    assert kicad_cli.find() == str(bundle)


def test_windows_glob_prefers_newest_version():
    key = kicad_cli._version_key
    paths = [r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
             r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"]
    assert max(paths, key=key).startswith(r"C:\Program Files\KiCad\10.0")


def test_absent_tool_is_non_fatal(monkeypatch):
    # Force "tool absent" and assert every entry point degrades to None (no raise).
    _force_absent(monkeypatch)
    assert kicad_cli.available() is False
    assert kicad_cli.version() is None
    assert kicad_cli.erc(str(V8)) is None
    assert kicad_cli.netlist(str(V8)) is None


def test_parse_version_helper():
    assert kicad_cli._parse_version("8.0.4") == (8, 0, 4)
    assert kicad_cli._parse_version("kicad-cli 7.0.11+foo") == (7, 0, 11)
    assert kicad_cli._parse_version("no version here") is None


# --------------------------------------------------------------------------- #
# real kicad-cli (skipped when not installed)
# --------------------------------------------------------------------------- #
@needs_cli
def test_version_when_installed():
    v = kicad_cli.version()
    assert v is not None and isinstance(v[0], int)


@needs_cli
def test_erc_when_installed():
    rep = kicad_cli.erc(str(V8))
    assert rep is None or isinstance(rep, dict)
    if isinstance(rep, dict):
        assert "exit_code" in rep


@needs_cli
def test_netlist_when_installed():
    rep = kicad_cli.netlist(str(V8))
    assert rep is None or isinstance(rep, dict)
