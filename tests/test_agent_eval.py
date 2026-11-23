"""Harness-integrity gate for the agent-loop eval (tools/agent_eval/).

No LLM runs in CI; what CI *can* verify is that the harness itself is sound:
every committed reference solution must validate, apply behind the real
safety rails, and score a perfect match against its task's ground truth.
This pins tasks + expected netlists + scorer to real CLI behavior, so a
drifted task (or a CLI change that breaks the eval corpus) fails here first
— and a model run scored with the same harness is trustworthy.

Beyond the reference run this file freezes four things the corpus depends on:

* **vocabulary coverage** — every op and macro in ``akcli.opspec`` is
  authored by at least one reference op-list (or sits in a commented
  allowlist), so adding an op without an eval task fails here;
* **edit-task setups** — each starting schematic is byte-identical to what
  ``build_setups.py`` regenerates from its setup op-list and is clean under
  ``akcli check --integrity``;
* **the scorer discriminates** — a plausible wrong answer per fact kind
  (mutants of the references) must FAIL, for the stated reason;
* **the docs** — the README's coverage table and every stated task count
  match the live corpus.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akcli import opspec
from akcli import ops as akcli_ops

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "tools" / "agent_eval"
TASKS = sorted(d for d in (EVAL_DIR / "tasks").iterdir()
               if (d / "task.md").is_file())

sys.path.insert(0, str(EVAL_DIR))
import build_setups  # noqa: E402
import eval_facts  # noqa: E402
import run_eval  # noqa: E402

EDIT_TASKS = build_setups.setup_tasks()
SETUP_SHEETS = sorted((EVAL_DIR / "tasks").glob("*/setup/*.kicad_sch"))


def _task(prefix: str) -> Path:
    return next(t for t in TASKS if t.name.startswith(prefix))


def _candidate(tmp_path: Path, task: Path, mutate=None) -> Path:
    """The task's reference op-list, optionally mutated, as a candidate file."""
    doc = json.loads((task / "reference_ops.json").read_text(encoding="utf-8"))
    if mutate is not None:
        doc["ops"] = mutate(doc["ops"])
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------- #
# corpus shape
# --------------------------------------------------------------------------- #
def test_task_corpus_present():
    assert len(TASKS) >= 5, "the eval corpus shrank below its floor"
    for task in TASKS:
        assert (task / "expected_nets.json").is_file(), task.name
        assert (task / "reference_ops.json").is_file(), task.name
        eval_facts.load(task)                    # malformed facts raise here


def test_edit_tasks_ship_board_and_its_generator():
    """A starting schematic never exists without the op-list that makes it."""
    assert EDIT_TASKS, "no edit task left in the corpus"
    with_board = [t for t in TASKS if (t / "setup" / run_eval.BOARD).is_file()]
    assert with_board == EDIT_TASKS, (
        "every setup/board.kicad_sch needs a setup/setup_ops.json (and vice "
        "versa) — the board is a generated artifact, see build_setups.py")


def test_briefs_quote_the_live_protocol_version():
    """A brief that tells the agent to author a stale ``protocol_version``
    makes every literal-minded answer fail ``ops validate``."""
    quoted = re.compile(r'"protocol_version":\s*(\d+)')
    for task in TASKS:
        text = (task / "task.md").read_text(encoding="utf-8")
        stale = [v for v in quoted.findall(text)
                 if int(v) != akcli_ops.PROTOCOL_VERSION]
        assert not stale, (
            f"{task.name}/task.md quotes protocol_version {stale}; the live "
            f"op-list protocol is {akcli_ops.PROTOCOL_VERSION}")


