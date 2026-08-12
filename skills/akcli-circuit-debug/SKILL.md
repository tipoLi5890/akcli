---
name: akcli-circuit-debug
description: >-
  Systematically diagnose misbehaving schematics and `akcli` tool failures.
  Use this skill whenever the task involves:
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

# akcli-circuit-debug — systematic diagnosis with `akcli`

Use this skill to find out **why** a schematic or `akcli` itself misbehaves. For basic
read/analyze/draw mechanics (input formats, op-list authoring, config, the full workflow) see the
**akcli-circuit-design** skill — this skill only covers diagnosis. Run every command with the `Bash`
tool; from a raw checkout use `PYTHONPATH=src python3 -m akcli ...` or `bin/akcli ...`.

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
| "Is this check finding real?" | `akcli check <file> --fail-on never --json` |
| Two revisions disagree | `akcli diff <a> <b> --fail-on never` |
| MCU pin assignment suspect | `akcli pinmap <file> --mcu U3` |
| `draw`/`plan` refused to write | `akcli draw <target> --ops <ops.json>` (dry-run, read connectivity block) |
| Refusal may predate the op-list (bad target file) | `akcli check <target> --integrity --fail-on never` |
| File will not parse at all | `akcli read <file> --debug` |
| Unclear tool state | `akcli --version` (prints package **and** protocol version) |
| Write refused by `TARGET_LOCKED` (KiCad GUI has the file open) | close the GUI, or `akcli draw <t> --ops <ops.json> --apply --allow-open` (then File>Revert in KiCad) |
| KiCad says "footprint not found" | `akcli library audit <project>` |

## 1. Net-connectivity debugging

Symptom classes: a net that should be one is **split** in two; a pin that should be attached is
**floating**; two unrelated nets got **merged**. One silent cause to keep on the suspect list:
a symbol **rotated or mirrored in the KiCad GUI** slides its pins under the other pin's
stationary labels — the netlist swaps with nothing visually wrong (see the rotate/mirror
label-swap entry in [docs/kicad-format-gotchas.md](../../docs/kicad-format-gotchas.md);
`akcli verify` and `check --intent` catch it after the fact). Work the same loop every time:

```bash
# (a) Dump the suspect net: members, aliases, source_names, confidence, merge_reasons
akcli net board.kicad_sch GND
akcli net board.kicad_sch GND --json

# (b) List ALL nets and look for duplicates / near-names (GND vs GND_2, N$-auto names)
akcli net board.kicad_sch
akcli net board.kicad_sch --json | jq '.total'   # same envelope + net shape as `akcli nets`
akcli net board.kicad_sch --why R1.2             # pin -> net, attachment kind, nearest name sources
# An unexpected net NAME (your label shows up as an alias) is usually not a
# wiring bug: canonical naming follows eeschema's driver priority — see the
# akcli-net-naming skill before rewiring anything.

# (c) Check the component side: which net does each pin of U3 actually land on?
akcli component board.kicad_sch U3
akcli component board.kicad_sch U3 --json

# (d) Pin-level causality — ask the engine directly instead of reverse-reading findings
akcli net board.kicad_sch --at U3.7            # which net is this pin on (null = floating)
akcli net board.kicad_sch --same U3.7 R1.2     # are these two pins one net?
akcli net board.kicad_sch --why U3.7           # WHY is it (not) connected: junction/wire-end
                                               # status, nearest wire end/label/pin with mil
                                               # distances, off-grid flag, fix hints
```

`--why` is the shortcut for the floating-pin symptom: it reports mid-span touches
(pin on a wire body with no junction), a wire end or label that stops N mil short,
and off-grid pin tips — the three causes that otherwise require reading
`check --nets` findings backwards.

Then compare against the expectation (datasheet, prior revision via `akcli diff`, or an expected
table via `akcli pinmap --expected`). Interpretation guide:

