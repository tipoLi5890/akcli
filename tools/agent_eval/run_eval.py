#!/usr/bin/env python3
"""Agent-loop eval harness: does an LLM agent actually drive akcli correctly?

The rest of the test suite proves the *tool* is correct; this harness measures
the other half of the AI-native claim — an agent reading the skills/docs must
produce an op-list that akcli validates, applies behind the safety rails, and
that lands the intended netlist. Each task under ``tasks/`` is a small,
self-contained design request with a committed ground truth:

* ``task.md``          — the natural-language prompt an agent receives
* ``expected_nets.json`` — the target netlist (named nets -> pin members)
* ``expected_facts.json`` — optional ground truth beyond the netlist (part
  position/value/absence, no-connects, power symbols, annotation objects, the
  net diff an edit must report) — see ``eval_facts.py``
* ``reference_ops.json`` — a known-good solution (also the harness self-check:
  ``tests/test_agent_eval.py`` asserts every reference scores 1.0, so the
  tasks can never drift from real CLI behavior)
* ``setup/``           — optional pre-made sheets copied into the workspace.
  ``setup/board.kicad_sch`` makes the task an EDIT task: the op-list is
  applied to that existing schematic instead of a blank sheet (it is
  generated from ``setup/setup_ops.json`` by ``build_setups.py``)

Scoring is deterministic and offline: the candidate op-list is structurally
validated (``akcli ops validate``), applied to a fresh sheet — or the task's
starting schematic — (``akcli new`` + ``draw --apply --strict-nets``), and the
resulting named nets are compared against the ground truth (exact
pin-membership match; unnamed nets and auto-numbered power-symbol pins
ignored; spurious extra named nets are reported and fail the task). Facts,
when the task ships them, are each one more check.

Modes::

    python tools/agent_eval/run_eval.py --reference            # harness self-check
    python tools/agent_eval/run_eval.py --ops-dir OUT/         # score OUT/<task>.json
    python tools/agent_eval/run_eval.py --agent-cmd 'claude -p "$(cat "$TASK_FILE")" > "$OPS_OUT"'
    python tools/agent_eval/run_eval.py --coverage             # which task authors which op

``--agent-cmd`` runs a shell command once per task with ``TASK_FILE`` (the
prompt path), ``TASK_DIR`` (its directory — an edit task's starting schematic
is ``$TASK_DIR/setup/board.kicad_sch``) and ``OPS_OUT`` (where the command
must write the op-list JSON) in the environment — plug in any LLM agent. Not
wheel-packaged; dev-only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
TASKS_DIR = EVAL_DIR / "tasks"
BOARD = "board.kicad_sch"
# Every symbol a task may place. Resolution matches the bare symbol NAME after
# the colon, so one file serves any library nickname (``Device:LED`` resolves
# from EvalParts). EvalParts carries what the vendored fixtures lack and the
# macros hard-code or need: LED, Crystal, VCC, PWR_FLAG and a dual op-amp.
SYMBOLS = [
    REPO / "tests/fixtures/kicad/symbols/Device.kicad_sym",
    REPO / "tests/fixtures/kicad/symbols/power.kicad_sym",
    REPO / "tests/fixtures/kicad/symbols/Generic.kicad_sym",
    EVAL_DIR / "symbols" / "EvalParts.kicad_sym",
]
SYMBOL_ARGS = [a for s in SYMBOLS for a in ("--symbols", str(s))]

for _p in (str(REPO / "src"), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_facts  # noqa: E402  (sibling module; needs EVAL_DIR on sys.path)


_NO_KICAD_CONFIG: tempfile.TemporaryDirectory | None = None


def _no_kicad_config() -> str:
    """An empty directory to stand in for the machine's KiCad config root."""
    global _NO_KICAD_CONFIG
    if _NO_KICAD_CONFIG is None:
        _NO_KICAD_CONFIG = tempfile.TemporaryDirectory(prefix="akcli-eval-cfg-")
    return _NO_KICAD_CONFIG.name


def _akcli(argv: list[str]) -> SimpleNamespace:
    """Run akcli IN-PROCESS (the golden-corpus tests' pattern).

    Scoring calls akcli 4x per task; a subprocess per call would pay an
    interpreter start each time for no isolation benefit — only the external
    agent command (``--agent-cmd``) runs as a subprocess.

    The call is HERMETIC: ``KICAD_CONFIG_HOME`` points at an empty directory,
    so a ``lib_id`` whose nickname none of :data:`SYMBOLS` is named after can
    never fall back to the scoring machine's global ``sym-lib-table`` — a
    candidate resolves the same symbols (or the same ``SYMBOL_NOT_FOUND``) on
    a laptop with KiCad installed as on a bare CI runner.
    """
    from akcli.cli import main as _main  # lazy: after sys.path setup

    out, err = io.StringIO(), io.StringIO()
    saved = os.environ.get("KICAD_CONFIG_HOME")
    os.environ["KICAD_CONFIG_HOME"] = _no_kicad_config()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _main(list(argv))
    finally:
        if saved is None:
            os.environ.pop("KICAD_CONFIG_HOME", None)
        else:
            os.environ["KICAD_CONFIG_HOME"] = saved
    return SimpleNamespace(returncode=rc, stdout=out.getvalue(),
                           stderr=err.getvalue())


