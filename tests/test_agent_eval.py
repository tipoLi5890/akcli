"""Harness-integrity gate for the agent-loop eval (tools/agent_eval/).

No LLM runs in CI; what CI *can* verify is that the harness itself is sound:
every committed reference solution must validate, apply behind the real
safety rails, and score a perfect match against its task's ground truth.
This pins tasks + expected netlists + scorer to real CLI behavior, so a
drifted task (or a CLI change that breaks the eval corpus) fails here first
— and a model run scored with the same harness is trustworthy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "tools" / "agent_eval"
TASKS = sorted(d for d in (EVAL_DIR / "tasks").iterdir()
               if (d / "task.md").is_file())

sys.path.insert(0, str(EVAL_DIR))
import run_eval  # noqa: E402


def test_task_corpus_present():
    assert len(TASKS) >= 5, "the eval corpus shrank below its floor"
    for task in TASKS:
        assert (task / "expected_nets.json").is_file(), task.name
        assert (task / "reference_ops.json").is_file(), task.name


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_solution_scores_perfect(task: Path):
    result = run_eval.score_ops(task, task / "reference_ops.json")
    assert result["valid"], result["errors"]
    assert result["applied"], result["errors"]
    assert result["pass"], result["errors"]
    assert result["score"] == 1.0