# --------------------------------------------------------------------------- #
# reference solutions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_solution_scores_perfect(task: Path):
    result = run_eval.score_ops(task, task / "reference_ops.json")
    assert result["valid"], result["errors"]
    assert result["applied"], result["errors"]
    assert result["pass"], result["errors"]
    assert result["score"] == 1.0
    assert result["facts_matched"] == result["facts_total"]


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_reference_plans_clean_and_reapplies_byte_identically(
        task: Path, tmp_path: Path):
    """The invariant every op-list owes: a clean dry-run, then a second apply
    that changes no byte of any sheet it touched."""
    sheet = run_eval.prepare_workspace(task, tmp_path)
    cand = _candidate(tmp_path, task)
    facts = eval_facts.load(task)

    plan = run_eval._akcli(["plan", str(sheet), "--ops", str(cand),
                            *run_eval.SYMBOL_ARGS])
    assert plan.returncode == 0, plan.stdout + plan.stderr

    argv = run_eval.apply_argv(sheet, cand, facts)
    first = run_eval._akcli(argv)
    assert first.returncode == 0, first.stdout + first.stderr
    once = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.kicad_sch"))}

    second = run_eval._akcli(argv)
    assert second.returncode == 0, second.stdout + second.stderr
    twice = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.kicad_sch"))}
    assert once == twice, f"{task.name}: re-apply is not byte-identical"


# --------------------------------------------------------------------------- #
# edit-task setups: generated by akcli, reproducible, integrity-clean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("task", EDIT_TASKS, ids=lambda t: t.name)
def test_setup_board_is_what_its_op_list_generates(task: Path):
    committed = (task / "setup" / run_eval.BOARD).read_bytes()
    assert build_setups.build(task) == committed, (
        f"{task.name}/setup/board.kicad_sch drifted from its setup_ops.json — "
        "run `python tools/agent_eval/build_setups.py` and review the diff")


@pytest.mark.parametrize("sheet", SETUP_SHEETS,
                         ids=lambda p: f"{p.parts[-3]}/{p.name}")
def test_setup_sheet_passes_integrity(sheet: Path):
    report = run_eval._akcli_json(["check", str(sheet), "--integrity",
                                   "--json", "--fail-on", "never"])
    assert report["findings"] == [], report["findings"]
    assert run_eval._akcli(["check", str(sheet), "--integrity"]).returncode == 0


@pytest.mark.parametrize("task", EDIT_TASKS, ids=lambda t: t.name)
def test_edit_task_is_not_already_solved(task: Path, tmp_path: Path):
    """The starting board must MISS its ground truth — an edit task an empty
    op-list already satisfies measures nothing."""
    noop = tmp_path / "noop.json"
    noop.write_text(json.dumps({
        "protocol_version": akcli_ops.PROTOCOL_VERSION,
        "target_format": "kicad", "target_file": run_eval.BOARD, "ops": []}),
        encoding="utf-8", newline="\n")
    result = run_eval.score_ops(task, noop)
    assert result["valid"] and result["applied"], result["errors"]
    assert not result["pass"], f"{task.name}: the untouched setup passes"


# --------------------------------------------------------------------------- #
# vocabulary coverage (fail-closed: a new op needs an eval task)
# --------------------------------------------------------------------------- #
# Ops/macros deliberately WITHOUT an eval task, each with the reason. Keep it
# small and shrink-only: an entry is a documented hole in the agent-loop
# signal, never a way to land an op without a task. Empty at the time of
# writing — all 32 vocabulary items are authored by some reference op-list
# (`add_junction` included: task 20 joins two CROSSING wires, the one case the
# writer's auto-junction never covers).
_UNCOVERED_ALLOWLIST: dict[str, str] = {}


def test_reference_oplists_cover_the_op_vocabulary():
    registry = {s.name for s in opspec.ALL}
    cover = run_eval.vocabulary_coverage()

    unknown = sorted(set(cover) - registry)
    assert not unknown, f"reference op-lists use ops opspec lacks: {unknown}"

    missing = sorted(registry - set(cover) - set(_UNCOVERED_ALLOWLIST))
    assert not missing, (
        f"no eval task exercises {missing}: add a task under "
        "tools/agent_eval/tasks/ whose reference_ops.json authors it (or, as "
        "a documented last resort, an _UNCOVERED_ALLOWLIST reason)")

    stale = sorted(n for n in _UNCOVERED_ALLOWLIST
                   if n in cover or n not in registry)
    assert not stale, (
        f"_UNCOVERED_ALLOWLIST entries that are covered or gone: {stale} "
        "— shrink the list")


