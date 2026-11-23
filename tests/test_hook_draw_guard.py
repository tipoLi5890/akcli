"""The PreToolUse draw guard (hooks/pretooluse_draw_guard.py).

Contract: BLOCK (exit 2) a `draw --apply` whose op-list is structurally
invalid; WARN (exit 0 + stderr) when no prior plan/dry-run is journaled for
that exact op-list; and FAIL OPEN (exit 0, silent) on anything uncertain —
the CLI's own gates always stand behind the hook.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT

HOOK = Path(__file__).parent.parent / "hooks" / "pretooluse_draw_guard.py"
SRC = str(Path(__file__).parent.parent / "src")

GOOD_OPS = {
    "protocol_version": 2, "target_format": "kicad",
    "target_file": "board.kicad_sch",
    "ops": [{"op": "add_text", "at": [1000, 1000], "text": "hi"}],
}
BAD_OPS = {
    "protocol_version": 2, "target_format": "kicad",
    "target_file": "board.kicad_sch",
    "ops": [{"op": "add_wire", "from": [1, 2]}],
}


def _hook_env() -> dict:
    # Start from the real environment (Windows needs SYSTEMROOT etc. for a
    # child Python to even start) and pin the knobs the contract depends on:
    # AKCLI names the CLI explicitly, so PATH content is irrelevant to what
    # the tests prove. sys.executable may contain backslashes/spaces — the
    # hook must split it correctly on every platform.
    import os
    env = dict(os.environ)
    env.update({"PYTHONPATH": SRC,
                "AKCLI": f'"{sys.executable}" -m akcli'
                if " " in sys.executable else f"{sys.executable} -m akcli",
                "AKCLI_JLC_CACHE": "off"})
    return env


def run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=_hook_env(), timeout=60)


def bash_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    assert main(["new", str(tmp_path / "board.kicad_sch")]) == EXIT["OK"]
    (tmp_path / "good.json").write_text(json.dumps(GOOD_OPS), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps(BAD_OPS), encoding="utf-8")
    return tmp_path


def test_ignores_other_tools(workspace: Path):
    proc = run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}},
                    workspace)
    assert proc.returncode == 0 and not proc.stderr


def test_ignores_non_draw_commands(workspace: Path):
    proc = run_hook(bash_payload("akcli check board.kicad_sch"), workspace)
    assert proc.returncode == 0 and not proc.stderr


def test_blocks_invalid_oplist(workspace: Path):
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops bad.json --apply"),
        workspace)
    assert proc.returncode == 2
    assert "structurally invalid" in proc.stderr


def test_warns_without_prior_plan(workspace: Path):
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops good.json --apply"),
        workspace)
    assert proc.returncode == 0
    assert "no prior" in proc.stderr


def test_silent_after_plan(workspace: Path, monkeypatch):
    monkeypatch.chdir(workspace)
    assert main(["plan", "board.kicad_sch", "--ops", "good.json"]) == EXIT["OK"]
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops good.json --apply"),
        workspace)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_silent_after_plan_that_was_rotated_away(workspace: Path, monkeypatch):
    # The plan entry sits in the rotated generation: still a prior plan.
    monkeypatch.chdir(workspace)
    assert main(["plan", "board.kicad_sch", "--ops", "good.json"]) == EXIT["OK"]
    journal = workspace / ".akcli" / "journal.jsonl"
    journal.replace(journal.with_suffix(".jsonl.1"))
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops good.json --apply"),
        workspace)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_fails_open_on_garbage_stdin(workspace: Path):
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, cwd=workspace,
                          env=_hook_env(), timeout=60)
    assert proc.returncode == 0


def test_fails_open_on_missing_ops_file(workspace: Path):
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops nope.json --apply"),
        workspace)
    assert proc.returncode == 0 and not proc.stderr


# --- absolute-path resolution (the Windows fail-open class) ----------------
#
# On Windows the guard used to split the command with POSIX semantics, which
# consumes the backslashes in `--ops C:\proj\ops.json`; the resulting token
# ("C:projops.json") failed os.path.exists() and the guard fell through to
# fail-open on EVERY call. These tests pin absolute-path handling on the
# current platform and, via _tokenize, on Windows semantics from any host.


def _hook_module():
    spec = importlib.util.spec_from_file_location("_draw_guard", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_blocks_invalid_oplist_via_absolute_path(workspace: Path):
    """An absolute --ops path must still reach the validator (and block)."""
    ops = (workspace / "bad.json").resolve()
    proc = run_hook(
        bash_payload(f'akcli draw board.kicad_sch --ops "{ops}" --apply'),
        workspace)
    assert proc.returncode == 2
    assert "structurally invalid" in proc.stderr


def test_tokenize_preserves_windows_backslash_paths(monkeypatch):
    """Windows semantics, asserted from any host: backslashes survive.

    Would have failed before the fix — POSIX splitting turns the path into
    `C:projops.json`, which is exactly how the guard fails open on Windows.
    """
    mod = _hook_module()
    monkeypatch.setattr(mod.os, "name", "nt", raising=False)
    tokens = mod._tokenize(r'akcli draw C:\proj\board.kicad_sch '
                           r'--ops C:\proj\ops.json --apply')
    assert tokens is not None
    assert mod._extract(tokens, "--ops") == r"C:\proj\ops.json"
    assert mod._extract_target(tokens) == r"C:\proj\board.kicad_sch"


def test_tokenize_strips_quotes_on_windows(monkeypatch):
    mod = _hook_module()
    monkeypatch.setattr(mod.os, "name", "nt", raising=False)
    tokens = mod._tokenize(r'akcli draw board.kicad_sch '
                           r'--ops "C:\my proj\ops.json" --apply')
    assert tokens is not None
    assert mod._extract(tokens, "--ops") == r"C:\my proj\ops.json"


def test_tokenize_posix_absolute_path(monkeypatch):
    mod = _hook_module()
    monkeypatch.setattr(mod.os, "name", "posix", raising=False)
    tokens = mod._tokenize("akcli draw /tmp/p/board.kicad_sch "
                           "--ops /tmp/p/ops.json --apply")
    assert tokens is not None
    assert mod._extract(tokens, "--ops") == "/tmp/p/ops.json"


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_blocks_invalid_oplist_native_windows_path(workspace: Path):
    ops = str((workspace / "bad.json").resolve())
    assert "\\" in ops
    proc = run_hook(
        bash_payload(f"akcli draw board.kicad_sch --ops {ops} --apply"),
        workspace)
    assert proc.returncode == 2
    assert "structurally invalid" in proc.stderr


# --------------------------------------------------------------------------- #
# hooks/hooks.json wiring — the manifest CI only ever `jq .`-parsed.
#
# `jq .` proves the JSON is well-formed, not that the scripts it points at
# exist. A renamed/moved hook script would have shipped a plugin whose hook
# silently fails to start (Claude Code just logs and moves on).
# --------------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_HOOKS_JSON = _PLUGIN_ROOT / "hooks" / "hooks.json"
_PLUGIN_ROOT_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[^\s\"']+)")


def _hook_commands() -> list[str]:
    doc = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    out: list[str] = []
    for event, groups in doc["hooks"].items():
        assert isinstance(groups, list), f"{event}: expected a list of matcher groups"
        for group in groups:
            for entry in group.get("hooks", []):
                assert entry.get("type") == "command", f"{event}: non-command hook entry"
                cmd = entry.get("command")
                assert isinstance(cmd, str) and cmd, f"{event}: empty hook command"
                out.append(cmd)
    return out


def test_hooks_json_declares_commands():
    assert _hook_commands(), "hooks.json registers no hook commands"


def test_hooks_json_plugin_root_paths_exist():
    """Every ${CLAUDE_PLUGIN_ROOT}/... path in hooks.json resolves to a file."""
    referenced = 0
    for cmd in _hook_commands():
        for rel in _PLUGIN_ROOT_VAR.findall(cmd):
            referenced += 1
            target = _PLUGIN_ROOT / rel.lstrip("/")
            assert target.is_file(), (
                f"hooks.json points at {rel} which does not exist in the plugin "
                f"tree (resolved to {target})")
    assert referenced, (
        "no ${CLAUDE_PLUGIN_ROOT} path found in hooks.json — if the hooks stopped "
        "referencing plugin files, delete this gate deliberately")
