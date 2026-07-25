"""Symbol discovery through the lib-tables (``_shared._draw_symbol_sources``).

``place_component Device:R`` used to fail ``SYMBOL_NOT_FOUND`` on a blank sheet
unless the caller repeated ``--symbols /path/to/Device.kicad_sym``, even though
:mod:`akcli.libtable` can resolve the nickname through the project / per-user
global ``sym-lib-table``. These tests pin the fallback:

* explicit sources still win (a nickname already passed in is never looked up);
* a nickname registered in the global table resolves (hermetically: the table is
  a fixture pointed at by ``KICAD_CONFIG_HOME``, never the machine's KiCad);
* an unregistered nickname still fails ``SYMBOL_NOT_FOUND`` / exit 6.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.commands._shared import (
    _draw_symbol_sources,
    _oplist_lib_ids,
)
from akcli.errors import EXIT

FIX = Path(__file__).parent / "fixtures" / "kicad"
SYMBOLS = FIX / "symbols"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _global_table(tmp_path: Path, monkeypatch, *, entries=("Device", "power")) -> Path:
    """A hermetic per-user global ``sym-lib-table`` over the symbol fixtures."""
    cfg = tmp_path / "kicad_cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    lines = ["(sym_lib_table (version 7)"]
    for name in entries:
        uri = (SYMBOLS / f"{name}.kicad_sym").as_posix()
        lines.append(f'  (lib (name "{name}")(type "KiCad")(uri "{uri}")'
                     '(options "")(descr ""))')
    lines.append(")\n")
    (cfg / "sym-lib-table").write_text("\n".join(lines), encoding="utf-8",
                                       newline="\n")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))
    return cfg


def _empty_config_home(tmp_path: Path, monkeypatch) -> Path:
    """An empty config root: KICAD_CONFIG_HOME REPLACES the platform default,
    so global-table discovery is hermetically disabled."""
    cfg = tmp_path / "kicad_cfg_empty"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))
    return cfg


def _blank_sheet(tmp_path: Path) -> Path:
    tgt = tmp_path / "proj" / "sheet.kicad_sch"
    assert main(["new", str(tgt)]) == EXIT["OK"]
    return tgt


def _ops_file(tmp_path: Path, ops: list) -> Path:
    p = tmp_path / "ops.json"
    p.write_text(json.dumps(
        {"protocol_version": 1, "target_format": "kicad", "ops": ops}),
        encoding="utf-8", newline="\n")
    return p


_PLACE_R = [{"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 2000, "y_mil": 1000, "value": "10k"}]


class _Cfg:
    paths: dict = {}


def _args(symbols=None) -> argparse.Namespace:
    return argparse.Namespace(symbols=list(symbols or []))


# --------------------------------------------------------------------------- #
# source collection unit level
# --------------------------------------------------------------------------- #
def test_oplist_lib_ids_dedups_in_first_seen_order():
    oplist = {"ops": [
        {"op": "place_component", "lib_id": "Device:R"},
        {"op": "add_wire", "vertices": ["R1.1", "R1.2"]},
        {"op": "place_power_port", "lib_id": "power:GND"},
        {"op": "place_component", "lib_id": "Device:R"},
    ]}
    assert _oplist_lib_ids(oplist) == ["Device:R", "power:GND"]
    assert _oplist_lib_ids({}) == []
    assert _oplist_lib_ids(None) == []


def test_no_lib_ids_means_no_discovery(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    assert _draw_symbol_sources(_args(), _Cfg()) == []


def test_nickname_resolves_through_global_table(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    sources = _draw_symbol_sources(_args(), _Cfg(), lib_ids=["Device:R"],
                                   project_dir=tmp_path)
    assert [Path(s).name for s in sources] == ["Device.kicad_sym"]


def test_explicit_sources_win_and_suppress_lookup(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    explicit = str(tmp_path / "mine" / "Device.kicad_sym")
    sources = _draw_symbol_sources(_args([explicit]), _Cfg(),
                                   lib_ids=["Device:R"], project_dir=tmp_path)
    assert sources == [explicit]        # same nickname: never looked up


def test_discovered_sources_are_appended_after_explicit(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    explicit = str(tmp_path / "mine" / "other.kicad_sym")
    sources = _draw_symbol_sources(_args([explicit]), _Cfg(),
                                   lib_ids=["Device:R", "power:GND"],
                                   project_dir=tmp_path)
    assert sources[0] == explicit       # explicit first: it must win resolution
    assert [Path(s).name for s in sources[1:]] == ["Device.kicad_sym",
                                                  "power.kicad_sym"]


def test_project_table_takes_precedence_over_global(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    local = proj / "local.kicad_sym"
    local.write_bytes((SYMBOLS / "Device.kicad_sym").read_bytes())
    (proj / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        '  (lib (name "Device")(type "KiCad")(uri "${KIPRJMOD}/local.kicad_sym")'
        '(options "")(descr ""))\n)\n', encoding="utf-8", newline="\n")
    sources = _draw_symbol_sources(_args(), _Cfg(), lib_ids=["Device:R"],
                                   project_dir=proj)
    assert [Path(s).name for s in sources] == ["local.kicad_sym"]


def test_unregistered_nickname_yields_no_source(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch, entries=("power",))
    assert _draw_symbol_sources(_args(), _Cfg(), lib_ids=["Device:R"],
                                project_dir=tmp_path) == []


def test_missing_global_table_is_not_an_error(tmp_path, monkeypatch):
    _empty_config_home(tmp_path, monkeypatch)
    assert _draw_symbol_sources(_args(), _Cfg(), lib_ids=["Device:R"],
                                project_dir=tmp_path) == []


def test_unresolvable_uri_variable_is_skipped(tmp_path, monkeypatch):
    cfg = tmp_path / "kicad_cfg_var"
    cfg.mkdir()
    (cfg / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        '  (lib (name "Device")(type "KiCad")'
        '(uri "${AKCLI_NOT_A_REAL_VAR}/Device.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8", newline="\n")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))
    assert _draw_symbol_sources(_args(), _Cfg(), lib_ids=["Device:R"],
                                project_dir=tmp_path) == []


# --------------------------------------------------------------------------- #
# CLI level: draw / pins on a blank sheet, no --symbols
# --------------------------------------------------------------------------- #
def test_draw_places_device_r_without_symbols_flag(tmp_path, monkeypatch):
    _global_table(tmp_path, monkeypatch)
    tgt = _blank_sheet(tmp_path)
    ops = _ops_file(tmp_path, _PLACE_R)
    rc = main(["draw", str(tgt), "--ops", str(ops), "--apply"])
    assert rc == EXIT["OK"]
    text = tgt.read_text(encoding="utf-8")
    assert '"Device:R"' in text          # cached from the discovered library
    assert '"R1"' in text


def test_draw_still_fails_when_nickname_is_unregistered(tmp_path, monkeypatch):
    _empty_config_home(tmp_path, monkeypatch)
    tgt = _blank_sheet(tmp_path)
    ops = _ops_file(tmp_path, _PLACE_R)
    before = tgt.read_bytes()
    assert main(["draw", str(tgt), "--ops", str(ops), "--apply"]) == EXIT["OPLIST"]
    assert tgt.read_bytes() == before    # nothing written


def test_pins_resolves_through_global_table(tmp_path, monkeypatch, capsys):
    _global_table(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    rc = main(["pins", "Device:R", "--at", "2000", "1000", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT["OK"]
    assert {p["number"] for p in payload["pins"]} == {"1", "2"}


@pytest.mark.parametrize("cmd", ["pins", "bbox"])
def test_lookup_without_a_table_still_reports_not_found(tmp_path, monkeypatch, cmd):
    _empty_config_home(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main([cmd, "Device:R", "--json"]) == EXIT["OPLIST"]