def test_coverage_counts_authored_ops_only():
    """A core op reached only through a macro expansion is not 'exercised':
    task 17's four block macros expand to place_component + add_net_label,
    yet neither is credited to it."""
    cover = run_eval.vocabulary_coverage()
    macros_only = _task("17-").name
    assert macros_only in cover["place_divider"]
    assert macros_only not in cover["place_component"]
    assert macros_only not in cover["add_net_label"]


# --------------------------------------------------------------------------- #
# docs: coverage table + every stated task count
# --------------------------------------------------------------------------- #
_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}


def _count(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS[token]
    tens, _, unit = token.partition("-")
    if tens in _TENS and (not unit or unit in _NUMBER_WORDS):
        return _TENS[tens] + (_NUMBER_WORDS[unit] if unit else 0)
    return None


def test_readme_coverage_table_is_generated_from_the_references():
    readme = (EVAL_DIR / "README.md").read_text(encoding="utf-8")
    assert run_eval.coverage_table() in readme, (
        "tools/agent_eval/README.md's coverage table is stale — paste the "
        "output of `python tools/agent_eval/run_eval.py --coverage`")


@pytest.mark.parametrize("doc, pattern, required", [
    (EVAL_DIR / "README.md", r"\*\*([\w-]+) tasks\*\*", True),
    (ROOT / "ROADMAP.md", r"\b([\w-]+) ground-truthed design tasks", False),
], ids=["agent_eval/README.md", "ROADMAP.md"])
def test_stated_task_counts_match_the_corpus(doc: Path, pattern: str,
                                             required: bool):
    claims = re.findall(pattern, doc.read_text(encoding="utf-8"))
    if required:
        assert claims, f"{doc.name} no longer states the eval task count"
    stated = [(t, _count(t)) for t in claims if _count(t) is not None]
    if required:
        assert stated, f"{doc.name}: no numeric task count among {claims}"
    for token, value in stated:
        assert value == len(TASKS), (
            f"{doc.name} says {token!r} eval tasks; the corpus holds "
            f"{len(TASKS)}")


# --------------------------------------------------------------------------- #
# the scorer discriminates: one plausible wrong answer per rail / fact kind
# --------------------------------------------------------------------------- #
def _drop(*names):
    return lambda ops: [o for o in ops if o["op"] not in names]


def _strip(*keys):
    return lambda ops: [{k: v for k, v in o.items() if k not in keys}
                        for o in ops]


def _edit(name, **changes):
    return lambda ops: [{**o, **changes} if o["op"] == name else o for o in ops]


def _swap(name, replacement):
    def mutate(ops):
        out: list = []
        for o in ops:
            out.extend(replacement if o["op"] == name else [o])
        return out
    return mutate


_SPARE_UNIT_BY_HAND = [
    {"op": "place_component", "lib_id": "EvalParts:OPAMP_DUAL",
     "designator": "U1", "unit": 2, "x_mil": 4800, "y_mil": 2000},
    {"op": "add_net_label", "at": "U1.5", "name": "GND"},
    {"op": "add_net_label", "at": "U1.6", "name": "VCC"},
]

# (task prefix, what the wrong answer does, mutation, must it apply?,
#  text the failure report must carry)
_MUTANTS = [
    ("11-", "move without carry_wires strands the wire",
     _strip("carry_wires"), False, "DANGLING_ENDPOINT"),
    ("11-", "move without carry_labels splits the named nets",
     _strip("carry_labels"), False, "--strict-nets"),
    ("12-", "delete without cascade leaves the wire dangling",
     _strip("cascade"), False, "DANGLING_ENDPOINT"),
    ("12-", "cascade alone leaves the mid-wire label behind",
     _drop("delete_object"), True, "NET_LABEL_UNATTACHED"),
    ("13-", "re-valued but never renamed",
     _drop("rename_net"), True, "fact net_diff"),
    ("13-", "only one resistor re-valued",
     lambda ops: ops[:2], True, "fact component R2"),
    ("13-", "footprint forgotten",
     _strip("footprint"), True, "fact component R1: footprint"),
    ("14-", "diode turned, regulator still mirrored",
     lambda ops: ops[:1], True, "fact component U1: mirror"),
    ("15-", "re-labelled without deleting the wrong label (shorts SDA+SCL)",
     _drop("delete_object"), True, "net 'SDA'"),
    ("15-", "rename_net renames the WHOLE net",
     lambda ops: [{"op": "rename_net", "old_name": "SDA", "new_name": "SCL"}],
     True, "fact net_diff"),
    ("16-", "note box missing",
     _drop("add_text_box"), True, "fact text_box"),
    ("16-", "free text missing",
     _drop("add_text"), True, "fact text 'DIVIDER'"),
    ("16-", "frame also swallows C1",
     _edit("add_rectangle", end=[4200, 3000]), True, "fact rectangle[0]"),
    ("16-", "wrong revision",
     _edit("set_title_block", rev="A"), True, "fact title_block.rev"),
    ("19-", "spare unit tied off but output not flagged no-connect",
     _swap("terminate_unused_unit", _SPARE_UNIT_BY_HAND),
     True, "fact no_connect U1.7"),
    ("20-", "a label where the +3V3 power symbol belongs",
     _swap("place_power_port",
           [{"op": "add_net_label", "at": [3300, 1600], "name": "+3V3"}]),
     True, "fact power_port on +3V3"),
    ("20-", "no PWR_FLAG on the connector-fed net",
     _drop("place_pwr_flag"), True, "fact pwr_flag on VIN"),
    ("20-", "crossing wires without the junction do not connect",
     _drop("add_junction"), True, "net '+3V3'"),
    ("20-", "unused header pins left floating",
     _drop("add_no_connect"), True, "fact no_connect J1.2"),
]


@pytest.mark.parametrize("prefix, why, mutate, applies, needle", _MUTANTS,
                         ids=[f"{m[0]}{m[1]}" for m in _MUTANTS])
def test_wrong_answers_fail_for_the_stated_reason(
        prefix, why, mutate, applies, needle, tmp_path: Path):
    task = _task(prefix)
    result = run_eval.score_ops(task, _candidate(tmp_path, task, mutate))
    report = "\n".join(result["errors"])
    assert result["valid"], report
    assert result["applied"] is applies, report
    assert not result["pass"], f"{why}: scored as a pass"
    assert result["score"] < 1.0
    assert needle in report, report


def test_labels_alone_do_not_satisfy_a_power_port_fact(tmp_path: Path):
    """Task 19 with every power symbol swapped for a same-name label keeps the
    exact netlist — only the ``power_ports`` facts can tell."""
    def mutate(ops):
        ops = _swap("terminate_unused_unit", [
            *_SPARE_UNIT_BY_HAND, {"op": "add_no_connect", "pin": "U1.7"}])(ops)
        ops = _swap("place_vcc", [
            {"op": "add_net_label", "at": "U1.8", "name": "VCC"}])(ops)
        return _swap("place_gnd", [
            {"op": "add_net_label", "at": "U1.4", "name": "GND"}])(ops)

    task = _task("19-")
    result = run_eval.score_ops(task, _candidate(tmp_path, task, mutate))
    assert result["applied"] and result["matched"] == result["total"]
    assert not result["pass"]
    report = "\n".join(result["errors"])
    assert "fact power_port on VCC" in report
    assert "fact power_port on GND" in report


# --------------------------------------------------------------------------- #
# harness mechanics
# --------------------------------------------------------------------------- #
def test_power_symbol_pins_never_enter_the_net_comparison():
    assert run_eval._is_power_pin("#PWR03.1")
    assert run_eval._is_power_pin("#FLG01.1")
    assert not run_eval._is_power_pin("R1.1")


def test_only_a_declared_risk_lifts_strict_nets(tmp_path: Path):
    sheet, ops = tmp_path / "b.kicad_sch", tmp_path / "o.json"
    assert "--strict-nets" in run_eval.apply_argv(sheet, ops, {})
    assert "--strict-nets" in run_eval.apply_argv(
        sheet, ops, {"net_diff": {"equivalent": True}})
    assert "--strict-nets" in run_eval.apply_argv(
        sheet, ops, {"net_diff": {"risk": False}})
    assert "--strict-nets" not in run_eval.apply_argv(
        sheet, ops, {"net_diff": {"risk": True}})
    risky = [t.name for t in TASKS if eval_facts.allows_risk(eval_facts.load(t))]
    assert risky == [_task("14-").name, _task("15-").name], (
        "only the two repair tasks whose POINT is a named-net change may be "
        f"scored without --strict-nets, got {risky}")


@pytest.mark.parametrize("bad, complaint", [
    ({"componets": {}}, "unknown field"),
    ({"components": {"R1": {"valu": "1k"}}}, "unknown field"),
    ({"components": {"R1": {}}}, "no field to check"),
    ({"absent": "R1"}, "list of non-empty strings"),
    ({"rectangles": [{"excludes": ["C1"]}]}, "rectangles[0].encloses"),
    ({"title_block": {"revision": "B"}}, "unknown field"),
    ({"net_diff": {}}, "no field to check"),
    ({"net_diff": {"line": []}}, "unknown field"),
    ([], "must be a JSON object"),
])
def test_malformed_facts_fail_closed(bad, complaint):
    """A typo'd fact must never silently pass as 'nothing to check'."""
    with pytest.raises(eval_facts.FactsError) as exc:
        eval_facts.validate(bad)
    assert complaint in str(exc.value)


def test_scoring_ignores_the_machine_kicad_config(tmp_path: Path, monkeypatch):
    """A candidate must resolve the same symbols on a laptop with KiCad
    installed as on a bare CI runner: the global sym-lib-table is never
    consulted, and the caller's environment comes back untouched."""
    lib = tmp_path / "Elsewhere.kicad_sym"
    lib.write_bytes((EVAL_DIR / "symbols" / "EvalParts.kicad_sym").read_bytes()
                    .replace(b'"OPAMP_DUAL', b'"ONLY_ELSEWHERE'))
    cfg = tmp_path / "kicad_cfg"
    cfg.mkdir()
    (cfg / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n'
        f'  (lib (name "Elsewhere")(type "KiCad")(uri "{lib.as_posix()}")'
        '(options "")(descr ""))\n)\n', encoding="utf-8", newline="\n")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(cfg))

    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({
        "protocol_version": akcli_ops.PROTOCOL_VERSION, "target_format": "kicad",
        "ops": [{"op": "place_component", "lib_id": "Elsewhere:ONLY_ELSEWHERE",
                 "designator": "U1", "x_mil": 2000, "y_mil": 2000}]}),
        encoding="utf-8", newline="\n")

    # the premise: outside the harness, that table DOES serve the symbol
    from akcli.cli import main as akcli_main
    outside = tmp_path / "outside.kicad_sch"
    assert akcli_main(["new", str(outside), "-q"]) == 0
    assert akcli_main(["draw", str(outside), "--ops", str(ops), "--apply",
                       "--no-erc", "-q"]) == 0

    result = run_eval.score_ops(_task("01-"), ops)
    assert result["valid"] and not result["applied"]
    assert "SYMBOL_NOT_FOUND" in "\n".join(result["errors"])
    assert os.environ["KICAD_CONFIG_HOME"] == str(cfg)


def test_agent_command_gets_the_task_directory(monkeypatch, capsys):
    """An edit task's starting schematic lives next to the prompt; the agent
    command finds it through TASK_DIR."""
    task = _task("13-")
    seen: dict = {}

    def fake_run(cmd, *, shell, env, capture_output, text):
        seen.update(cmd=cmd, shell=shell, env=env)
        Path(env["OPS_OUT"]).write_bytes(
            (Path(env["TASK_DIR"]) / "reference_ops.json").read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_eval, "_tasks", lambda: [task])
    monkeypatch.setattr(run_eval.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--agent-cmd", "agent"])
    assert run_eval.main() == 0
    assert "1/1 tasks passed" in capsys.readouterr().out
    assert seen["cmd"] == "agent" and seen["shell"] is True
    assert Path(seen["env"]["TASK_DIR"]) == task
    assert Path(seen["env"]["TASK_FILE"]) == task / "task.md"
    assert (Path(seen["env"]["TASK_DIR"]) / "setup" / run_eval.BOARD).is_file()


def test_coverage_mode_prints_the_readme_table(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--coverage"])
    assert run_eval.main() == 0
    assert capsys.readouterr().out == run_eval.coverage_table()