- **Not-found is exit 8 (`QUERY_MISS`), not a tool failure.** `akcli net <file> NAME` and
  `akcli component <file> REF` exit **8** when nothing matches — the notice
  (`no net named '...'` / `no component '...'`) goes to **stderr**, and with `--json` stdout
  still carries the structured `error` object. Branch on exit 8 / `error.code == "QUERY_MISS"`
  when probing for existence: an 8 means "file parsed fine, that name is absent".
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
akcli check board.kicad_sch --fail-on never        # report mode: findings without exit 1
akcli check board.kicad_sch --erc --fail-on never  # isolate one family (--erc/--power/--bom/--nets/
                                                  # --layout/--integrity/--pairs)
akcli check board.kicad_sch --integrity --fail-on never   # the write gate's document checks,
                                                  # read-only on ANY .kicad_sch
akcli check board.kicad_sch -C akcli.toml --json
```

`--integrity` is the diagnosis shortcut for "the file itself is wrong": it runs
the same checks `draw --apply` gates on (`DANGLING_ENDPOINT`,
`DANGLING_BUS_ENTRY`, `DUPLICATE_UUID`, `DUPLICATE_DESIGNATOR`,
`UNRESOLVED_LIB_ID`, `INVALID_INSTANCES_PATH`, `POWER_PORT_STACKED`,
`SHEET_FILE_MISSING`; plus `NO_CONNECT_CONFLICT`, reported by the same pass
at WARNING — visible here, never a write refusal) over a file
akcli did not write — the fastest way to tell a hand edit / bad merge / moved
library from an electrical problem, and to confirm a target is clean BEFORE
blaming an op-list for a refused write.

**Read the metadata header first** — it is printed on every run and decides how much the
findings (and their absence) are worth. The full caveat table (passive-pin ratio, No-ERC
suppression, unnamed-net count, fractional coords — and what each does to finding
reliability) lives in the **akcli-schematic-review** skill; do not re-derive it here. The
debugging-specific consequence: a "clean" run under any of those caveats is **vacuous
evidence** for the demoted rules — say so explicitly instead of reporting a pass.

Real fault vs false positive, per rule:

- `ERC_FLOATING_INPUT` / `ERC_DRIVER_CONFLICT`: trust only when pin types are real (see the
  passive-pin gate above). Verify by `akcli component <file> <REF>` on the flagged parts.
  A net held by a **passive tie to a rail** (the 10k from `EN` to `+3V3`, a reset pull-up)
  or reaching a **connector** (an off-board probe drives it) counts as driven and is not
  reported — a DNP pull-up ties nothing, so that case still fires.
- `ERC_NO_POWER` / `ERC_NO_GROUND`: **name-based**, and only components with `U`/`IC` designator
  prefixes are checked. TVS/ESD arrays (`TPD4E05U06`, `USBLC6`…) are exempt from the POWER half
  only — they have no supply pin by construction, but a clamp still needs its ground. False
  positive when the rail exists under an unrecognized name — add a
  `[[rail]]` entry in config (derived rail names like `VBUS_FUSED`/`VCC_3V3_A` are recognized
  already; `VBUS_DET`-style *signals about* a rail deliberately are not). Note the waiver quirk: `[[erc_waiver]]` for these two rules matches
  the waiver `net` field against the component **designator**, not a net name.
- `ERC_DANGLING_NET`: single-pin net. Real for forgotten wires; false positive for intentional
  test points — waive via `[[erc_waiver]]` (rule tokens are the lowercase forms, e.g.
  `dangling_net`) rather than ignoring the whole check.
- Non-ERC findings (power/BOM/nets/layout codes) use the generic `[[waiver]]` table instead:
  any finding `code` + optional `refs`, and a `reason` is required — see the
  akcli-schematic-review skill for the full syntax.
- `ERC_NET_ALIAS`: NOTE-only by design (the merge was deliberate). Escalate only if the merged
  names should not be one net (see §1 wrong-merge).
- BOM findings: duplicate designators are ERRORs and almost always real; refdes gaps are
  NOTE-level hygiene.

Exit semantics: `check`/`diff`/`pinmap` exit **1** when any finding is WARNING or worse; use
`--fail-on never` (`--exit-zero` is its deprecated alias) when you want the report
without tripping scripted error handling.

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
  `INVALID_INSTANCES_PATH`, `POWER_PORT_STACKED`, `SHEET_FILE_MISSING` are ERRORs — any one of
  them (or any errored op) makes `draw` exit
  **6** and refuse the write; `NO_CONNECT_CONFLICT` is a warning. A `DANGLING_ENDPOINT` on a
  wire end that visibly touches another wire means a **T with no `(junction)`** — eeschema
  reads those as two nets, so add `add_junction` at that point (akcli inserts it automatically
  for wires it draws). Read the finding's `; note:` suffix before deleting anything: when the
  same wire carries a net label along its span, or its other end is genuinely connected, the
  message says so and tells you to **trim the wire to its last connected point instead** —
  deleting the whole wire would break that net; the honest repair is usually a shorter
  `add_wire`, not a `delete_object`. Fix the op-list (usually a wire
  endpoint that misses a pin — prefer `"REF.PIN"` endpoints, which snap exactly) and re-run the
  dry-run until the connectivity block is clean. Run
  `akcli check <target> --integrity --fail-on never` first when the same codes may
  already be in the target: the gate reports pre-existing document damage as if
  your ops caused it.
- **`INVALID_INSTANCES_PATH` on a hierarchical child file is no longer expected.**
  A child `.kicad_sch` of an existing hierarchy accepts ops (its `/<top-root>/<sheet>`
  instance paths are valid); a single-segment path must still name the file's own
  root uuid. What is still unsupported is naming a sub-sheet PATH from the
  parent — route the op onto the child with the envelope's `sheets` map + a
  per-op `"sheet"` tag instead (or target the child file directly).
- **Hierarchy-era codes (0.20), each with a precise fix:** `SHEET_UNKNOWN` —
  a `"sheet"` tag names no `sheets` entry (did-you-mean rides along), or the
  declared child file does not exist (`akcli new` it first).
  `INSTANCE_REFS_REQUIRED` — the target sheet is instantiated more than once,
  so a scalar designator would annotate every instance with one refdes; the
  error lists the instance paths — copy them into
  `instance_designators` maps. `PROJECT_ROOT_AMBIGUOUS`
  — several roots instantiate the target and the net gate refuses to guess
  the tree: pass `--root FILE` (or `--flat` to accept the truncated
  single-file view). `PROJECT_ROOT_MISMATCH` (exit 2) — your `--root`'s tree
  does not contain the target. On a child target the "Net changes" block is
  evaluated over the WHOLE sheet tree — a diff that looks bigger than your
  edit usually means the tree view is showing members the single-file view
  never could.
- Per-op failures name the culprit: `SYMBOL_NOT_FOUND` → add `--symbols`, but when it hits
  EVERY `lib_id` including `Device:R` the op-list is fine and the environment is not —
  `akcli doctor --require symbols` resolves `Device:R` down the real path and says where it
  broke (see the akcli-setup skill); `VERIFY_FAILED` on
  `set_component_transform`/`set_component_parameters` → the designator does not exist in the
  target (check with `akcli component`); `PROTOCOL_MISMATCH` / `OP_UNSUPPORTED` → stop and
  report, do not retry blindly.
- **`TARGET_LOCKED` is a PRE-write lock gate, not a connectivity gate.** It fires before ops
  are even evaluated, when a KiCad GUI holds the target open (a `~<name>.lck` file next to it)
  — it says nothing about whether the op-list itself is sound. `--allow-open` is explicit risk
  acceptance that a concurrent GUI save could overwrite the edit; use it only after confirming
  that is safe, and do File>Revert in KiCad afterwards so the GUI picks up the write.
- `--dry-run` is accepted but **inert** — dry-run is already the default; only `--apply` changes
  behavior. Do not treat `--dry-run` as extra safety.
- **Backup/rollback:** a successful `--apply` writes `<name>.kicad_sch.bak` under the
  workspace's `.akcli/backups/` before replacing the target. To roll back, run `akcli undo`
  (or copy the `.bak` over the target). If the target changed on
  disk between akcli's read and write, apply aborts with `VERIFY_FAILED` and touches nothing —
  re-run from a fresh dry-run.
- **Multi-sheet rollback (`sheets`-routed op-lists):** the transaction copies every
  pre-write file into `.akcli/backups/runs/<run_id>/` (the `run_id` is in the draw's
  status line, `--json` payload, and `akcli log`). `akcli undo <target> --run <run_id>
  --apply` restores ALL files of that run together — same command again is a redo.
  It refuses when any file changed after the run (a swap would discard that later
  edit). A partial commit rolls itself back automatically; `TXN_ROLLBACK_FAILED`
  means even that failed — the backup set is intact, run the `undo --run` above.
- **Re-read-after-apply discipline:** an exit-0 apply proves the write landed, not that intent
  was met. Immediately `akcli net board.kicad_sch <NET>` / `akcli component board.kicad_sch <REF>`
  the touched objects and compare against the op-list. Replaying the same op-list is idempotent
  (deterministic UUIDs), so re-running `--apply` after a partial investigation is safe.

## 4. Tool-level triage

Exit-code table (frozen in `errors.py`):

| Code | Meaning | Typical action |
|---|---|---|
| 0 | success / no findings | proceed (but see vacuous-pass caveats, §2) |
| 1 | check/diff/pinmap findings ≥ WARNING | triage findings; `--fail-on never` for report mode |
| 2 | usage / config error (`BAD_CONFIG`, bad flags, `export --json`) | fix the invocation or TOML |
| 3 | parse error (corrupt OLE2 / S-expr, alloc guards) | suspect the file; report upstream |
| 4 | file not found | fix the path |
| 5 | unsupported format (`ALTIUM_UNSUPPORTED`, wrong input kind) | expected refusal — see below |
| 6 | op-list / verify failure (also `TARGET_LOCKED` — GUI lock, see §3) | fix ops or connectivity (§3); or close the GUI / `--allow-open` |
| 7 | external tool missing / network | install hint on stderr; `jlc` needs network, `sim` needs libngspice (`NGSPICE_MISSING`/`NGSPICE_FAILED`) |
| 8 | query miss (`QUERY_MISS`: `net`/`component` asked for a name/ref the parsed file does not contain) | fix the name — or treat as a clean "absent" answer when probing (§1) |

Known deliberate refusals (exit 5 — **unsupported, not corrupt**; do not file these as parse bugs):
binary `.SchLib` symbol records; binary `.PcbDoc` fills/regions/texts/polygons (pads/vias/
tracks/arcs ARE decoded since post-v0.3.1 — an exit-5 there means an unknown record type);
and feeding `.SchLib`/`.PcbDoc`/`.kicad_pcb` to schematic-only commands (`net`, `component`,
`check`, `diff`, `pinmap`, `export`) — the stderr notice says to use `akcli read` instead, which
does accept them. Exit 3 with `ALTIUM_ALLOC_GUARD` on a large file may be the DIFAT-spillover
limit rather than corruption. An unknown OLE2 container now deterministically exits 5 (format
detection by storage layout) — it is no longer misread as an empty schematic.

What to report upstream when akcli itself seems wrong: the one-line `ERROR: CODE: message` from
stderr, the full traceback from re-running with `--debug`, the `akcli --version` output (package +
protocol version), the exact command line, and the smallest input file that reproduces. Parse
failures (exit 3) on files that open fine in Altium/KiCad are always report-worthy.

With `--json`, a failure still yields parseable stdout instead of an empty stream:
`{"schema_version":"1","error":{"code","message","exit","remediation"}}` — `remediation` carries
the actionable next step for every error/exit code, so a caller can branch on `error.code` instead
of scraping the stderr `ERROR: ...` line.

## 5. `akcli sim` as a hypothesis tester

Connectivity tools tell you *what is wired to what*; they cannot tell you *why the
node sits at the wrong voltage* or *why the detector never latches*. When a bug is
behavioral, turn the hypothesis into a simulation and let libngspice adjudicate.

The move: state the hypothesis as a measurable bound, build a minimal `sim.json`
that reproduces the suspect stage, and run it.

```bash
# "the divider should sit at half-rail but the ADC reads full-scale"
akcli sim board.kicad_sch --deck-only              # (1) read the deck: is the node even wired right?
akcli sim board.kicad_sch --sim probe.sim.json     # (2) measure it: does v(mid) match the theory?
akcli sim board.kicad_sch --sim probe.sim.json --json | jq '.measured'   # raw numbers to compare
```

Debugging tactics specific to `sim`:

- **`--deck-only` first, always.** It runs with no ngspice and shows the exact
  net→node mapping. A bug is often visible right here: a stimulus wired to a
  *floating* node because its `node` field did not match the net name after
  sanitizing (net `+3V3` → node `_3V3`; the stimulus must say `"+3V3"`, the net
  name, not `"3V3"`). A node that should be shared but shows two different tokens
  is a real connectivity split.
- **`SIM_UNMODELED` warnings are diagnostic.** If the stage under test contains a
  diode/transistor/IC the tool would not guess a model for, it is *omitted from
  the deck* — so a "wrong" measurement may just mean the part isn't in the
  simulation. Model it (`Sim.*` fields, a `models` entry, or `fit_diode`) before
  trusting the result.
- **`SIM_PART_SKIPPED` is often the bug.** Any part the deck does not emit — DNP/
  `NP`/`OPEN` value, `Sim.Enable=0`, a `models` `skip`, a connector/test point, or
  unmodeled — is reported here, and the finding is an **ERROR** (exit 1) when it
  has ≥2 connected nodes and an assertion measures one of them. A divider that
  "measures the rail" usually has one leg skipped, not miswired. When the part
  genuinely has no obtainable model (a PMIC, a radio module), list its
  designator in the spec's top-level `acknowledged_unmodeled` array — that one
  part's escalation downgrades to WARNING (visible, non-blocking) and a stale
  entry is called out with `SIM_UNMODELED_ACK_UNUSED`.
- **`SIM_VALUE_INVALID` (exit 6) is a refusal, not a crash.** A value SPICE cannot
  take confidently (`LED red`, `TBD`, an ohms-marked `600R` on an `L`) stops the
  run; `10k 1%`-style trailing tokens are dropped with a `SIM_VALUE_NORMALIZED`
  warning instead. Read those warnings: the normalized value is what was actually
  simulated.
- **`SIM_NODE_COUNT_MISMATCH` (exit 6) means the model's terminal count and the
  part's pad count disagree** — the classic case is a 2-terminal lumped
  equivalent (`{"device": "C", "value": "33n"}`) on a 3-pad part, which would
  otherwise reach ngspice as a 3-node capacitor and die with a bare `circuit
  not parsed`. Fix: add `"pin_order": ["1", "3"]` to the `models` entry naming
  exactly which pads are the SPICE terminals (`pin_order` selects *which* pads
  reach SPICE, not just their order).
- **An assert bound to `op` is rejected** (`SIM_OP_ASSERT_UNSUPPORTED`, exit 2) —
  `.meas` needs a plot. Re-declare the analysis as a single-point `dc` sweep
  (`"dc": "Vsupply 3.3 3.3 1"`) to assert an operating point.
- **A failed measurement (`SIM_MEAS_FAILED`) is a finding, not a crash.** A `WHEN`
  edge that never crosses (e.g. `t_detect` on a detector that never latches)
  comes back as `None`/`failed` — which is frequently the bug you were chasing.
- **A `SIM_SUSPICIOUS_ZERO` warning means an upper-bound-only assert measured
  exactly 0** — a dead branch (load component deleted) passes `MAX < bound`
  vacuously, so that green is unverified. Check the branch is populated, and
  make critical asserts falsifiable: pair `lt`/`le` with a `gt`/`ge` lower
  bound, or use `approx`+`tol`.
- **Bisect with the deck.** Copy the `--deck-only` output, delete stages, and
  re-measure to localize where the behavior diverges from intent.
- **The IS-1000× trap (model, not circuit).** A diode fit from eyeballed
  datasheet *curve* coordinates can land IS ~1000× high; its phantom reverse
  leakage then breaks peak-detectors and sample-holds. If a detector "won't hold"
  in sim but the topology is right, suspect the model — refit from a datasheet
  *table* row with an ideality prior (`fit_diode`, single point). See `docs/sim.md`.

Full reference (engine discovery, `sim.json` format, exit codes): `docs/sim.md`.

## Companion material

Basic workflows, op-list schema, config keys: **akcli-circuit-design** skill. Read-only review flows:
`/circuit-review`, `/circuit-diff`, `/circuit-pinmap`. Writes: `/circuit-draw` (user-triggered).
