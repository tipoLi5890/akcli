#!/usr/bin/env python3
"""Agent-loop eval harness: does an LLM agent actually drive akcli correctly?

The rest of the test suite proves the *tool* is correct; this harness measures
the other half of the AI-native claim — an agent reading the skills/docs must
produce an op-list that akcli validates, applies behind the safety rails, and
that lands the intended netlist. Each task under ``tasks/`` is a small,
self-contained design request with a committed ground truth:

* ``task.md``          — the natural-language prompt an agent receives
* ``expected_nets.json`` — the target netlist (named nets -> pin members)
* ``reference_ops.json`` — a known-good solution (also the harness self-check:
  ``tests/test_agent_eval.py`` asserts every reference scores 1.0, so the
  tasks can never drift from real CLI behavior)

Scoring is deterministic and offline: the candidate op-list is structurally
validated (``akcli ops validate``), applied to a fresh sheet
(``akcli new`` + ``draw --apply --strict-nets``), and the resulting named nets
are compared against the ground truth (exact pin-membership match; unnamed
nets ignored; spurious extra named nets are reported and fail the task).

Modes::

    python tools/agent_eval/run_eval.py --reference            # harness self-check
    python tools/agent_eval/run_eval.py --ops-dir OUT/         # score OUT/<task>.json
    python tools/agent_eval/run_eval.py --agent-cmd 'claude -p "$(cat "$TASK_FILE")" > "$OPS_OUT"'

``--agent-cmd`` runs a shell command once per task with ``TASK_FILE`` (the
prompt path) and ``OPS_OUT`` (where the command must write the op-list JSON)
in the environment — plug in any LLM agent. Not wheel-packaged; dev-only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
TASKS_DIR = Path(__file__).resolve().parent / "tasks"
SYMBOLS = [
    REPO / "tests/fixtures/kicad/symbols/Device.kicad_sym",
    REPO / "tests/fixtures/kicad/symbols/power.kicad_sym",
]

if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _akcli(argv: list[str]) -> SimpleNamespace:
    """Run akcli IN-PROCESS (the golden-corpus tests' pattern).

    Scoring calls akcli 4x per task; a subprocess per call would pay an
    interpreter start each time for no isolation benefit — only the external
    agent command (``--agent-cmd``) runs as a subprocess.
    """
    from akcli.cli import main as _main  # lazy: after sys.path setup

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _main(list(argv))
    return SimpleNamespace(returncode=rc, stdout=out.getvalue(),
                           stderr=err.getvalue())


def _load_expected(task_dir: Path) -> dict[str, frozenset[str]]:
    doc = json.loads((task_dir / "expected_nets.json").read_text())
    return {n["name"]: frozenset(n["members"]) for n in doc["nets"]}


def score_ops(task_dir: Path, ops_path: Path) -> dict:
    """Score one candidate op-list against a task's ground-truth netlist."""
    result: dict = {"task": task_dir.name, "valid": False, "applied": False,
                    "score": 0.0, "matched": 0, "total": 0, "errors": []}
    expected = _load_expected(task_dir)
    result["total"] = len(expected)

    try:
        oplist = json.loads(ops_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        result["errors"].append(f"unreadable op-list: {exc}")
        return result

    with tempfile.TemporaryDirectory(prefix="akcli-eval-") as td:
        tmp = Path(td)
        sheet = tmp / "board.kicad_sch"
        new = _akcli(["new", str(sheet)])
        if new.returncode != 0:
            result["errors"].append(f"akcli new failed: {new.stderr.strip()}")
            return result

        # The harness owns the target filename; everything else is the agent's.
        if isinstance(oplist, dict):
            oplist["target_file"] = sheet.name
        cand = tmp / "candidate.json"
        cand.write_text(json.dumps(oplist), encoding="utf-8")

        val = _akcli(["ops", "validate", str(cand), "--json"])
        try:
            val_doc = json.loads(val.stdout)
        except json.JSONDecodeError:
            result["errors"].append(f"ops validate emitted no JSON: "
                                    f"{val.stderr.strip()[:300]}")
            return result
        result["valid"] = bool(val_doc.get("valid"))
        if not result["valid"]:
            result["errors"].extend(
                f"[{e.get('op_index')}] {e.get('code')}: {e.get('message')}"
                for e in val_doc.get("errors", []))
            return result

        draw = _akcli(["draw", str(sheet), "--ops", str(cand), "--apply",
                       "--strict-nets", "--no-erc",
                       *(a for s in SYMBOLS for a in ("--symbols", str(s)))])
        result["applied"] = draw.returncode == 0
        if not result["applied"]:
            result["errors"].append(
                f"draw --apply failed (exit {draw.returncode}): "
                f"{(draw.stderr or draw.stdout).strip()[:500]}")
            return result

        nets = _akcli(["nets", str(sheet), "--json"])
        doc = json.loads(nets.stdout)
        observed = {n["name"]: frozenset(n["members"])
                    for n in doc["nets"] if n["name"]}

    matched = sum(1 for name, members in expected.items()
                  if observed.get(name) == members)
    extras = sorted(set(observed) - set(expected))
    result["matched"] = matched
    result["extra_named_nets"] = extras
    for name, members in expected.items():
        got = observed.get(name)
        if got != members:
            result["errors"].append(
                f"net {name!r}: expected {sorted(members)}, "
                f"got {sorted(got) if got else 'MISSING'}")
    if extras:
        result["errors"].append(f"unexpected named nets: {extras}")
    result["score"] = round(matched / len(expected), 4) if expected else 1.0
    result["pass"] = (result["applied"] and matched == len(expected)
                      and not extras)
    return result


def _tasks() -> list[Path]:
    return sorted(d for d in TASKS_DIR.iterdir() if (d / "task.md").is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reference", action="store_true",
                      help="score the committed reference solutions "
                           "(harness self-check; must be 100%%)")
    mode.add_argument("--ops-dir", type=Path,
                      help="score pre-generated <task-name>.json op-lists")
    mode.add_argument("--agent-cmd",
                      help="shell command run once per task with TASK_FILE / "
                           "OPS_OUT in the environment")
    ap.add_argument("--json", action="store_true", help="emit a JSON report")
    args = ap.parse_args()

    results = []
    with tempfile.TemporaryDirectory(prefix="akcli-eval-out-") as td:
        for task in _tasks():
            if args.reference:
                ops_path = task / "reference_ops.json"
            elif args.ops_dir:
                ops_path = args.ops_dir / f"{task.name}.json"
            else:
                ops_path = Path(td) / f"{task.name}.json"
                env = dict(os.environ, TASK_FILE=str(task / "task.md"),
                           OPS_OUT=str(ops_path))
                proc = subprocess.run(args.agent_cmd, shell=True, env=env,
                                      capture_output=True, text=True)
                if proc.returncode != 0 or not ops_path.is_file():
                    results.append({"task": task.name, "pass": False,
                                    "score": 0.0,
                                    "errors": [f"agent command failed: "
                                               f"{proc.stderr.strip()[:300]}"]})
                    continue
            if not ops_path.is_file():
                results.append({"task": task.name, "pass": False, "score": 0.0,
                                "errors": [f"no op-list at {ops_path}"]})
                continue
            results.append(score_ops(task, ops_path))

    passed = sum(1 for r in results if r.get("pass"))
    if args.json:
        print(json.dumps({"tasks": results, "passed": passed,
                          "total": len(results)}, indent=2))
    else:
        for r in results:
            mark = "PASS" if r.get("pass") else "FAIL"
            print(f"{mark}  {r['task']}  score={r.get('score', 0.0)}")
            for e in r.get("errors", []):
                print(f"      {e}")
        print(f"\n{passed}/{len(results)} tasks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
