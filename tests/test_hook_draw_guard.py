"""The PreToolUse draw guard (hooks/pretooluse_draw_guard.py).

Contract: BLOCK (exit 2) a `draw --apply` whose op-list is structurally
invalid; WARN (exit 0 + stderr) when no prior plan/dry-run is journaled for
that exact op-list; and FAIL OPEN (exit 0, silent) on anything uncertain —
the CLI's own gates always stand behind the hook.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from akcli.cli import main
from akcli.errors import EXIT

HOOK = Path(__file__).parent.parent / "hooks" / "pretooluse_draw_guard.py"
SRC = str(Path(__file__).parent.parent / "src")

GOOD_OPS = {
    "protocol_version": 1, "target_format": "kicad",
    "target_file": "board.kicad_sch",
    "ops": [{"op": "add_text", "at": [1000, 1000], "text": "hi"}],
}
BAD_OPS = {
    "protocol_version": 1, "target_format": "kicad",
    "target_file": "board.kicad_sch",
    "ops": [{"op": "add_wire", "from": [1, 2]}],
}


def run_hook(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC,
           "AKCLI": f"{sys.executable} -m akcli",
           "AKCLI_JLC_CACHE": "off"}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=env, timeout=60)


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


def test_fails_open_on_garbage_stdin(workspace: Path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC}
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, cwd=workspace,
                          env=env, timeout=60)
    assert proc.returncode == 0


def test_fails_open_on_missing_ops_file(workspace: Path):
    proc = run_hook(
        bash_payload("akcli draw board.kicad_sch --ops nope.json --apply"),
        workspace)
    assert proc.returncode == 0 and not proc.stderr
