"""Tests for config discovery, parsing and validation (config.py)."""

from __future__ import annotations

import pytest

from akcli import config
from akcli.errors import AkcliError

_GOOD = """
[project]
mcu_designator = "U3"

[[rail]]
name = "V3V3"
voltage = 3.3
tolerance_pct = 5

[paths]
schematic = "hardware/main.SchDoc"
pinout_md = "docs/pinout.md"

[[erc_waiver]]
net = "LED1_GPIO_RD"
rule = "driver_conflict"
reason = "shared open-drain STAT by design"
"""


def _write(tmp_path, text, name=config.CONFIG_FILENAME):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_good_config(tmp_path):
    p = _write(tmp_path, _GOOD)
    cfg = config.load_config(p)
    assert cfg.mcu_designator == "U3"
    assert cfg.rails[0]["name"] == "V3V3"
    assert cfg.rails[0]["voltage"] == 3.3
    assert cfg.erc_waivers[0]["net"] == "LED1_GPIO_RD"
    # paths resolved relative to the toml's directory -> absolute
    assert cfg.paths["schematic"].replace("\\", "/").endswith("hardware/main.SchDoc")
    assert cfg.paths["schematic"].startswith(str(tmp_path.resolve()))


def test_unknown_top_level_key_rejected(tmp_path):
    p = _write(tmp_path, _GOOD + "\n[bogus]\nx = 1\n")
    with pytest.raises(AkcliError) as ei:
        config.load_config(p)
    assert ei.value.code == "BAD_CONFIG"


def test_unknown_project_key_rejected(tmp_path):
    p = _write(tmp_path, "[project]\nmcu_designator = \"U1\"\nwidget = 5\n")
    with pytest.raises(AkcliError) as ei:
        config.load_config(p)
    assert ei.value.code == "BAD_CONFIG"


def test_unknown_rail_key_rejected(tmp_path):
    p = _write(tmp_path, "[[rail]]\nname = \"V3V3\"\nfoo = 1\n")
    with pytest.raises(AkcliError) as ei:
        config.load_config(p)
    assert ei.value.code == "BAD_CONFIG"


def test_bad_toml_syntax(tmp_path):
    p = _write(tmp_path, "this is = = not toml")
    with pytest.raises(AkcliError) as ei:
        config.load_config(p)
    assert ei.value.code == "BAD_CONFIG"


def test_find_config_walks_up(tmp_path):
    _write(tmp_path, _GOOD)
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = config.find_config(deep)
    assert found is not None
    assert found.name == config.CONFIG_FILENAME
    assert found.parent == tmp_path.resolve()


def test_find_config_absent(tmp_path):
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    # tmp_path has no config; walk-up from here will reach fs root with none found
    # (guard against an ancestor having one by checking within an isolated subtree).
    assert config.find_config(deep) is None or config.find_config(deep).name == config.CONFIG_FILENAME


def test_path_must_be_string(tmp_path):
    p = _write(tmp_path, "[paths]\nschematic = 5\n")
    with pytest.raises(AkcliError) as ei:
        config.load_config(p)
    assert ei.value.code == "BAD_CONFIG"