def _akcli_json(argv: list[str]) -> dict:
    """Run akcli and parse its ``--json`` document (``ValueError`` if none)."""
    proc = _akcli(argv)
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ValueError(
            f"akcli {argv[0]} emitted no JSON (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:300]}") from None
    if not isinstance(doc, dict):
        raise ValueError(f"akcli {argv[0]} emitted a non-object JSON document")
    return doc


def _load_expected(task_dir: Path) -> dict[str, frozenset[str]]:
    doc = json.loads((task_dir / "expected_nets.json").read_text(encoding="utf-8"))
    return {n["name"]: frozenset(n["members"]) for n in doc["nets"]}


def _is_power_pin(member: str) -> bool:
    """A power-symbol pin (``#PWR03.1`` / ``#FLG01.1``).

    Power symbols are auto-numbered in op order, so their pins can never be
    part of a ground truth; the netlist comparison ignores them (a task that
    REQUIRES a real power symbol says so in ``expected_facts.json``).
    """
    return member.startswith("#")


def prepare_workspace(task_dir: Path, tmp: Path) -> Path:
    """Lay a task's starting state out in ``tmp`` and return the board path.

    Setup sheets are committed so their root uuids — and thus every derived
    uuid — are deterministic; they land next to the board so a
    ``sheets``-routed op-list resolves them. A ``setup/board.kicad_sch`` IS
    the board: the task edits an existing schematic, not a blank one.
    """
    sheet = tmp / BOARD
    setup = task_dir / "setup"
    if not (setup / BOARD).is_file():
        new = _akcli(["new", str(sheet)])
        if new.returncode != 0:
            raise RuntimeError(f"akcli new failed: {new.stderr.strip()}")
    if setup.is_dir():
        for f in sorted(setup.glob("*.kicad_sch")):
            shutil.copy2(f, tmp / f.name)
    return sheet


def apply_argv(sheet: Path, ops_path: Path, facts: dict) -> list[str]:
    """The ``akcli draw --apply`` call the harness scores a candidate behind.

    ``--strict-nets`` is the default rail. Only a ground truth that declares
    an INTENDED named-net split/merge/move (``net_diff.risk``) lifts it — the
    exact post-edit netlist is still enforced by the scorer.
    """
    strict = [] if eval_facts.allows_risk(facts) else ["--strict-nets"]
    return ["draw", str(sheet), "--ops", str(ops_path), "--apply", *strict,
            "--no-erc", *SYMBOL_ARGS]


