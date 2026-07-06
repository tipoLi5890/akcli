#!/usr/bin/env python3
"""Keep the Claude-Code manifests' version fields in lock-step with ``pyproject.toml``.

``pyproject.toml`` is the single source of truth (SoT) for the project version. The two
plugin manifests may *optionally* carry a ``version`` field:

* ``.claude-plugin/plugin.json``      -> top-level ``"version"``
* ``.claude-plugin/marketplace.json`` -> ``"version"`` on each entry of ``"plugins"``
* ``.codex-plugin/plugin.json``       -> top-level ``"version"``

During active development the manifests deliberately ship **without** a ``version`` key
(commit-SHA versioning, per SPEC §5.1/§5.2). This tool therefore only ever touches a
manifest that *already declares* a version: a missing key is treated as "dev mode" and is
never drift and never injected. Once a release adds a ``version`` field, this tool (and the
CI ``--check`` gate) guarantees it never silently diverges from ``pyproject.toml``.

Usage::

    python tools/sync_version.py            # rewrite manifests in place to match pyproject
    python tools/sync_version.py --check     # exit 1 if any declared version drifts (CI gate)
    python tools/sync_version.py --root DIR  # operate on a repo other than the auto-detected one

Zero runtime dependencies (stdlib + ``tomllib``, Python >= 3.11).
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

PLUGIN_REL = ".claude-plugin/plugin.json"
MARKETPLACE_REL = ".claude-plugin/marketplace.json"
CODEX_PLUGIN_REL = ".codex-plugin/plugin.json"
PYPROJECT_REL = "pyproject.toml"


def repo_root() -> Path:
    """Best-effort repo root = parent of this file's ``tools/`` directory."""
    return Path(__file__).resolve().parent.parent


def read_pyproject_version(pyproject_path: Path) -> str | None:
    """Return ``[project].version`` from *pyproject_path*, or ``None`` if absent/dynamic."""
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, obj: dict) -> None:
    # Trailing newline; 2-space indent matches the hand-authored manifests.
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _plan_plugin(doc: dict, version: str) -> bool:
    """Return True if plugin.json declares a ``version`` that differs from *version*."""
    return "version" in doc and doc["version"] != version


def _plan_marketplace(doc: dict, version: str) -> bool:
    """Return True if any marketplace plugin entry declares a drifting ``version``."""
    drift = False
    for entry in doc.get("plugins", []) or []:
        if isinstance(entry, dict) and "version" in entry and entry["version"] != version:
            drift = True
    return drift


def _apply_plugin(doc: dict, version: str) -> None:
    if "version" in doc:
        doc["version"] = version


def _apply_marketplace(doc: dict, version: str) -> None:
    for entry in doc.get("plugins", []) or []:
        if isinstance(entry, dict) and "version" in entry:
            entry["version"] = version


def sync(root: Path, *, check: bool) -> tuple[int, list[str]]:
    """Sync (or, with *check*, verify) manifest versions against pyproject.

    Returns ``(exit_code, messages)``. ``exit_code`` is 0 on success/no-op, 1 on drift
    detected in check mode.
    """
    pyproject = root / PYPROJECT_REL
    if not pyproject.is_file():
        return 2, [f"sync_version: pyproject not found at {pyproject}"]

    version = read_pyproject_version(pyproject)
    if version is None:
        # Dev mode: SoT carries no static version, so there is nothing to stamp.
        return 0, ["sync_version: pyproject has no [project].version; nothing to sync (dev mode)"]

    messages: list[str] = []
    drifted = False

    targets = (
        (root / PLUGIN_REL, _plan_plugin, _apply_plugin),
        (root / MARKETPLACE_REL, _plan_marketplace, _apply_marketplace),
        # the Codex plugin manifest carries the same version as the Claude one
        (root / CODEX_PLUGIN_REL, _plan_plugin, _apply_plugin),
    )
    for path, plan, apply in targets:
        if not path.is_file():
            messages.append(f"sync_version: {path} missing; skipped")
            continue
        doc = _load_json(path)
        needs_update = plan(doc, version)
        if not needs_update:
            continue
        if check:
            drifted = True
            messages.append(f"sync_version: DRIFT in {path.name} (expected version {version!r})")
        else:
            apply(doc, version)
            _dump_json(path, doc)
            messages.append(f"sync_version: updated {path.name} -> {version}")

    if not messages:
        messages.append(f"sync_version: all declared versions match pyproject ({version})")
    return (1 if drifted else 0), messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail (exit 1) on version drift instead of fixing it")
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: auto-detected)")
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    code, messages = sync(root, check=args.check)
    stream = sys.stderr if code else sys.stdout
    for line in messages:
        print(line, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
