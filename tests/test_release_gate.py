"""The release workflow's CI-green gate (.github/scripts/ci_green_gate.sh).

The gate is the last thing standing between a pushed tag and a public release,
and it is the piece of the pipeline that cannot be exercised by pushing a tag
"just to see". Two properties are pinned here:

1. **Required-job lock-step.** The gate asserts the ``windows-latest`` matrix
   legs by exact job name (every historically red release — 0.4.0, 0.8.0,
   0.9.0, 0.10.0 — failed there and only there). Renaming or shrinking ci.yml's
   matrix without updating the gate would silently weaken it, so the two lists
   are compared here.

2. **The three states the gate must tell apart**, replayed against a mocked
   GitHub API: the workflow *never ran* on the SHA, it ran and went *red*, it
   ran and went *green*. The previous check-runs implementation could not see
   state 1 at all (the gate is itself a check run on the tagged SHA, so "zero
   check runs" was unreachable) and insta-failed the tag-with-commit race
   instead of waiting it out.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / ".github" / "scripts" / "ci_green_gate.sh"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"

_BASH = shutil.which("bash")
posix_bash_only = pytest.mark.skipif(
    _BASH is None or sys.platform == "win32",
    reason="gate script is POSIX bash and only ever runs on ubuntu-latest",
)


# --------------------------------------------------------------------------- #
# (1) required-job whitelist vs ci.yml's matrix
# --------------------------------------------------------------------------- #
def _gate_required_jobs() -> list[str]:
    text = GATE.read_text(encoding="utf-8")
    m = re.search(r"REQUIRED_JOBS_DEFAULT='([^']*)'", text)
    assert m, "ci_green_gate.sh lost its REQUIRED_JOBS_DEFAULT block"
    return [ln for ln in m.group(1).splitlines() if ln.strip()]


def _ci_test_job() -> tuple[str, list[str], list[str]]:
    """(name template, os list, python-version list) of ci.yml's `test` job."""
    text = CI_YML.read_text(encoding="utf-8")
    block = re.search(r"\n  test:\n(.*?)(?=\n  \w[\w-]*:\n)", text, re.S)
    assert block, "ci.yml lost its `test:` job"
    body = block.group(1)

    name = re.search(r"^    name: (.+)$", body, re.M)
    assert name, "ci.yml `test:` job lost its `name:`"

    def _list(key: str) -> list[str]:
        m = re.search(rf"^        {key}: \[(.+)\]$", body, re.M)
        assert m, f"ci.yml `test:` matrix lost its `{key}:` list"
        return [v.strip().strip('"').strip("'") for v in m.group(1).split(",")]

    return name.group(1).strip(), _list("os"), _list("python-version")


def test_gate_required_jobs_match_the_ci_matrix() -> None:
    template, oses, pys = _ci_test_job()
    assert "windows-latest" in oses, (
        "ci.yml dropped windows-latest from the test matrix — that is the OS "
        "every historically red release failed on")

    expected = [
        template.replace("${{ matrix.os }}", "windows-latest")
                .replace("${{ matrix.python-version }}", py)
        for py in pys
    ]
    assert _gate_required_jobs() == expected, (
        "REQUIRED_JOBS_DEFAULT in .github/scripts/ci_green_gate.sh drifted from "
        "ci.yml's test matrix. Update both in the same commit — a stale "
        "whitelist means the gate stops checking a matrix leg without saying so."
    )


def test_release_workflow_invokes_the_gate_script() -> None:
    text = RELEASE_YML.read_text(encoding="utf-8")
    assert "bash .github/scripts/ci_green_gate.sh" in text
    # the force bypass must still exist, and only on workflow_dispatch
    assert "inputs.force" in text
    assert "workflow_dispatch:" in text
    # the API the gate needs must be permitted
    assert "actions: read" in text


# --------------------------------------------------------------------------- #
# (2) scenario replay against a mocked GitHub API
# --------------------------------------------------------------------------- #
# The gate calls gh_runs inside a command substitution (a subshell), so the
# poll counter has to live in a file to survive across iterations.
_HARNESS = """\
set -uo pipefail
source "$GATE"
gh_runs() {
  n=$(cat "$FIXTURES/counter" 2>/dev/null || echo 0)
  n=$(( n + 1 ))
  printf '%s' "$n" > "$FIXTURES/counter"
  f="$FIXTURES/runs.$n.tsv"
  if [ ! -f "$f" ]; then f="$FIXTURES/runs.last.tsv"; fi
  cat "$f"
}
gh_jobs() { cat "$FIXTURES/jobs.tsv"; }
ci_green_gate
"""

_WIN_JOBS_OK = [
    ("test (windows-latest / py3.11)", "completed", "success"),
    ("test (windows-latest / py3.12)", "completed", "success"),
    ("test (windows-latest / py3.13)", "completed", "success"),
    ("test (windows-latest / py3.14)", "completed", "success"),
]