def score_ops(task_dir: Path, ops_path: Path) -> dict:
    """Score one candidate op-list against a task's ground truth."""
    result: dict = {"task": task_dir.name, "valid": False, "applied": False,
                    "pass": False, "score": 0.0, "matched": 0, "total": 0,
                    "facts_matched": 0, "facts_total": 0, "errors": []}
    expected = _load_expected(task_dir)
    facts = eval_facts.load(task_dir)      # malformed ground truth raises
    result["total"] = len(expected)

    try:
        oplist = json.loads(ops_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["errors"].append(f"unreadable op-list: {exc}")
        return result

    with tempfile.TemporaryDirectory(prefix="akcli-eval-") as td:
        tmp = Path(td)
        try:
            sheet = prepare_workspace(task_dir, tmp)
        except RuntimeError as exc:
            result["errors"].append(str(exc))
            return result

        # The harness owns the target filename; everything else is the agent's.
        if isinstance(oplist, dict):
            oplist["target_file"] = sheet.name
        cand = tmp / "candidate.json"
        cand.write_text(json.dumps(oplist), encoding="utf-8", newline="\n")

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

        # A task that asserts the reported net diff reads it where an agent
        # would: the dry-run plan, BEFORE anything is written.
        plan_diff: dict | None = None
        if eval_facts.wants_plan(facts):
            try:
                diff = _akcli_json(["plan", str(sheet), "--ops", str(cand),
                                    "--json", *SYMBOL_ARGS]).get("net_diff")
            except ValueError as exc:
                result["errors"].append(f"plan failed: {exc}")
                return result
            plan_diff = diff if isinstance(diff, dict) else None

        draw = _akcli(apply_argv(sheet, cand, facts))
        result["applied"] = draw.returncode == 0
        if not result["applied"]:
            result["errors"].append(
                f"draw --apply failed (exit {draw.returncode}): "
                f"{(draw.stderr or draw.stdout).strip()[:500]}")
            return result

        # Optional post-check: tasks that teach the SAFE-RE-PACK discipline
        # ship a postcheck_arrange.json; the drawn sheet must survive an
        # `arrange --groups --apply` (the net-preservation gate refuses a
        # sheet whose cross-block connectivity is not label-on-pin), and the
        # ground-truth nets are compared AFTER the re-pack.
        post = task_dir / "postcheck_arrange.json"
        if post.is_file():
            params = json.loads(post.read_text(encoding="utf-8"))
            argv = ["arrange", str(sheet), "--groups", "--apply", *SYMBOL_ARGS]
            for flag in ("group-gap", "page-width", "row-width", "margin"):
                key = flag.replace("-", "_")
                if key in params:
                    argv += [f"--{flag}", str(params[key])]
            arr = _akcli(argv)
            result["arranged"] = arr.returncode == 0
            if not result["arranged"]:
                result["errors"].append(
                    f"postcheck arrange --groups failed (exit "
                    f"{arr.returncode}): the re-pack must be net-preserving "
                    f"— {(arr.stderr or arr.stdout).strip()[:400]}")
                return result

        nets = _akcli(["nets", str(sheet), "--json"])
        doc = json.loads(nets.stdout)
        named = {n["name"]: frozenset(n["members"])
                 for n in doc["nets"] if n["name"]}
        observed = {name: frozenset(m for m in members if not _is_power_pin(m))
                    for name, members in named.items()}

        try:
            checks = eval_facts.evaluate(facts, sheet=sheet, nets=named,
                                         run_json=_akcli_json,
                                         plan_diff=plan_diff)
        except (ValueError, KeyError, TypeError) as exc:
            # A candidate that left the sheet unreadable fails its facts; it
            # must not crash the scorer.
            checks = [eval_facts.Check("evaluation", False, repr(exc))]

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
                f"got {sorted(got) if got is not None else 'MISSING'}")
    if extras:
        result["errors"].append(f"unexpected named nets: {extras}")

    facts_ok = sum(1 for c in checks if c.ok)
    result["facts_matched"] = facts_ok
    result["facts_total"] = len(checks)
    result["errors"].extend(f"fact {c.name}: {c.detail}"
                            for c in checks if not c.ok)

    goals = len(expected) + len(checks)
    result["score"] = round((matched + facts_ok) / goals, 4) if goals else 1.0
    result["pass"] = (result["applied"] and matched == len(expected)
                      and not extras and facts_ok == len(checks))
    return result


def vocabulary_coverage() -> dict[str, list[str]]:
    """Op/macro name -> the tasks whose reference op-list AUTHORS it.

    Only ops written in a ``reference_ops.json`` count: a core op reached
    solely through a macro's expansion is not exercised as vocabulary (the
    agent never had to spell it).
    """
    cover: dict[str, list[str]] = {}
    for task in _tasks():
        doc = json.loads((task / "reference_ops.json").read_text(encoding="utf-8"))
        for name in sorted({o["op"] for o in doc["ops"]}):
            cover.setdefault(name, []).append(task.name)
    return cover


def coverage_table() -> str:
    """The README's vocabulary-coverage table, generated (Markdown)."""
    from akcli import opspec  # lazy: after sys.path setup

    cover = vocabulary_coverage()
    by_task: dict[str, list[str]] = {t.name: [] for t in _tasks()}
    order = [s.name for s in opspec.ALL]
    for name in order:
        for task in cover.get(name, []):
            by_task[task].append(name)
    lines = ["| Task | Ops and macros its reference authors |", "|---|---|"]
    for task, names in by_task.items():
        lines.append(f"| `{task}` | " + ", ".join(f"`{n}`" for n in names) + " |")
    missing = [n for n in order if n not in cover]
    lines += ["",
              f"Covered: **{len(order) - len(missing)} of {len(order)}** "
              f"vocabulary items"
              + (" — uncovered: " + ", ".join(f"`{n}`" for n in missing)
                 if missing else "") + "."]
    return "\n".join(lines) + "\n"


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
                           "TASK_DIR / OPS_OUT in the environment")
    mode.add_argument("--coverage", action="store_true",
                      help="print which ops/macros each reference op-list "
                           "authors (the README table)")
    ap.add_argument("--json", action="store_true", help="emit a JSON report")
    args = ap.parse_args()

    if args.coverage:
        sys.stdout.write(json.dumps(vocabulary_coverage(), indent=2) + "\n"
                         if args.json else coverage_table())
        return 0

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
                           TASK_DIR=str(task), OPS_OUT=str(ops_path))
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
