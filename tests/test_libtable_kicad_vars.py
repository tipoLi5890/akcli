"""KiCad's compiled-in ``${KICAD<N>_*_DIR}`` path variables.

KiCad's bundled default lib-tables are written entirely against
``${KICAD10_SYMBOL_DIR}`` & friends, but KiCad **compiles those in**: they are
absent from the environment and stored as `null` in `kicad_common.json`. A
pure `os.environ` expansion therefore resolved nothing on a clean install and
every standard nickname (`Device:R`) died as `SYMBOL_NOT_FOUND`. `libtable`
now derives them from the located `kicad-cli` install; the environment still
wins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from akcli import libtable
from akcli.cli import main

_MINI_LIB = """\
(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "R"
    (pin_names (offset 0) hide)
    (property "Reference" "R" (at 0 0 0))
    (symbol "R_0_1"
      (rectangle (start -1.016 2.54) (end 1.016 -2.54) (stroke (width 0.254)) (fill none))
    )
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
      (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
    )
  )
)
"""


@pytest.fixture
def fake_kicad(tmp_path: Path, monkeypatch):
    """A POSIX-prefix-shaped fake KiCad install; returns its symbols dir."""
    install = tmp_path / "kicad-install"
    exe = install / "bin" / "kicad-cli"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    syms = install / "share" / "kicad" / "symbols"
    syms.mkdir(parents=True)
    (syms / "Device.kicad_sym").write_text(_MINI_LIB, encoding="utf-8",
                                           newline="\n")
    (install / "share" / "kicad" / "footprints").mkdir()
    monkeypatch.setenv("KICAD_CLI", str(exe))
    return syms


@pytest.fixture
def no_kicad(tmp_path: Path, monkeypatch):
    """`kicad_cli.find()` returns None (KICAD_CLI points nowhere)."""
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "nope" / "kicad-cli"))


# --------------------------------------------------------------------------- #
# derivation rules (pure — no filesystem, so every platform shape is testable)
# --------------------------------------------------------------------------- #
def test_macos_app_bundle_root():
    roots = libtable.kicad_data_roots(
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    assert roots[0] == Path(
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport")


def test_posix_prefix_root():
    roots = libtable.kicad_data_roots("/usr/bin/kicad-cli")
    assert Path("/usr/share/kicad") in roots


def test_offprefix_posix_exe_still_finds_the_distro_default():
    # /usr/lib/kicad/bin/kicad-cli — its own prefix has no share/kicad
    roots = libtable.kicad_data_roots("/usr/lib/kicad/bin/kicad-cli")
    assert roots.index(Path("/usr/lib/kicad/share/kicad")) < \
        roots.index(Path("/usr/share/kicad"))


def test_windows_install_prefix_root():
    # forward slashes so the shape is assertable from any platform's Path
    roots = libtable.kicad_data_roots(
        "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")
    assert Path("C:/Program Files/KiCad/10.0/share/kicad") in roots


def test_no_kicad_cli_still_offers_the_distro_defaults():
    roots = libtable.kicad_data_roots(None)
    assert all(isinstance(r, Path) for r in roots)


# --------------------------------------------------------------------------- #
# expand_var
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("var,sub", [
    ("KICAD10_SYMBOL_DIR", "symbols"),
    ("KICAD9_SYMBOL_DIR", "symbols"),        # version-agnostic on purpose
    ("KICAD_SYMBOL_DIR", "symbols"),
    ("KICAD10_FOOTPRINT_DIR", "footprints"),
])
def test_builtin_vars_derive_from_the_install(fake_kicad, monkeypatch, var, sub):
    monkeypatch.delenv(var, raising=False)
    assert libtable.expand_var(var) == str(fake_kicad.parent / sub)


def test_environment_wins_over_the_builtin(fake_kicad, monkeypatch, tmp_path):
    override = tmp_path / "my-symbols"
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(override))
    assert libtable.expand_var("KICAD10_SYMBOL_DIR") == str(override)


def test_unknown_variable_stays_unresolved(fake_kicad, monkeypatch):
    monkeypatch.delenv("NOT_A_KICAD_VAR", raising=False)
    assert libtable.expand_var("NOT_A_KICAD_VAR") is None
    # a KiCad-shaped var with no directory behind it is unresolved too
    assert libtable.expand_var("KICAD10_3DMODEL_DIR") is None


def test_no_kicad_install_leaves_the_var_unresolved(no_kicad, monkeypatch):
    monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
    assert libtable.expand_var("KICAD10_SYMBOL_DIR") in (
        None, "/usr/share/kicad/symbols", "/usr/local/share/kicad/symbols")


# --------------------------------------------------------------------------- #
# end to end: the bundled default table shape resolves a standard nickname
# --------------------------------------------------------------------------- #
def _global_table(cfg: Path) -> Path:
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        '  (lib (name "Device")(type "KiCad")'
        '(uri "${KICAD10_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr ""))\n'
        ')\n', encoding="utf-8", newline="\n")
    return cfg


def test_default_table_uri_resolves_without_the_env_var(
        fake_kicad, tmp_path, monkeypatch):
    monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(_global_table(tmp_path / "cfg")))
    ws = libtable.discover(tmp_path)
    entry = ws.global_sym_table.get("Device")
    path, unresolved = ws.resolve_uri(entry.uri)
    assert not unresolved
    assert path == fake_kicad / "Device.kicad_sym"


def test_pins_resolves_a_standard_nickname(fake_kicad, tmp_path, monkeypatch,
                                           capsys):
    monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(_global_table(tmp_path / "cfg")))
    monkeypatch.chdir(tmp_path)
    assert main(["pins", "Device:R", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lib_id"] == "Device:R"
    assert len(payload["pins"]) == 2


# --------------------------------------------------------------------------- #
# doctor probe
# --------------------------------------------------------------------------- #
def test_doctor_symbols_probe_ok(fake_kicad, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(_global_table(tmp_path / "cfg")))
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--require", "symbols", "--json"]) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["symbols"]["ok"] is True
    assert "Device:R via" in checks["symbols"]["detail"]
    assert "/" in checks["symbols"]["detail"]      # as_posix, never backslashes
    assert "\\" not in checks["symbols"]["detail"]


def test_doctor_symbols_probe_missing_is_actionable(no_kicad, tmp_path,
                                                    monkeypatch, capsys):
    monkeypatch.delenv("KICAD10_SYMBOL_DIR", raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(_global_table(tmp_path / "cfg")))
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--require", "symbols", "--json"]) == 1
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["symbols"]["ok"] is False
    assert "KICAD10_SYMBOL_DIR" in checks["symbols"]["hint"]
