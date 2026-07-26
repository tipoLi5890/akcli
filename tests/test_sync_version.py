"""Tests for ``tools/sync_version.py`` — the pyproject->manifest version sync/drift gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "tools" / "sync_version.py"

# tools/ is not an importable package, so load the module by path.
_spec = importlib.util.spec_from_file_location("sync_version", _MODULE_PATH)
assert _spec and _spec.loader
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)


def _make_repo(tmp_path: Path, *, version: str | None, plugin_version=None, market_version=None,
               codex_version=None) -> Path:
    (tmp_path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".codex-plugin").mkdir(parents=True, exist_ok=True)

    if version is None:
        proj = '[project]\nname = "akcli"\n'
    else:
        proj = f'[project]\nname = "akcli"\nversion = "{version}"\n'
    (tmp_path / "pyproject.toml").write_text(proj, encoding="utf-8")

    plugin: dict = {"name": "altium-kicad"}
    if plugin_version is not None:
        plugin["version"] = plugin_version
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(plugin, indent=2) + "\n", encoding="utf-8"
    )

    entry: dict = {"name": "altium-kicad", "source": "./"}
    if market_version is not None:
        entry["version"] = market_version
    market = {"name": "altium-kicad", "plugins": [entry]}
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(market, indent=2) + "\n", encoding="utf-8"
    )

    codex: dict = {"name": "altium-kicad"}
    if codex_version is not None:
        codex["version"] = codex_version
    (tmp_path / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(codex, indent=2) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_read_pyproject_version_present(tmp_path: Path):
    repo = _make_repo(tmp_path, version="1.2.3")
    assert sv.read_pyproject_version(repo / "pyproject.toml") == "1.2.3"


def test_read_pyproject_version_absent(tmp_path: Path):
    repo = _make_repo(tmp_path, version=None)
    assert sv.read_pyproject_version(repo / "pyproject.toml") is None


def test_dev_mode_versionless_manifests_pass_check(tmp_path: Path):
    # Manifests without a version key are dev-mode and must never count as drift.
    repo = _make_repo(tmp_path, version="0.1.0")
    code, _ = sv.sync(repo, check=True)
    assert code == 0


def test_check_detects_drift(tmp_path: Path):
    repo = _make_repo(tmp_path, version="0.2.0", plugin_version="0.1.0", market_version="0.2.0")
    code, msgs = sv.sync(repo, check=True)
    assert code == 1
    assert any("DRIFT" in m and "plugin.json" in m for m in msgs)


def test_check_passes_when_aligned(tmp_path: Path):
    repo = _make_repo(tmp_path, version="0.2.0", plugin_version="0.2.0", market_version="0.2.0")
    code, _ = sv.sync(repo, check=True)
    assert code == 0


def test_write_fixes_drift(tmp_path: Path):
    repo = _make_repo(tmp_path, version="0.3.0", plugin_version="0.1.0", market_version="0.1.0")
    code, _ = sv.sync(repo, check=False)
    assert code == 0

    plugin = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((repo / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["version"] == "0.3.0"
    assert market["plugins"][0]["version"] == "0.3.0"

    # Idempotent: a re-check now passes.
    assert sv.sync(repo, check=True)[0] == 0


def test_write_does_not_inject_into_versionless_manifests(tmp_path: Path):
    repo = _make_repo(tmp_path, version="0.4.0")
    sv.sync(repo, check=False)
    plugin = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((repo / ".claude-plugin" / "marketplace.json").read_text())
    assert "version" not in plugin
    assert "version" not in market["plugins"][0]


def test_dev_mode_pyproject_without_version_is_noop(tmp_path: Path):
    repo = _make_repo(tmp_path, version=None, plugin_version="9.9.9")
    # No SoT version -> nothing to compare against -> success no-op even with a stray version.
    code, _ = sv.sync(repo, check=True)
    assert code == 0


def test_main_check_returns_exit_code(tmp_path: Path):
    repo = _make_repo(tmp_path, version="1.0.0", plugin_version="0.0.1")
    assert sv.main(["--check", "--root", str(repo)]) == 1
    assert sv.main(["--root", str(repo)]) == 0  # write fixes it
    assert sv.main(["--check", "--root", str(repo)]) == 0


def test_main_missing_pyproject(tmp_path: Path):
    assert sv.main(["--check", "--root", str(tmp_path)]) == 2


@pytest.mark.parametrize("rel", [sv.PLUGIN_REL, sv.MARKETPLACE_REL, sv.CODEX_PLUGIN_REL])
def test_missing_manifest_is_an_error_not_a_skip(tmp_path: Path, rel: str):
    """A manifest that vanished must fail, not silently reduce the sync scope."""
    repo = _make_repo(tmp_path, version="1.0.0", plugin_version="1.0.0",
                      market_version="1.0.0", codex_version="1.0.0")
    (repo / rel).unlink()
    code, msgs = sv.sync(repo, check=True)
    assert code == 2
    assert any("MISSING" in m and rel.rsplit("/", 1)[-1] in m for m in msgs)
    # write mode must fail the same way (no "fixed it" illusion)
    assert sv.sync(repo, check=False)[0] == 2


def test_repo_manifests_in_sync():
    # Guard the real repo: with a static pyproject version, manifests must not drift.
    code, _ = sv.sync(_REPO_ROOT, check=True)
    assert code == 0


def test_repo_manifests_all_declare_a_version():
    """All three shipped manifests are stamped (CHANGELOG "Versioning policy")."""
    version = sv.read_pyproject_version(_REPO_ROOT / "pyproject.toml")
    assert version
    plugin = json.loads((_REPO_ROOT / sv.PLUGIN_REL).read_text(encoding="utf-8"))
    codex = json.loads((_REPO_ROOT / sv.CODEX_PLUGIN_REL).read_text(encoding="utf-8"))
    market = json.loads((_REPO_ROOT / sv.MARKETPLACE_REL).read_text(encoding="utf-8"))
    assert plugin.get("version") == version
    assert codex.get("version") == version
    assert market["plugins"], "marketplace declares no plugins"
    for entry in market["plugins"]:
        assert entry.get("version") == version, f"{entry.get('name')} carries no synced version"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