def _run_gate(tmp_path: Path, runs: list[list[tuple[str, ...]]],
              jobs: list[tuple[str, ...]], *, timeout: str = "0") -> subprocess.CompletedProcess:
    """Replay the gate with canned API responses.

    *runs* is one TSV payload per poll iteration (the last one repeats).
    """
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    for i, payload in enumerate(runs, start=1):
        (fixtures / f"runs.{i}.tsv").write_text(
            "".join("\t".join(r) + "\n" for r in payload), encoding="utf-8", newline="\n")
    (fixtures / "runs.last.tsv").write_text(
        "".join("\t".join(r) + "\n" for r in runs[-1]), encoding="utf-8", newline="\n")
    (fixtures / "jobs.tsv").write_text(
        "".join("\t".join(j) + "\n" for j in jobs), encoding="utf-8", newline="\n")

    harness = tmp_path / "harness.sh"
    harness.write_text(_HARNESS, encoding="utf-8", newline="\n")

    assert _BASH
    return subprocess.run(
        [_BASH, str(harness)],
        capture_output=True, text=True, timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GATE": str(GATE),
            "FIXTURES": str(fixtures),
            "REPO": "tipoLi5890/akcli",
            "SHA": "d34db33f",
            "POLL_INTERVAL": "0",
            "POLL_TIMEOUT": timeout,
        },
    )


@posix_bash_only
def test_gate_fails_when_ci_never_ran_the_commit(tmp_path: Path) -> None:
    """The bug this rewrite exists for: a tag on a commit CI never saw."""
    proc = _run_gate(tmp_path, runs=[[]], jobs=[])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "No ci.yml run exists" in proc.stderr
    assert "CI never ran this commit" in proc.stderr


@posix_bash_only
def test_gate_fails_when_the_ci_run_is_red(tmp_path: Path) -> None:
    proc = _run_gate(
        tmp_path,
        runs=[[("42", "completed", "failure", "2026-07-25T10:00:00Z")]],
        jobs=[("test (ubuntu-latest / py3.12)", "completed", "success"),
              ("test (windows-latest / py3.11)", "completed", "failure")],
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "concluded 'failure'" in proc.stderr
    assert "not green: test (windows-latest / py3.11) (failure)" in proc.stderr


@posix_bash_only
def test_gate_passes_when_the_ci_run_is_green(tmp_path: Path) -> None:
    proc = _run_gate(
        tmp_path,
        runs=[[("42", "completed", "success", "2026-07-25T10:00:00Z")]],
        jobs=[("ruff lint", "completed", "success"),
              ("browser ui (akcli view)", "completed", "skipped"),
              *_WIN_JOBS_OK],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CI green on d34db33f" in proc.stdout
    assert "run 42" in proc.stdout


@posix_bash_only
def test_gate_fails_when_a_required_matrix_leg_is_absent(tmp_path: Path) -> None:
    """A green run is not enough: the windows legs are asserted by name."""
    proc = _run_gate(
        tmp_path,
        runs=[[("42", "completed", "success", "2026-07-25T10:00:00Z")]],
        jobs=[("ruff lint", "completed", "success"), *_WIN_JOBS_OK[:-1]],
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "required job absent" in proc.stderr
    assert "test (windows-latest / py3.14)" in proc.stderr


@posix_bash_only
def test_gate_waits_out_a_run_that_is_still_in_progress(tmp_path: Path) -> None:
    """Tag pushed with its commit: the race is waited out, not insta-killed."""
    proc = _run_gate(
        tmp_path,
        runs=[
            [],  # no run yet
            [("42", "in_progress", "none", "2026-07-25T10:00:00Z")],
            [("42", "completed", "success", "2026-07-25T10:00:00Z")],
        ],
        jobs=[("ruff lint", "completed", "success"), *_WIN_JOBS_OK],
        timeout="120",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no ci.yml run for d34db33f yet" in proc.stdout
    assert "is 'in_progress'" in proc.stdout


@posix_bash_only
def test_gate_times_out_on_a_run_that_never_finishes(tmp_path: Path) -> None:
    proc = _run_gate(
        tmp_path,
        runs=[[("42", "in_progress", "none", "2026-07-25T10:00:00Z")]],
        jobs=[],
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "still 'in_progress'" in proc.stderr


@posix_bash_only
def test_gate_uses_the_newest_run_for_the_sha(tmp_path: Path) -> None:
    """Re-run after a red first attempt: the latest attempt decides."""
    proc = _run_gate(
        tmp_path,
        runs=[[("41", "completed", "failure", "2026-07-25T09:00:00Z"),
               ("42", "completed", "success", "2026-07-25T10:00:00Z")]],
        jobs=[*_WIN_JOBS_OK],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "run 42" in proc.stdout
