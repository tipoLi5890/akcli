#!/usr/bin/env python3
"""PreToolUse guard for `akcli draw --apply` (Claude Code plugin hook).

Enforces, at the harness layer, the two disciplines the skills teach in prose:

1. **Validate before write** — the op-list is run through
   ``akcli ops validate``; a structurally invalid op-list BLOCKS the call
   (exit 2, stderr fed back to the model) before a subprocess ever touches
   the target.
2. **Plan before apply** — the workspace journal (``.akcli/journal.jsonl``)
   is checked for a prior ``plan``/dry-run entry with this op-list's sha256;
   a missing one WARNS (exit 0 + stderr) but never blocks.

Fail-open by design: any uncertainty (unparseable command, missing files,
no akcli on PATH) allows the call — every one of the CLI's own gates
(validation, connectivity verify, net diff, --strict-nets, atomic write)
still stands behind this hook. Pure stdlib; safe under any Python >= 3.8.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys


def _akcli_argv() -> list[str] | None:
    """How to invoke akcli here, or None (fail open)."""
    env = os.environ.get("AKCLI")
    if env:
        return shlex.split(env)
    exe = shutil.which("akcli")
    if exe:
        return [exe]
    return None


def _extract(tokens: list[str], flag: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def _extract_target(tokens: list[str]) -> str | None:
    """The positional after `draw` (skipping flag values)."""
    try:
        i = tokens.index("draw")
    except ValueError:
        return None
    skip_next = False
    for tok in tokens[i + 1:]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok not in (
                    "--apply", "--dry-run", "--no-net-diff", "--strict-nets",
                    "--allow-open", "--json", "-q", "--quiet", "--no-color",
                    "--debug"):
                skip_next = True  # a value-taking flag
            continue
        return tok
    return None


def _had_prior_plan(target: str, ops_sha: str) -> bool:
    journal = os.path.join(os.path.dirname(os.path.abspath(target)) or ".",
                           ".akcli", "journal.jsonl")
    try:
        with open(journal, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(entry, dict)
                and entry.get("ops_sha256") == ops_sha
                and entry.get("target") == os.path.basename(target)
                and entry.get("status") in ("dry-run", "applied")):
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "akcli" not in command or "draw" not in command or "--apply" not in command:
        return 0
    try:
        tokens = shlex.split(command)
    except ValueError:
        return 0
    ops_path = _extract(tokens, "--ops")
    if not ops_path or not os.path.exists(ops_path):
        return 0  # the CLI itself will report the missing file

    akcli = _akcli_argv()
    if akcli is None:
        return 0

    # gate 1: structural validation — invalid op-list blocks the call
    try:
        proc = subprocess.run(
            akcli + ["ops", "validate", ops_path],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode == 6:
        sys.stderr.write(
            "akcli draw guard: the op-list is structurally invalid — "
            "fix it before --apply:\n"
            + (proc.stdout or "") + (proc.stderr or ""))
        return 2  # block; stderr goes back to the model
    if proc.returncode != 0:
        return 0  # anything unexpected: fail open

    # gate 2 (advisory): was there a plan / dry-run for THIS op-list?
    target = _extract_target(tokens)
    if target and os.path.exists(target):
        try:
            with open(ops_path, "rb") as fh:
                ops_sha = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return 0
        if not _had_prior_plan(target, ops_sha):
            sys.stderr.write(
                "akcli draw guard: no prior `akcli plan`/dry-run recorded for "
                "this exact op-list — consider `akcli plan " + target
                + " --ops " + ops_path + "` first (net-diff preview).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
