"""Offline packaging sanity for ``pyproject.toml``.

Guards the install-critical invariants that the build/twine CI jobs only catch
after a network round-trip: the TOML parses, the PEP 639 setuptools floor is
declared (bare ``license = "MIT"`` breaks setuptools < 77), the metadata
sections a release needs are present, package data for the shipped JSON
schemas is declared, and every console-script entry point resolves to a real
callable.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_build_system_declares_setuptools_77_floor(pyproject: dict):
    build = pyproject["build-system"]
    assert build["build-backend"] == "setuptools.build_meta"
    floors = [
        m.group(1)
        for req in build["requires"]
        if (m := re.fullmatch(r"setuptools\s*>=\s*(\d+)(?:\.\d+)*", req))
    ]
    assert floors, f"no 'setuptools>=N' requirement in {build['requires']!r}"
    assert all(int(f) >= 77 for f in floors), "PEP 639 bare license string needs setuptools>=77"


def test_core_project_metadata(pyproject: dict):
    proj = pyproject["project"]
    assert proj["name"] == "altium-kicad-cli"
    assert re.fullmatch(r"\d+\.\d+\.\d+", proj["version"])
    assert proj["license"] == "MIT"
    assert proj["requires-python"] == ">=3.11"
    # Every supported minor gets a Trove classifier (and CI matrix coverage).
    for minor in ("3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {minor}" in proj["classifiers"]


def test_project_urls_present_and_https(pyproject: dict):
    urls = pyproject["project"]["urls"]
    for key in ("Homepage", "Repository", "Changelog"):
        assert urls[key].startswith("https://"), f"{key}: {urls.get(key)!r}"


def test_dev_extra_has_the_tools_ci_uses(pyproject: dict):
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    names = {re.split(r"[<>=!\[ ]", req, maxsplit=1)[0] for req in dev}
    assert {"pytest", "jsonschema", "build", "twine", "ruff"} <= names


def test_package_data_declares_shipped_assets(pyproject: dict):
    data = pyproject["tool"]["setuptools"]["package-data"]
    assert "*.html" in data["altium_kicad_cli.webui"]
    assert "*.json" in data["altium_kicad_cli.schemas"]


def test_ruff_lint_gate_is_configured(pyproject: dict):
    # CI's `ruff check src tests` reads this table; its absence would silently
    # widen the gate to ruff defaults over the vendored tree.
    ruff = pyproject["tool"]["ruff"]
    assert any("_vendor" in pat for pat in ruff["extend-exclude"])
    assert ruff["lint"]["select"], "empty ruff select set"


def test_console_script_entry_points_resolve(pyproject: dict):
    scripts = pyproject["project"]["scripts"]
    assert set(scripts) == {"akcli", "altium-kicad-cli"}
    for target in scripts.values():
        modname, _, attr = target.partition(":")
        obj = importlib.import_module(modname)
        for part in attr.split("."):
            obj = getattr(obj, part)
        assert callable(obj), f"{target} is not callable"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
