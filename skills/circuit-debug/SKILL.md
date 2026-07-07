---
name: circuit-debug
description: >-
  Systematically diagnose misbehaving schematics and `akcli` tool failures — no
  Altium or KiCad install required. Use this skill whenever the task involves:
  debugging net connectivity (a net that is split, merged wrongly, missing, or a
  pin that floats when it should not); triaging `akcli check` findings (real
  fault vs false positive, deciding whether a clean pass is trustworthy);
  investigating why `akcli plan`/`akcli draw` refused to write or what an exit
  code means; recovering from a failed apply (backup/rollback); or classifying
  parse failures and unsupported-input refusals before reporting a bug upstream.
  Triggers on keywords: debug, diagnose, troubleshoot, net split, floating pin,
  wrong net merge, false positive, ERC finding, exit code, parse error, refused,
  dangling endpoint, connectivity gate, rollback, backup, corrupt SchDoc,
  unsupported format.
---

# circuit-debug — systematic diagnosis with `akcli`

Use this skill to find out **why** a schematic or `akcli` itself misbehaves. For basic
read/analyze/draw mechanics (input formats, op-list authoring, config, the full workflow) see the
**circuit-design** skill — this skill only covers diagnosis. Run every command with the `Bash`
tool; from a raw checkout use `PYTHONPATH=src python3 -m altium_kicad_cli ...` or `bin/akcli ...`.

Two ground rules:
- **stdout is data, stderr is diagnosis.** `akcli ... --json | jq` stays clean; the not-found
  notices and `ERROR: CODE: ...` lines live on stderr. Capture both streams when debugging.
- **Reproduce from raw data before theorizing.** Dump the net, dump the component, compare against
  the expectation — never argue from a rendered summary alone.

## Symptom → first command

| Symptom | First command |
|---|---|
| Net looks split / pin floating / wrong merge | `akcli net <file> <NETNAME>` |
| One component wired wrongly | `akcli component <file> <REF>` |
| "Is this check finding real?" | `akcli check <file> --exit-zero --json` |
| Two revisions disagree | `akcli diff <a> <b> --exit-zero` |
| MCU pin assignment suspect | `akcli pinmap <file> --mcu U3` |
| `draw`/`plan` refused to write | `akcli draw <target> --ops <ops.json>` (dry-run, read connectivity block) |
| File will not parse at all | `akcli read <file> --debug` |
| Unclear tool state | `akcli --version` (prints package **and** protocol version) |

## 1. Net-connectivity debugging

Symptom classes: a net that should be one is **split** in two; a pin that should be attached is
**floating**; two unrelated nets got **merged**. Work the same loop every time:

```bash
# (a) Dump the suspect net: members, aliases, source_names, confidence, merge_reasons
akcli net board.kicad_sch GND
akcli net board.kicad_sch GND --json

# (b) List ALL nets and look for duplicates / near-names (GND vs GND_2, N$-auto names)
akcli net board.kicad_sch
akcli net board.kicad_sch --json | jq 'length'

# (c) Check the component side: which net does each pin of U3 actually land on?
akcli component board.kicad_sch U3
akcli component board.kicad_sch U3 --json
```

Then compare against the expectation (datasheet, prior revision via `akcli diff`, or an expected
table via `akcli pinmap --expected`). Interpretation guide:

- **Trap: not-found is NOT an error exit.** `akcli net <file> NAME` and
  `akcli component <file> REF` exit **0** even when nothing matches — they print
  `no net named '...'` / `no component '...'` to **stderr**. Always check stderr, never the exit
  code, when probing for existence.
- **Net identity is membership, not name.** Each net's `stable_id` is a hash of its sorted
  `(designator, pin)` members. Two nets with the same display name but different `stable_id`s are
  genuinely disconnected pieces — that is the split.
