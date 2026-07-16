# Agent-loop eval harness

The test suite proves the *tool* is correct; this harness measures the other
half of the AI-native claim: **an LLM agent reading the skills/docs must
produce an op-list that akcli validates, applies behind the safety rails, and
that lands the intended netlist.** Nothing else in CI exercises that loop —
a skill rewrite that subtly makes agents worse at authoring would otherwise
ship silently.

## Layout

```text
tools/agent_eval/
├── run_eval.py            # scorer + runners (dev-only, not wheel-packaged)
└── tasks/<nn>-<slug>/
    ├── task.md            # the natural-language prompt the agent receives
    ├── expected_nets.json # ground truth: named nets -> exact pin members
    └── reference_ops.json # known-good solution (harness self-check)
```

## Scoring

Deterministic and offline. For a candidate op-list, per task:

1. `akcli ops validate` — structural validity (fail -> score 0).
2. `akcli new` + `akcli draw --apply --strict-nets` on a fresh sheet —
   the same safety rails a real session runs behind (fail -> score 0).
3. `akcli nets --json` — the named nets must match `expected_nets.json`
   exactly (pin membership); spurious extra named nets fail the task;
   unnamed nets are ignored. `score` = matched / expected.

`tests/test_agent_eval.py` scores every committed `reference_ops.json` and
asserts 100%, so the tasks and ground truths can never drift from real CLI
behavior — the harness itself is CI-verified even though no LLM runs in CI.

## Running against a real agent

```bash
# harness self-check (no LLM):
python tools/agent_eval/run_eval.py --reference

# any agent, via a shell command run once per task; it gets TASK_FILE and
# must write the op-list JSON to OPS_OUT:
python tools/agent_eval/run_eval.py --agent-cmd \
  'claude -p "$(cat "$TASK_FILE")" --output-format text > "$OPS_OUT"'

# or score op-lists you generated some other way (one <task-name>.json each):
python tools/agent_eval/run_eval.py --ops-dir /path/to/answers --json
```

Run the eval before merging a change to `skills/` or `commands/` prose, and
periodically against the models you actually use; track the pass rate over
time. A model-authored run should be compared against `--reference` (always
100%) to separate harness breakage from agent regression.

## Known limitations (v1)

- Tasks prescribe designators, pin assignments, and net names, so scoring is
  exact-match on membership; an electrically-equivalent-but-relabeled answer
  scores as a miss. This deliberately tests instruction-following + tool use,
  not topology isomorphism.
- Five small analog tasks; grow the corpus (hierarchy, buses, multi-unit
  parts) as skills cover more ground.
- The harness scores the *artifact* (op-list), not the conversation — an
  agent that needed ten retries scores the same as one that nailed it. Wrap
  `--agent-cmd` with your own attempt/latency accounting if you need that.