- **The same-name-net-split bug class.** Historically the Altium net merge had a real bug: two
  wire islands both labeled `GND` stayed as two nets, and a `STAT`↔`LED1_GPIO_RD` alias was
  dropped. Spot this class by: (1) the same name appearing twice in the `akcli net` listing,
  (2) a net whose `members` list is suspiciously short for its name, (3) missing entries in
  `aliases`/`source_names` where you expected a label merge. `akcli check` also surfaces
  multi-name merges as `ERC_NET_ALIAS` NOTEs — those are deliberate merges, verify each is wanted.
- **Wrong merge:** a net with multiple `source_names`, `confidence` 0.8, and populated
  `merge_reasons` was merged by the same-name/label engine. Read `merge_reasons` to see why; if
  two labels merged nets that must stay apart, the fix is in the schematic labels, not the tool.
- **Floating pin:** if the pin is absent from every net's members, cross-check its coordinates —
  `akcli read <file> --json` gives pin electrical-tip positions (mils, top-left origin, +Y down).
  A pin tip that misses the wire end by a few mils is a draw error in the source schematic.
- Cross-sheet note: geometry never connects across sheets; power ports and global labels merge
  by name. A parent joins exactly its child's same-named counterpart — KiCad: sheet pin ↔
  hierarchical label; Altium: sheet entry ↔ port (ports merge globally only in designs WITHOUT
  sheet symbols). A "split" between sheets with only local labels is expected.

## 2. Triaging `akcli check` findings

```bash
akcli check board.kicad_sch --exit-zero            # report mode: findings without exit 1
akcli check board.kicad_sch --erc --exit-zero      # isolate one check family (--erc/--power/--bom)
akcli check board.kicad_sch -C altium-kicad-cli.toml --json
```

**Read the metadata header first** — it is printed on every run and decides how much the findings
(and their absence) are worth:

- `passive_pin_ratio` high (few pins carry a real electrical type): the type-gated rules
  (driver conflict, floating input) are demoted to NOTE with an explanatory suffix. A "clean" ERC
  on such a board is **vacuous** for those rules — say so explicitly.
- `no_erc_suppressed` > 0: that many findings were silenced by No-ERC markers. Confirm the
  markers are intentional before trusting the pass.
- `unnamed_net_count` > 0: the power check ignores unnamed nets entirely, so an IC fed by an
  unnamed rail is never checked for power/ground. A clean power pass with many unnamed nets is
  weak evidence.
- Fractional coords present (Altium sources only): sub-unit coordinates existed; geometry
  reasoning is still valid but be alert on near-miss pin/wire questions.

Real fault vs false positive, per rule:

- `ERC_FLOATING_INPUT` / `ERC_DRIVER_CONFLICT`: trust only when pin types are real (see the
  passive-pin gate above). Verify by `akcli component <file> <REF>` on the flagged parts.
- `ERC_NO_POWER` / `ERC_NO_GROUND`: **name-based**, and only components with `U`/`IC` designator
  prefixes are checked. False positive when the rail exists under an unrecognized name — add a
  `[[rail]]` entry in config. Note the waiver quirk: `[[erc_waiver]]` for these two rules matches
  the waiver `net` field against the component **designator**, not a net name.
- `ERC_DANGLING_NET`: single-pin net. Real for forgotten wires; false positive for intentional
  test points — waive via `[[erc_waiver]]` (rule tokens are the lowercase forms, e.g.
  `dangling_net`) rather than ignoring the whole check.
- `ERC_NET_ALIAS`: NOTE-only by design (the merge was deliberate). Escalate only if the merged
  names should not be one net (see §1 wrong-merge).
- BOM findings: duplicate designators are ERRORs and almost always real; refdes gaps are
  NOTE-level hygiene.

Exit semantics: `check`/`diff`/`pinmap` exit **1** when any finding is WARNING or worse; use
`--exit-zero` when you want the report without tripping scripted error handling.

## 3. Debugging `plan` / `draw` failures

Escalate in this order — never jump straight to `--apply`:

```bash
akcli plan board.kicad_sch --ops ops.json                  # validate + resolve; never writes
akcli draw board.kicad_sch --ops ops.json                  # dry-run (the default): per-op results + connectivity
akcli draw board.kicad_sch --ops ops.json --apply          # write, only after a clean dry-run
akcli draw board.kicad_sch --ops ops.json --symbols extra.kicad_sym   # if SYMBOL_NOT_FOUND
```

- **Reading the connectivity-gate refusal:** the dry-run/apply output ends with a
  `# connectivity (N)` block. `DANGLING_ENDPOINT`, `DUPLICATE_UUID`, `UNRESOLVED_LIB_ID`,
  `INVALID_INSTANCES_PATH` are ERRORs — any one of them (or any errored op) makes `draw` exit
  **6** and refuse the write; `NO_CONNECT_CONFLICT` is a warning. Fix the op-list (usually a wire
  endpoint that misses a pin — prefer `"REF.PIN"` endpoints, which snap exactly) and re-run the
  dry-run until the connectivity block is clean.
- Per-op failures name the culprit: `SYMBOL_NOT_FOUND` → add `--symbols`; `VERIFY_FAILED` on
  `set_component_transform`/`set_component_parameters` → the designator does not exist in the
  target (check with `akcli component`); `PROTOCOL_MISMATCH` / `OP_UNSUPPORTED` → stop and
  report, do not retry blindly.
- `--dry-run` is accepted but **inert** — dry-run is already the default; only `--apply` changes
  behavior. Do not treat `--dry-run` as extra safety.
- **Backup/rollback:** a successful `--apply` writes `<name>.kicad_sch.bak` next to the target
  before replacing it. To roll back, copy the `.bak` over the target. If the target changed on
  disk between akcli's read and write, apply aborts with `VERIFY_FAILED` and touches nothing —
  re-run from a fresh dry-run.
- **Re-read-after-apply discipline:** an exit-0 apply proves the write landed, not that intent
  was met. Immediately `akcli net board.kicad_sch <NET>` / `akcli component board.kicad_sch <REF>`
  the touched objects and compare against the op-list. Replaying the same op-list is idempotent
  (deterministic UUIDs), so re-running `--apply` after a partial investigation is safe.

## 4. Tool-level triage

Exit-code table (frozen in `errors.py`):

| Code | Meaning | Typical action |
|---|---|---|
| 0 | success / no findings | proceed (but see vacuous-pass caveats, §2) |
| 1 | check/diff/pinmap findings ≥ WARNING | triage findings; `--exit-zero` for report mode |
| 2 | usage / config error (`BAD_CONFIG`, bad flags, `export --json`) | fix the invocation or TOML |
| 3 | parse error (corrupt OLE2 / S-expr, alloc guards) | suspect the file; report upstream |
| 4 | file not found | fix the path |
| 5 | unsupported format (`ALTIUM_UNSUPPORTED`, wrong input kind) | expected refusal — see below |
| 6 | op-list / verify failure | fix ops or connectivity (§3) |
| 7 | external tool missing / network | install hint on stderr; only `jlc` needs network |

Known deliberate refusals (exit 5 — **unsupported, not corrupt**; do not file these as parse bugs):
binary `.SchLib` symbol records; binary `.PcbDoc` fills/regions/texts/polygons (pads/vias/
tracks/arcs ARE decoded since post-v0.3.1 — an exit-5 there means an unknown record type);
and feeding `.SchLib`/`.PcbDoc`/`.kicad_pcb` to schematic-only commands (`net`, `component`,
`check`, `diff`, `pinmap`, `export`) — the stderr notice says to use `akcli read` instead, which
does accept them. Exit 3 with `ALTIUM_ALLOC_GUARD` on a large file may be the DIFAT-spillover
limit rather than corruption.

What to report upstream when akcli itself seems wrong: the one-line `ERROR: CODE: message` from
stderr, the full traceback from re-running with `--debug`, the `akcli --version` output (package +
protocol version), the exact command line, and the smallest input file that reproduces. Parse
failures (exit 3) on files that open fine in Altium/KiCad are always report-worthy.

## Companion material

Basic workflows, op-list schema, config keys: **circuit-design** skill. Read-only review flows:
`/circuit-review`, `/circuit-diff`, `/circuit-pinmap`. Writes: `/circuit-draw` (user-triggered).
