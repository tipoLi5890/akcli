# Changelog

All notable changes to `akcli` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning policy

`akcli` ships **three** version numbers; this section is their contract.

- **Package version (SemVer `MAJOR.MINOR.PATCH`).** The single source of truth is
  `pyproject.toml`; `tools/sync_version.py` stamps it into `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json`, and CI fails on drift.
  SemVer rules:
  - **MAJOR** — backwards-incompatible change to the public CLI surface, the normalized data model, or
    the on-disk JSON exports.
  - **MINOR** — backwards-compatible new subcommands, flags, checks, readers, or ops.
  - **PATCH** — backwards-compatible bug fixes and internal changes.

- **`schema_version`** (stamped on every `Schematic`/`Pcb`/`Library` JSON export; currently `"2.0"` — `akcli.contract.SCHEMA_VERSIONS` is the single source of truth and `model.SCHEMA_VERSION` an alias of it).
  Bumped independently of the package version. A **minor** schema bump only adds optional fields
  (consumers must ignore unknown keys); a **major** schema bump may remove or rename fields and
  coincides with a package MAJOR bump.

- **`protocol_version`** (integer; currently `2`) governs the op-list document. It is bumped **only**
  on a breaking change to op shapes or the result object. Executors **reject a higher major
  `protocol_version`** with `ERROR: PROTOCOL_MISMATCH` rather than guessing. Adding a new optional op or optional op field does
  **not** bump `protocol_version`.

When in doubt, prefer additive, backwards-compatible changes and leave the version contracts untouched.

## [Unreleased]

## [0.21.0] - 2026-08-12

The 0.21 architecture rework: a whole-tree internal restructuring
(`tools/equiv_snapshot.py`: 98 behavioral fingerprint cases against the
0.20.0 tree) that carried its external surface unchanged — plus, in the
same batch, the deliberate 1.0 contract breaks below. Everything outside the
BREAKING section is behaviour-preserving.

### BREAKING

Migration guide: [docs/migration-0.21.md](docs/migration-0.21.md).

- **op-list `protocol_version` 1 → 2.** Four op fields were renamed; a
  document still declaring `1` is refused with `PROTOCOL_MISMATCH`, and a v1
  field name on a v2 document is refused with an explicit rename line
  (`field 'orientation' was renamed to 'rotation' in protocol 2`) instead of
  a bare unknown-field error.

  | op | v1 field | v2 field |
  | --- | --- | --- |
  | `add_net_label` | `orientation` | `rotation` |
  | `set_component_parameters` | `reference` | `new_designator` |
  | `set_component_parameters` | `instance_references` | `instance_designators` |
  | `rename_net` | `from` / `to` | `old_name` / `new_name` |

  Values and semantics are unchanged in every case. `route_net` and
  `connect_and_label` keep their own `from`/`to` (pin refs, not net names),
  and `connect_and_label` keeps its own `orientation` — it expands into an
  `add_net_label` carrying `rotation`.

- **Canonical net names now follow eeschema's driver-priority ladder.** A net
  named by two different explicit names (a power port and a local label, a
  global and a local label) picks the name `kicad-cli sch export netlist`
  would pick, instead of the first name the reader happened to see. Existing
  designs can therefore report a DIFFERENT `Net.name` — the corpus board's
  `MID`/`VOUT` net now reports `VOUT` (with `MID` in `aliases`). Every
  displaced name stays reachable through `aliases`/`source_names`, so
  `check --intent`, `check --contract` and sim probes still match on it.

- **Export `schema_version` 1.3 → 2.0** on every `Schematic`/`Pcb`/`Library`
  export and on the `check`/`review`/`diff` finding envelopes, marking the
  net-name change as a major (renaming) schema bump rather than an additive
  one.

- **The `--json` error envelope gained `akcli_version`** (next to
  `schema_version`). Additive for readers that ignore unknown keys; listed
  here because the envelope is a frozen contract.

### Changed (internal architecture)

- **`prepare_oplist` returns a `PreparedOpList` record** (`doc` / `errors` /
  `groups_resolved`) instead of a `(doc, errors)` tuple, and group resolution
  no longer smuggles a `_groups_resolved` marker key INTO the op-list
  document — the flag is the resolved document's type, so no internal
  pipeline state can leak into a payload that echoes the op-list.

- **Layering is now a tested invariant.** `tests/test_layering.py` freezes a
  ranked import graph over `src/akcli` (AST scan, lazy imports included,
  iterative SCC cycle check, shrink-only waivers). The one historical import
  cycle (`arrange` ↔ `groupframe` ↔ `checks.layout`, hidden behind lazy
  imports) is gone.
- **New `akcli.sexpr` package (IR layer).** `readers/sexpr.py` →
  `sexpr/parser.py` (the ONE parser/serializer), `writers/sexpr_writer.py` →
  `sexpr/writer.py` (atom builders), plus `sexpr/query.py` (shared read-only
  accessors: `root_uuid`, numeric children — formerly duplicated across four
  modules) and `sexpr/build.py` (SNode construction helpers out of the
  writer monolith).
- **New `akcli.geometry` package.** `writers/geometry.py` →
  `geometry/transform.py` (unchanged math, plus `point_world_mil` hoisted
  from the reader); `geometry/boxes.py` (bounding-box primitives formerly
  private to `checks/layout.py`); `geometry/segments.py` (integer segment
  tests + union-find shared by the redundant-wire planner).
- **`arrange` is a package.** `arrange.py` + `groupframe.py` →
  `arrange/{collect,plan,frames,propose,groupsfile}.py`; frames no longer
  reach back into the planner through lazy imports.
- **The 2,278-line `writers/kicad.py` is now a 14-module package** behind a
  re-export facade: `executor` (apply/run_ops/verify), `registry`
  (`OpCall`/`Handler`/`@op` decorator), `context` (`ExecCtx` dataclass —
  formerly an untyped dict), `anchors`, `properties`, `placement`, and six
  `handlers/` modules (one per op family). Handler signatures are uniformly
  `Callable[[OpCall], list[str]]`.
- **`commands/drawing.py` (1,712 lines) shed its engine half** into the new
  typed `akcli.pipeline` package (`draw`, `netgate`, `preview`, `undo`,
  `arrange_apply`) plus `writers/blank.py` and `writers/backups.py`;
  command modules now only translate argparse ↔ engine ↔ JSON envelope.
- **`commands/query.py` and `commands/jlc.py` shed theirs too.** `net --why`'s
  ~120-line attachment diagnosis became the typed `akcli.explain` engine;
  `bbox`'s body∪pin-tip union became `geometry.boxes.placement_boxes_nm`;
  `groups --frame`'s write half joined `pipeline.arrange_apply`
  (`apply_group_frames`); `jlc bom`'s catalog check, table-row assembly and
  summary line plus the shared exact-MPN resolution moved to
  `parts/bom_report.py`, and `jlc add --place`'s planner (with the
  read-back-from-artifacts symbol name/value) to `parts/place_oplist.py`.
- **One implementation per semantic.** The six identical S-expression
  quoters collapsed into `kicad_escape.quote_atom` (every historical name is
  pinned `is`-identical); the unused second serializer (`serialize`) was
  deleted (`sexpr.dumps` is the one); the three refdes-prefix copies
  collapsed into `classify.refdes_prefix` (+ new `classify.split_refdes`);
  the two SI prefix tables now derive from one base table
  (`akcli.si_prefix`), with each consumer's deliberate alphabet/tolerance
  differences kept and pinned by tests.
- **The op vocabulary has a single source of truth.** New
  `src/akcli/opspec.py` (typed, declarative) describes every op/macro —
  fields, constraints, templates, group-coordinate classes, handler
  bindings, capability rows, every schema description. `schemas/ops.schema.json`
  and `ops.capabilities.json` are now **generated** from it
  (`tools/gen_ops_schema.py`, byte-stable, `--check` drift gate in CI);
  adding an op went from touching 6+ hand-synced tables to one spec entry
  plus one handler.
- **`ops.py` is a package** (`ops/{base,vocab,lints,template,macros,groups,
  validate,schema_io}.py` behind a flat re-export shim); every vocabulary
  table is derived from `opspec` at import time. `tools/schemas.py` is the
  one schema entry point (`build`/`check`; `gen_ops_schema.py` and
  `sync_schemas.py` remain as thin delegates), wired into CI lint.
- **Version stamps have a central registry** (`akcli.contract`):
  `SCHEMA_VERSIONS` / `PROTOCOL_VERSIONS` (four input protocols stay
  separate keys on purpose) / `JOURNAL_VERSION`. Every stamp site reads the
  registry; new gates assert `schemas/*.json` version consts equal the
  registry and refuse new hardcoded version literals.
- **Node-identity seeds have one home** (`writers/identity.py`): every uuid
  seed format (`R3`, `R3#u2`, `R3.pin1`, `wire:…`, `label:…`, `sheet:…`,
  keyed annotations) is a named builder; `deterministic_uuid` lost its
  vestigial always-zero `op_index` parameter while the seed **strings**
  (including the historical `:0` suffix) stay byte-identical — pinned by
  hardcoded-uuid tests, because changing a seed re-identifies every node in
  every existing document.
- Symbol-level boundary gates: `_ExitWith`/`argparse` may not leave the CLI
  layer; `writers/kicad.py` must stay a def-less facade. The
  depth-sensitive `parents[2]` schema lookup re-anchored on the package
  location (a gate now refuses new depth-sensitive anchors).

### Fixed

- `bom_policy.refdes_prefix` and `ops`' macro-role prefix check now classify
  synthesized `$U7`-style designators (Altium parts whose designator record
  is missing) the same way the rest of the tree does, instead of silently
  returning no class. (Surfaced by the convergence's differential census;
  affects BOM class rows for Altium imports with missing designators.)

### Added (verification infrastructure)

- `tools/equiv_snapshot.py` — behavioral fingerprint differ (read-only
  command matrix × every fixture + apply-twice over every known op-list);
  the mechanical "this refactor changed nothing" proof, with a determinism
  self-check in CI.
- Meta-gates: AST-gate scan-surface coverage census, depth-sensitive path
  anchor whitelist, SPEC module-table ghost check
  (`tests/test_gate_coverage.py`); mypy-scope census with an explicit,
  shrink-only untyped-module ledger (`tests/test_mypy_scope.py`).
- eeschema net-name parity fixtures for DIFFERENT-name driver competition
  (power vs local label, global vs local), strict-xfail until the planned
  canonical-net-name alignment; verified against a real `kicad-cli`.
- `docs/pin-identity.md` — the audited pin-identity model and its
  major-version migration boundaries.
- New `akcli-net-naming` skill (13 skills total): predicts and debugs which
  name a net gets under eeschema's driver priority; the authoring, review,
  debug and sim surfaces cross-reference it.

## [0.20.0] - 2026-08-08

The hierarchy-first architecture update, complete in one release: the net
gate evaluates the sheet TREE a child belongs to, one op-list edits several
sheets in one atomic transaction, a reused sheet gets per-instance
references, and the agent contract surface becomes sliceable and
schema-reachable.

### Changed

- **`plan`/`draw` on a child `.kicad_sch` of a hierarchy now evaluate the
  net-diff over the whole sheet tree**, not the child alone. A child read
  standalone is a truncated graph — its global labels lose every member the
  sibling sheets contribute — so the previous behaviour could grade a
  split/merge against the wrong design ("connectivity is the ONLY hard write
  gate" was silently false for hierarchical projects). The root is
  auto-discovered by reverse-indexing the project directory (nearest
  `.kicad_pro` ancestor, bounded climb); `--root FILE` names it explicitly,
  `--flat` restores the single-file view. Writes that previously passed on
  the truncated view may now be refused with tree-level findings. Flat
  (single-file) targets short-circuit to the historical path — their output
  is byte-identical to 0.19.
- Root resolution failures are honest, never guessed: several claiming roots
  refuse with the new `PROJECT_ROOT_AMBIGUOUS` (exit 6; a single `.kicad_pro`
  stem tie-breaks), and a `--root` whose tree does not contain the target is
  the new `PROJECT_ROOT_MISMATCH` (exit 2). Under `--strict-nets` an
  unresolvable root fails CLOSED; advisory runs degrade to
  `net diff unavailable` (a missing diff beats a truncated one presented as
  the design's net effect).
- `plan`/`draw --render` on a child sheet renders the staged child from the
  tree evaluation (same one-dry-apply-two-consumers contract as before).

### Added

- **Multi-sheet op-lists in one atomic transaction.** The envelope gains an
  optional `sheets` map (`{"amp": {"file": "amp.kicad_sch"}}`) and every op
  an optional `"sheet"` tag routing it onto the declared child file
  (`protocol_version` stays 1 — purely additive; an undeclared tag is the
  new `SHEET_UNKNOWN`, with did-you-mean; a `sheet`-tagged macro expands
  with the tag riding onto every child op). The write is a transaction
  (`writers/txn.py`): every dirty file is staged — temp + fsync +
  connectivity-verified — before the first `os.replace`; the pre-write bytes
  of every file land in `.akcli/backups/runs/<run_id>/` with a manifest (the
  recovery unit, deliberately journal-independent); a partial commit rolls
  back automatically, and only a failed rollback is `TXN_ROLLBACK_FAILED`
  (loud, with the backup set intact). Each routed file keeps its own uuid
  namespace, so multi-sheet op-lists replay byte-identically per file. The
  draw `--json` payload carries `run_id` + per-file `files[]` on these runs
  (absent — not null — on single-file runs), and the journal records the
  transaction.
- **`akcli undo <target> --run <run_id>`** — all-or-nothing restore of every
  file a multi-sheet transaction wrote (three-way swap per file: the same
  command again is a redo). Refuses when any file changed since the
  transaction (a swap would discard that edit); dry-run by default.
- **Per-instance references (sheet reuse becomes writable).** A sheet file
  instantiated more than once is per-instance annotated in KiCad — so a
  scalar designator, which would write the SAME refdes onto every instance
  (a duplicate-refdes design), is now refused with the new
  `INSTANCE_REFS_REQUIRED` (the message lists the instance paths to use).
  `place_component` gains optional `instance_designators` and
  `set_component_parameters` gains `instance_references`
  (`{"<instance path>": "<REF>"}`, pairwise-distinct; `designator` stays the
  scalar uuid seed, so per-instance references never re-identify the node);
  power ports need no map — the writer allocates one `#PWR0n` per instance
  automatically. Locating a part by refdes (`_symbol_by_ref`, wire anchors,
  `set_component_*`) now matches ANY instance's reference, not just the
  first path entry's. The manifest's `constraints.hierarchy` is
  `"hierarchical_write_v2"` with a machine-readable `sheet_reuse: true`.
- **`akcli capabilities --section NAME` / `--command VERB`** — the ~96 KB
  manifest becomes sliceable: one section (`exit_codes`, `error_codes`,
  `global_flags`, `commands`, `ops`, `calculators`, `schemas`,
  `conventions`) or one command's entry (~3% of the full document) in a
  stamped envelope. An unknown VERB reports `found: false` and exits 8 (the
  query-miss convention). The no-flag document keeps its exact historical
  shape.
- **`akcli schema <name>`** — every packaged JSON Schema printed
  byte-for-byte (short names or full filenames; unknown names fail at parse
  time with the full list). A `pip install`-ed orchestrator no longer digs
  into site-packages for `ops.schema.json`.
- **`examples/circuits/`** — copy-paste-ready op-lists (`voltage-divider`,
  `rc-lowpass-with-power`) with pinned expected nets; CI applies the exact
  committed op-lists and asserts the nets plus apply-twice byte-equality, so
  the examples cannot drift from the tool.
- `draw-result.schema.json`: optional `project_root` + `evaluation_scope`
  fields, present ONLY when the gate ran tree-scoped (absent — not null — on
  the flat path, whose payload is unchanged).
- New modules `akcli.project` (root discovery + tree staging, read-only),
  `akcli.writers.atomic` (the atomic write decomposed into
  prepare/commit/abort — the two-phase-commit primitives; behaviour
  byte-identical) and `akcli.writers.txn` (the transaction).
- Agent-eval tasks 09 (hierarchical amp sheet — the multi-sheet envelope,
  cross-file `GND`, sheet-pin↔hier-label pairing; hierarchy coverage was
  zero) and 10 (bus fan-out — `add_bus`/`add_bus_entry` coverage was zero);
  the harness now copies a task's `setup/*.kicad_sch` children next to the
  board.
- The agent surface teaches all of it: the `akcli-schematic-authoring` /
  `akcli-circuit-design` / `akcli-circuit-debug` / `akcli-altium-interop`
  skills and `/circuit-draw` now cover the `sheets` envelope, tree-scoped
  net-diff (`--root`/`--flat`), per-instance references, `undo --run`, the
  new error codes, and the capabilities slices — replacing the
  one-op-list-per-FILE prose that 0.20 obsoleted.

### Changed (write gates)

- `DUPLICATE_DESIGNATOR` now counts per **path entry**, not per symbol node:
  one symbol carrying the same reference on two instance paths of a reused
  sheet is two placements of that refdes (previously invisible).
- A multi-sheet apply GUI-lock-checks EVERY file up front
  (`safety.refuse_if_gui_locked`), before touching a single byte.

### Fixed

- **BOM dedup could hide a duplicated refdes across sheet instances**: the
  `_identity` key was the bare symbol `UniqueId`, but a sheet file
  instantiated twice re-uses the file's symbol uuid for BOTH physical parts —
  two instances carrying the same (duplicate!) refdes were silently merged
  into one BOM line instead of raising `BOM_DUPLICATE_DESIGNATOR`. The key
  is now `(UniqueId, sheet instance)`.
- A scalar `set_component_parameters.reference` rename of a symbol on a
  multiply-instantiated sheet used to smash its per-path references into one
  duplicate refdes; it now refuses (`INSTANCE_REFS_REQUIRED` — use
  `instance_references`).

- **`place_gnd`/`place_vcc` no longer die `SYMBOL_NOT_FOUND` on a stock
  KiCad install with no project `sym-lib-table`.** The sugar power ops carry
  no explicit `lib_id` — the writer injects it at execution time, AFTER the
  symbol-source scan collected the lib_ids to resolve, so the lib-table
  fallback never learned the implied `power:GND`/`power:VCC` nickname. The
  scan now sees sugar ops. (This was the documented rail-port pattern in
  `docs/op-list-authoring.md` failing in the most common environment.)
- `CHANGELOG` version-policy prose claimed `schema_version` is "currently
  1.0" while `model.SCHEMA_VERSION` has been `"1.3"` since 0.17 — corrected
  and pointed at the single source of truth.

## [0.19.0] - 2026-08-06

A field-hardening pass driven by a multi-day agent-operated board campaign
(agent CLI edits interleaved with human GUI edits, sim/BOM/intent gates in
CI). Every defect below was reproduced from the field notes before it was
fixed; the behaviors the campaign leaned on hardest gained regression locks.

### Added (checks)
- **`NC_PIN_CONNECTED` (ERC-lite, WARNING, waiver token `nc_pin_connected`).**
  A pin carrying a no-connect marker while sitting on a >=2-pin net — KiCad's
  native ERC catches this contradiction, akcli only ever used markers to
  *suppress* the dangling-net rule, so the classic agent-flow accident (an
  op-list edit wires up a pin still carrying a stale marker) sailed through
  every akcli gate, twice, on a real board. Rides on inferred net membership,
  so it works on any input format and sees label-only connections — the
  `--integrity` `NO_CONNECT_CONFLICT` rule covers only the wire-based subset
  on `.kicad_sch` (the overlap is deliberate defense-in-depth).

### Added (sim)
- **`SIM_NODE_COUNT_MISMATCH` (exit 6): the deck builder refuses an element
  whose node count disagrees with its device letter's terminal arity**
  (R/C/L/D/V/I/B = 2, Q = 3; `X` exempt — port count is model-defined). A
  2-terminal equivalent model on a 3-pad part used to emit a 3-node capacitor
  that ngspice rejected with a bare `circuit not parsed` (exit 7); the
  structured refusal points at `pin_order`, whose pads-selection use case —
  previously discoverable only by reading the source — is now documented.
- **`SIM_SUSPICIOUS_ZERO` (WARNING): an upper-bound-only assert that measures
  exactly 0 is flagged as a vacuous pass.** Delete the load component from a
  measured branch and `MAX i(...) < 50m` still passes — CI stayed green on a
  real project while the circuit under test was no longer being verified,
  caught only by comparing against historical values. A live branch shows
  float noise, never bit-exact 0, so the warning fires only on the vacuous
  shape; the PASS verdict and exit code are unchanged. Docs gain the
  pair-`lt`-with-`gt`/`approx` best practice.
- **`acknowledged_unmodeled` (sim.json top level): a per-designator, audited
  escape hatch for the in-series `SIM_PART_SKIPPED` ERROR.** Since 0.17 a
  measured node touching a part with no obtainable SPICE model (a PMIC, a
  radio module) was an unconditional ERROR — mass false-reds on a real
  project, which considered pinning the previous release. Listed designators
  downgrade to WARNING (visible, never dropped); unlisted parts keep the
  full ERROR; a stale entry raises `SIM_UNMODELED_ACK_UNUSED` (NOTE) so a
  blanket list cannot rot silently. Schema mirrors updated in lockstep.

### Fixed (release gating)
- **`release preflight --sim` never read deck-level findings at all** — a
  release could PASS its sim gate while a series element was absent from the
  very measurement being asserted (the standalone `akcli sim` CLI already
  refused such a run). Deck ERROR/CRITICAL findings now join the gate; deck
  WARNINGs stay out (the gate's any-WARNING-fails posture would have turned
  routine unmodeled parts into false reds).
- **Config `[[waiver]]` entries now apply to sim findings** in both the `sim`
  CLI and the preflight sim gate — the same reviewed-acceptance governance as
  `check`, instead of no escape hatch at all.

### Added (CLI ergonomics)
- **`jlc search --category NAME`** — case-insensitive exact-match post-filter
  (jlcsearch has no server-side category parameter) with over-fetch so a
  small `--limit` applies to the *filtered* list. A value/package query like
  `0402 47K` mixes NTC thermistors (Sensors) into a resistor search; an agent
  keying on value+stock picked the wrong part in the field.
- **`-o` short alias on every bare `--out` flag** (`library import-altium`,
  `review analyze/propose/testbench/validate`, `jlc datasheet/add`,
  `release preflight`, `sim`) — the output-flag surface had drifted into
  three conventions and agents kept guessing wrong. A new census test walks
  the real parser tree: a future `--out` without `-o` fails, and the
  grandfathered `--output` spelling (`expected`/`render`/`export`) can only
  shrink.
- **The dangling-endpoint report names the wire's load-bearing side.**
  `DANGLING_ENDPOINT` pointed only at the free end; when the same polyline's
  other end or mid-span carried a label or reached a pin, the obvious "fix" —
  delete the whole wire — split a live net (caught by the plan-time net diff
  only after the fact). The message now appends "this wire also carries label
  X" / "the other end is connected", steering the edit toward trimming.
  Message-only: firing conditions, severity, refs and the write gate are
  unchanged.
- **`TARGET_LOCKED` remediation points agent+GUI interleaved workflows at
  gating write batches on `library check-lock`** instead of discovering the
  GUI lock mid-apply.

### Added (regression locks from the field's trusted paths)
- An all-fixture sweep pins `nets <x>.SchDoc --json` member serialization to
  the reader's inferred netlist — a field report of "every net name listed,
  every members array empty" could not be reproduced on current source
  (plausible causes: a pre-0.17 install, or a bus-fanout schematic — Altium
  Bus/BusEntry records are a documented unsupported gap that yields
  single-member nets, not empty ones); the sweep makes any future
  serialization fork fail loudly.
- The GUI rotate/mirror label-swap (a symmetric part rotated 180 degrees
  swaps which pin sits under each label — netlist changes, nothing looks
  wrong) is now a documented gotcha (`docs/kicad-format-gotchas.md`)
  prescribing `diff`/`verify`/`--intent` after every GUI session, plus a
  diff-engine test pinning that the swap is reported loudly (2-pin nets fall
  below the Jaccard match floor and present as removed+added with every pin
  listed — a matcher refactor must not lose a side). A single-file heuristic
  lint was assessed and rejected: without a baseline, a rotated symbol with
  a label on a pin is indistinguishable from the intended design.

### Changed (docs)
- `docs/sim.md`: `pin_order` pads-selection use case, `SIM_NODE_COUNT_MISMATCH`
  / `SIM_SUSPICIOUS_ZERO` / `acknowledged_unmodeled` / `SIM_UNMODELED_ACK_UNUSED`
  reference rows, upper-bound-assert best practice, `[[waiver]]` applicability.
- jlcpcb skill: **refill zones before DRC/Gerber export** — a stale saved
  fill produced dozens of ghost DRC violations on a real board.
- op-list authoring docs + schematic-authoring skill: vertical `place_array`
  with label-on-pin auto-orients text across the neighbor below
  (`LAYOUT_LABEL_OVER_SYMBOL` storm) — prefer `direction: right` or a wider
  pitch.

An architecture-assessment round followed the field-hardening pass: a
six-aspect review (module boundaries, CLI surface, schema governance, docs
governance, test architecture, extensibility) plus a capability push on
schematic *design*: generating correct-by-construction schematics from a
declared netlist instead of hand-computed coordinates.

### Added (schematic design capability)
- **`akcli ops from-intent DESIGN.json`** — compiles a design document (the
  `check --intent` nets table plus a `components` declaration and an
  optional `layout` block) into an ordinary op-list: grid placement sized
  from real symbol geometry (body, pin tips, label text), one net label per
  listed pin, no-connects on unlisted pins, optional group tagging.
  Deliberately zero new ops — output flows through the unchanged plan →
  dry-apply → netdiff → connectivity-verify pipeline. Round-trip acceptance
  is machine-enforced: the drawn sheet's `nets --intent-snapshot` reproduces
  the design's nets, `check --intent` over the same design file reports zero
  findings, and re-apply is byte-identical. Honest refusals
  (`DESIGN_UNSUPPORTED`): pin-name addressing (the message hands over the
  correct pin number), a pin number matching neither a symbol's numbers nor
  names, an undeclared component reference (a net member's `REF` missing a
  `components` entry), wildcard members, bus-syntax net names (safe under
  akcli's engine but eeschema would reinterpret them), a pin in two nets,
  coincident stacked pins on different nets, multi-unit parts with common
  unit-0 pins. New `schemas/design-intent.schema.json`; one file drives
  generation and stays the assertion baseline.
- **`akcli library new-symbol SPEC.json`** — generates a `.kicad_sym` from a
  pin table (number/name/electrical type/side/group): pins on the 100-mil
  grid, body sized from pin counts and name lengths, stacked pins only via
  the explicit same-name/same-type convention, deterministic bytes.
  Electrical type is required per pin — unset types are the known source of
  native-ERC pin-matrix noise in imported symbols. Generation refuses a
  coincident-pin symbol with or without `--apply` (a coincident pin is a
  connectivity-class defect — the documented exception to "geometry is
  advisory"). Grammar was verified fixture-first against a real `kicad-cli`
  before the renderer was written; the probe file is committed ground truth.
  New `schemas/symbol-pins.schema.json`.
- **`akcli library lint-symbols LIB.kicad_sym`** — standalone symbol lint:
  `SYMLIB_PIN_COINCIDENT` (ERROR), `SYMLIB_PIN_TYPE_UNSPECIFIED` (NOTE),
  `SYMLIB_PIN_OFF_GRID` (WARNING).
- **`akcli gate`** — the iteration-weight bundle of the four checks a
  designer re-runs after every GUI edit round: the full default `check`
  family, intent assertions, sch↔pcb verify, and BOM lock drift
  (offline/cache-only by default; `--live` opts into the network).
  Zero-argument in a configured project — the first consumer of the
  documented `[paths].schematic` config key (plus new `[paths].pcb` /
  `[paths].bom_lock`); per-gate PASS/FAIL/SKIPPED summary, worst-of exit.
  Distinct from `release preflight` (release-weight: provenance manifest,
  git cleanliness, fab/sim/review gates) and unrelated to the CI "ci-green
  gate" script. `preflight` and `gate` now share one gate-aggregation
  helper, and bare `check`'s default check-name selection is a single
  extracted source of truth.

### Fixed (CLI contract)
- **`review report --json` emitted text.** The subparser's `--format`
  defaulted to `"text"` and the handler never consulted the global `--json`
  flag — the one command that broke the "every subcommand accepts `--json`"
  contract.

### Changed (architecture hygiene)
- Apply-twice byte-equality is now census-enforced: a parametrized test
  applies every registered op and macro twice and asserts the second apply
  is a byte-identical no-op (zero violations found); a future op added
  without the property fails closed.
- Exit-code census de-vacuoused: the exit-code table must cover the
  error-code registry exactly (previously a missing mapping silently fell
  back to the PARSE exit).
- Cross-module private-symbol traffic removed: `root_uuid`,
  `net_candidate_names`, `child_mil`, `quantize`/`on_segment` are public at
  their single homes; `gui_lock_path` moved into `safety.py` so the leaf
  module no longer imports the executor (`writers.kicad` re-exports it).
- `sim`'s local exit-map duplicate deleted; the query miss payload is built
  by one helper.
- Docs conformance: the count gate now also guards skills/commands noun
  claims (eleven stale or format-inconsistent claims normalized); SPEC §6.1
  describes the real schema-validation coverage (a documented integration
  test that never existed is gone); CLAUDE.md gains a new-CLI-command
  checklist; `capabilities`'s `version_stamps` prose no longer lists `net`
  among the unstamped-array exceptions (stale — `net --json` has stamped
  since 0.9.0).

### Changed (doc/code consistency sweep)
- A surface-by-surface sweep of every prose claim against the code it
  describes: the CLI reference, README (all four locales), INSTALL, SPEC, the
  op-list / gotchas / releasing / sim / jlc / review / design-integrity guides,
  the config example, and the skills. Flags, exit semantics, JSON envelopes,
  worked-example values and behavior claims now match the shipped parser,
  checks and drivers rather than an earlier surface; the two release-gate
  finding codes are named where they fire, and `release preflight`'s exit
  semantics are stated.
- New machine-code census gates (`tests/test_docs_code_census.py`) fail closed
  on the drift class the fence gate cannot see: SPEC's error-registry recital
  and the CLI reference's exit table must match `errors.py` exactly, no doc may
  name a code the source does not define, the review-rule and sim-code catalogs
  are two-way censuses against their sources, and the `jlc bom` CSV header and
  the BOM lock-drift kinds are pinned to their emitters.

## [0.18.0] - 2026-07-26

A post-0.17.0 audit pass. 0.17.0 hardened 23 points but several of those fixes
were half-applied, and the audit reproduced the resulting regressions along with
two blockers on the main agent journey. Everything below was reproduced before it
was fixed.

### Fixed (write correctness — reproduced regressions from 0.17.0)
- **A power port kept its old symbol when its net changed.** Replay identity was
  `position + net name`, so re-applying an op-list after renaming a rail left the old
  symbol stacked under the new one and silently merged the two rails (`GND` folded
  into `+3V3` with the connectivity gate reporting clean). Identity is now position
  plus species (net-naming port vs `PWR_FLAG`), so the node is replaced in place.
- **Two power ports on one anchor silently overwrote each other**, both ops reporting
  `ok`. Now refused by `POWER_PORT_STACKED` at both the op-list validator and the
  writer, and `connectivity.verify` reports pre-existing stacked ports in files
  already damaged by this. All three layers key on the coordinate *and the
  species*, so `place_pwr_flag` sharing a rail port's point — the prescribed way
  to mark a rail driven — stays legal, while two flags on one point do not.
- **Un-annotated schematics were unwritable.** `DUPLICATE_DESIGNATOR` counted KiCad's
  placeholder `R?` references as collisions, so every op — even `add_text` — was
  refused on a sheet drawn but not yet annotated. References containing `?` are
  now exempt.
- **Only symbols got op-index-free UUIDs in 0.17.** Wires, junctions, no-connects,
  labels, bus entries, sheets and keyless annotations still seeded on the op index, so
  inserting one op ahead of them duplicated every one of them on the next apply. All
  keyless nodes now seed on self-distinguishing content, and a per-apply claim registry
  refuses two ops that would seed the same UUID.
- **Writes into a child sheet used the wrong instances path.** 0.17 relaxed the gate but
  left `instances_path()` emitting this file's own root UUID; a part written into a child
  of a twice-instantiated sheet made real `kicad-cli` report annotation errors. New
  symbols now copy the full path set of their siblings.
- **`add_sheet` accepted a child file that does not exist**, exited 0, and left a root
  that no subsequent command could read. Now refused with `SHEET_FILE_MISSING`; existing
  files with the same damage are reported by `connectivity.verify`.
- **The two connectivity engines disagreed on a mid-span T with no junction** — the write
  gate treated it as attached, the net engine (correctly, matching eeschema) did not, so
  `check --integrity` and `nets` could give opposite answers on a hand-drawn file. The
  write gate now requires the junction, and `test_engine_parity.py` pins the case.
- `relink-symbols` now runs `connectivity.verify` on its splice (as a delta) instead of
  only comparing net membership.

### Fixed (blocked journeys)
- **`render` crashed on 59% of the standard KiCad symbol library** — including this
  repo's own `groups_board` corpus — because every optional node in a symbol body was
  dereferenced unconditionally (`(pin_names hide)` with no `(offset)` was the reported
  case). All six sites are None-safe, and `render_art.py` joined the mypy beachhead so
  the Optional class of bug cannot return.
- **Symbols did not resolve on a stock KiCad install.** `${KICAD*_SYMBOL_DIR}` is
  compiled into KiCad rather than exported, so `pins`/`draw` failed with
  `SYMBOL_NOT_FOUND` until the user exported it by hand. These variables now fall back to
  paths derived from the located `kicad-cli` (macOS bundle, POSIX/Windows prefix), and
  `doctor` gained a `symbols` probe that resolves `Device:R` down the real path.

### Fixed (silently wrong output)
- **Macro op fields were never validated.** The CLI expanded macros before validating, so
  the macro branch was dead code: a typo (`valu`) or an invented field passed silently and
  the expander manufactured a plausible circuit. Validation now runs before expansion,
  with post-expansion errors mapped back to the author's op index.
- **Macro `designators` are positional, and swapping them corrupted the BOM silently** —
  `place_led_indicator` with `["D1","R3"]` produced a resistor called `D1` and an LED
  called `R3`, with no finding from any check. Each slot now declares its part role.
- **The sim value gate was wrong in both directions.** A bare `float()` let `1_000`
  through to the deck, where ngspice reads it as 1 Ω (measured: a 10k/1_000 divider
  outputs 3.3e-4 V instead of 0.3 V); `inf`/`nan` passed too. Meanwhile `10U`, `100N`,
  `4U7`, `1T` and Greek `μ` — all valid SPICE — were refused, and those are exactly the
  spellings Altium imports carry. Prefixes are now case-insensitive (except `m`/`M`,
  where case is the whole meaning) and non-finite input is refused.
- **Altium `Implementation` records ignored `ModelType`/`IsCurrent`**, so a part whose
  first model was a SPICE model got the SPICE model's name as its footprint — worse than
  no footprint, because it satisfied `BOM_MISSING_FOOTPRINT`. Footprint-class models are
  now selected by an allow-list with `IsCurrent` winning.
- Altium `Comment="=Value"` (an indirect parameter reference) was imported literally and
  overwrote the real value; it now resolves against the component's own parameters, with a
  collapsed warning when it cannot.
- Document-level Altium parameters (`Title`/`Revision`/`DocumentNumber`/`Date`) were
  discarded; they now surface as `title_block` on the schematic export.
- Netlist net names were emitted bare where real `kicad-cli` qualifies them by sheet
  (`/GND`, `/Amp/NAME`), and `(class "Default")` was missing — a downstream PCB fed the
  akcli netlist and then updated from eeschema would rename every net. Arbitrated against
  real `kicad-cli` across flat, hierarchical and twice-instantiated boards.
- **A malformed input produced `INTERNAL` and a raw traceback** across ~20 commands
  (non-UTF-8 bytes, a directory, an unreadable file), breaking the `--json` envelope
  contract, while `arrange`/`groups` read the same corrupt bytes leniently and answered
  `clean: true`. There is now one guarded reader, two new codes
  (`KICAD_BAD_ENCODING` → exit 3, `FILE_UNREADABLE` → exit 4), and `cli.main` classifies
  by exception type instead of hard-coding exit 3.
- `--json` output could contain bare `NaN`/`Infinity` (not valid JSON) and `calc` could
  raise `ZeroDivisionError` as `INTERNAL`; the shared JSON writer sets `allow_nan=False`
  and the calc registry converts domain errors into `CalcError`.
- `calc info <name> --json` printed plain text; a bare `akcli --json` exited 2 with an
  empty stdout; `-C` pointed at a missing or malformed config was ignored silently. All
  three now emit the proper envelope (`BAD_CONFIG` is finally reachable).
- `akcli new --title` with a newline wrote a file real `kicad-cli` refuses to open — the
  fourth of six s-expression quoters was missing control-character escaping. All six are
  unified and pinned byte-for-byte, with an AST gate against a seventh appearing.

### Changed (check/review signal quality)
- New `src/akcli/classify.py` is the single home for refdes prefixes, rail
  classification and part roles, replacing six diverging `_prefix` copies and two verbatim
  `_is_ground`/`_is_power` pairs. Synthesized designators (`$U3`, from an Altium import
  with no reference) previously classified as no-prefix in four review detectors, which
  silently skipped those parts.
- ERC no longer reports TVS/ESD arrays for having no power pin, and counts a passive tie
  to a rail (or a connector) as a driver — the corpus's five ERC findings were all false,
  and a correctly pulled-up enable was being flagged.
- Rail names derived with a separator (`VBUS_FUSED`, `VCC_3V3_A`) are recognized as power;
  detection signals (`VBUS_DET`, `VIN_SENSE`) deliberately are not.
- `X` is classified as a crystal by library evidence rather than as a connector by prefix,
  which had disabled the entire crystal rule family on the corpus's only crystal while
  emitting a false connector finding.
- `LAYOUT_POWER_ON_PIN` no longer fires on the label-on-pin anchoring this project's own
  authoring guide recommends; unlabelled wires crossing a power pin still do.
- I²C pull-up detection matches bus tokens rather than exact net-name suffixes.
- `review/topo.py`'s private value parser delegates to `calc.si`, so Altium-style `10U`
  values no longer make detectors skip a part silently.

### Changed (agent contract)
- `capabilities.ops.constraints.hierarchy` reports `hierarchical_write_v1`; it had been
  hard-coded to `flat_v1_only` since before 0.17 made child sheets writable, and a test
  was pinning the stale value.

### Added (drift gates — these are the ones that were missing)
- The release CI-green gate is rewritten: it asks the CI workflow for its conclusion on the
  tagged SHA, so "CI never ran this commit" and "CI ran and failed" are distinct states.
  The previous version counted its own check run, which made the "never ran" branch
  unreachable — a tag on a commit CI had never seen would have passed. It also asserts the
  four `windows-latest` matrix jobs by name, so a shrinking matrix fails instead of
  quietly weakening the gate.
- `_OP_FIELDS` field kinds and all ten enum tables are now asserted against
  `ops.schema.json` (only field *names* were compared before).
- Schema mirroring iterates both directories, so an orphan in `src/akcli/schemas/` is
  pruned or reported — it used to be invisible while being loaded in preference to the
  canonical file.
- The three hand-patched `newline="\n"` call sites in `_vendor/jlc2kicadlib` are recorded
  in `PROVENANCE.md` and pinned by a sentinel test; `_vendor` is excluded from the general
  text-I/O gate, so re-vendoring would have dropped them silently.
- CI asserts zero skips for `test_kicad_parity.py` (33 tests against real `kicad-cli` could
  previously degrade to green) and for the four ngspice test files; `test_kicad_parity.py`
  uses the product's own `kicad-cli` discovery instead of a second copy.
- `tests/golden/nets-groups_board.json` is committed, so `test_engine_parity.py` no longer
  skips permanently.
- `changelog_extract.py --strict` fails instead of falling back to `[Unreleased]` or a stub
  line, and a test asserts the `pyproject` version has a matching CHANGELOG section.
- `golden_regen.py` requires `--bless-all` to rewrite more than five snapshots at once.
- `hooks.json` command paths are asserted to exist; `calc.html`'s diagram table is
  asserted against the calculator registry; every `MAPPABLE` calculator's `--ops` output is
  validated (only two of eight were).
- `.claude-plugin/marketplace.json` carries the version and is covered by the drift gate
  (it had been documented as covered but was not); a missing manifest now fails.
- `pypa/gh-action-pypi-publish` is pinned to a SHA (it was a mutable branch ref on the job
  holding `id-token: write`), ruff's CI scope extended to
  `tools` and `hooks`, and pytest gained `filterwarnings = ["error"]` — the only warning
  gate that will give notice before a stdlib removal in a future Python.

## [0.17.0] - 2026-07-25

### Changed (write-identity — re-apply compatibility note)
- **Symbol/pin UUIDs are now seeded from the designator alone** (previously
  `designator:op_index`). Editing an op-list — inserting or reordering ops — no longer
  changes untouched parts' identity, so a re-apply after an edit updates in place instead
  of duplicating the whole design downstream of the edit (a reproduced data-loss class).
  Compatibility: a file written by an *older* akcli re-applied with an *edited* op-list
  may still duplicate parts whose original op index was ≠ 0 — re-apply the unedited list
  once (or start from `akcli undo`) before editing. Auto-allocated `#PWR` refs now
  recover their replay identity by position+net (op-index-free).
- **Hierarchical child sheets are writable.** The connectivity gate accepts
  `/<top-root>/<sheet>` instance paths, so ops apply to a child file of an existing
  hierarchy (previously every op — even `add_text` — was refused with a misleading
  `INVALID_INSTANCES_PATH`). Single-segment paths must still name the file's own root.

### Added
- **`akcli check --integrity`** — the write gate's document checks (dangling endpoints,
  duplicate uuids **and duplicate designators**, unresolved lib_ids, instance paths,
  no-connect conflicts) now run read-only over any `.kicad_sch`, and join the default
  check family; previously they only ran post-write inside `draw --apply`.
- **Duplicate annotation keys are rejected** — two `add_text`/`add_rectangle`/
  `add_text_box` ops sharing a `key` seeded the same uuid, so the second silently
  replaced the first's node while both reported ok; `validate_oplist` now refuses the
  op-list and the writer guards post-expansion collisions.
- **`KICAD_SEXPR_BAD_NUMBER` + `INTERNAL` error codes** — a corrupt numeric field in a
  `.kicad_sch` (bad merge, disk damage) raises a structured parse error instead of a raw
  `ValueError` traceback, and a last-resort handler keeps the `--json` stdout-envelope
  contract even for unexpected exceptions (traceback goes to stderr; `--debug` re-raises).
- **`tools/sync_schemas.py`** — one command syncs `schemas/*.json` into the packaged
  mirror (`--check` for drift); the op schema's 32 branches now declare
  `additionalProperties: false` (plus the universal `group` tag), so schema-side
  validation catches typos the internal validator already caught.
- **DNP parts satisfy nothing** — a `(dnp yes)` pull-up/load-cap/bypass-cap/TVS/EN-tie
  no longer silences the corresponding review/check rule (`topo.is_fitted` gates every
  presence-satisfier search); BOM enumeration still sees DNP parts.
- **`POWER_RAIL_NO_SOURCE`** (WARNING) — a recognized rail with ≥2 consumers and no
  potential source (no POWER_OUT/OUTPUT pin, PWR_FLAG, connector/battery member,
  entry-named or series-fed rail, or configured `[[rail]]`) is no longer fully silent.
- **Per-net ERC type-confidence** — a fully-typed net keeps full severity for the
  type-gated ERC rules even on a low-confidence board (one untyped connector no longer
  demotes the whole board's typed findings to NOTE).
- **GUI-open guard everywhere** — `relink-symbols --apply` and `library repair --apply`
  now refuse with `TARGET_LOCKED` like `draw`/`arrange`/`undo` (`safety.
  refuse_if_gui_locked`); a cross-engine parity test pins netbuild and the write gate to
  the same junction/T-span/near-miss verdicts.
- **mypy beachhead 33 → 38 files** — `netbuild`, `readers/sexpr`, `checks/diff`,
  `writers/geometry`, `groupframe` join the typed core.

### Fixed
- **argparse `--json` envelope could be swallowed by a prior in-process run** —
  stdout tracking now resets before parsing, so `_Parser.error`'s JSON envelope
  no longer depends on what a previous `main()` call in the same process emitted
  (embedders and test suites saw order-dependent behavior).

### Added (pre-existing batch)
- **Pin-level net queries** — `akcli net --at REF.PIN` (reverse lookup: which net is
  this pin on), `net --same A.PIN B.PIN` (binary connectivity + the common net), and
  `net --why REF.PIN` (attachment diagnosis: junction/wire-end/mid-span status, nearest
  wire end / label / other pin with mil distances, `off_grid_50mil` flag, actionable
  hints — built from the same quantized helpers `netbuild` uses). Unknown pins exit 8
  with a did-you-mean plus the valid pin list.
- **argparse failures are machine-readable under `--json`** — a wrong flag, wrong
  choice, or unknown command now emits the standard
  `{"error": {code: "USAGE", message, exit: 2, remediation}}` envelope (the argparse
  message, including the full `choose from` list, rides along); plain-text behavior is
  unchanged.
- **`REVIEW_FACTS_UNMATCHED` engine advisory** — `review analyze` now reports (NOTE)
  when audited facts files match no component MPN, instead of silently leaving those
  findings heuristic; the metadata block gains a `facts_unmatched` list.
- **`review drift`** — alias for `review diff` (findings-drift), disambiguating it from
  the net-level `akcli diff`.
- **`release preflight --sim <sim.json>`** — quantitative SPICE assertions as an opt-in
  release gate: violations become manifest findings; a *requested* sim gate that cannot
  run (no libngspice, deck build failure) FAILS rather than skips. No `--sim` = the usual
  skipped-with-reason.
- **`library relink`** — the library-family home of `relink-symbols` (same handler and
  flags; the top-level name stays).
- **`/circuit-datasheet` and `/circuit-release` slash commands** — direct entry points
  for the datasheet→facts flow and the release gate (eight `/circuit-*` commands total).
- **`NGSPICE_MISSING` / `NGSPICE_FAILED` / `NETWORK` are now registered error codes**
  (exit 7) with remediation hints — they were emitted as pseudo-codes the `--json`
  envelope could not recover; `jlc` network failures now raise through the shared error
  path so `--json` gets the standard envelope.
- **The blessed connection ladder** — docs/op-list-authoring.md and the authoring skill
  now converge the connection vocabulary into one decision ladder (label-on-pin default →
  `connect_and_label` → `route_net` → raw `add_wire` last), with bus/sheet/graphics ops
  explicitly demoted to an advanced layer.
- **`/circuit-sim` slash command** (`commands/circuit-sim.md`) — a direct entry point for
  simulation; the `akcli-schematic-authoring` skill's trigger description now also owns
  simulate/SPICE/ngspice requests. Both close the gap where "simulate this circuit" matched
  no skill or command.
- **Better op-list error messages for agents:** a top-level-array op-list now says exactly
  how to wrap it (`{"protocol_version": 1, "target_format": "kicad", "ops": [...]}`);
  `place_component` given `at` is told it takes `x_mil`/`y_mil`; a wire to a nonexistent
  pin lists the component's valid pins with a did-you-mean.
- **Altium reads no longer drop records silently** — every `.SchDoc` RECORD id the reader
  does not consume is counted into a skipped-record histogram and surfaced as a real
  reader warning (stderr `warning:` line + the `warnings` array in `read --json`):
  `altium: skipped 5 unsupported record(s): 26 (Bus) ×3, 37 (BusEntry) ×2`. Unknown ids
  print bare, known ones get their name from the (previously dead) `RECORDS` map, which
  now covers the documented schematic record ids. This makes the missing Bus/BusEntry
  support visible instead of inferring a quietly incomplete netlist. `Schematic.warnings`
  was hardcoded `[]` on both Altium exit paths before.
- **`.PrjPcb` multi-top and unreadable-child warnings** — a project listing more than one
  top-level schematic warns and names the sheets it ignored (only one top is read), and a
  project schematic that cannot be scanned for sheet symbols now warns with the file, error
  class and message instead of being swallowed (it silently skewed top-sheet detection).

### Fixed
- Bare `akcli review` / `akcli review facts` exited with code 49 (the byte count of the
  usage message — `sys.stderr.write(...) or EXIT` short-circuited) instead of the
  documented usage exit 2.
- `akcli draw --apply --dry-run` silently wrote the file; `--dry-run` now wins with a
  warning, so a defensively added flag can never cause a write.

### Changed
- **`net --json` output is now the `nets` envelope** (breaking for `net --json`
  consumers): the list branch emits
  `{schema_version, source, total/matched/returned/truncated, nets: [...]}` instead of a
  bare array, the `NAME` branch wraps its matches in the same envelope (plus per-net
  diagnostics), and members are `"REF.PIN"` strings everywhere — one parser now covers
  `net` and `nets`. Nets that carry aliases additionally expose a sorted `aliases` key
  in both commands (additive; golden snapshots regenerated).
- **`diff`/`verify` `--json` net members are now `"REF.PIN"` strings** (breaking for
  consumers of `added_members`/`removed_members`, which were `[ref, pin]` pairs) — ONE
  member encoding now covers `net`/`nets`/`diff`/`verify`; `schemas/diff.schema.json`
  updated (+ mirror). Deliberate 0.x decision: this ships under the existing
  `schema_version` (`"1.3"`) rather than forcing a model-wide major schema bump — this
  changelog entry is the migration notice.
- **`calc` names are argparse-native choices** — an unknown calculator now fails at
  parse time with the full `choose from` list (and the USAGE JSON envelope under
  `--json`) via a lazy choices container: the calculator registry still loads only when
  a `calc` command actually parses, so nothing else pays the import. `calc --help`
  documents the `list`/`info`/`batch` verbs explicitly.
- Bare `akcli review` / `review facts` now route through the shared usage-error path,
  so `--json` gets the standard error envelope (previously plain stderr text only).
- **`review analyze --fail-on` now accepts the full shared ladder**
  (`info|note|warning|error|critical|never`); the default stays advisory (exit 0), and
  both `check`-family and `review` help texts state the opposite gating defaults
  explicitly.
- A missing `add_wire`/`add_bus` `vertices` field is now ONE missing-field error (the
  misleading `NON_ORTHOGONAL_WIRE` duplicate is gone); a wrong-typed `vertices` is an
  `OP_UNSUPPORTED` type error like every other field.
- `akcli-circuit-debug` examples also move off the deprecated `--exit-zero`, and its
  near-verbatim copy of the check metadata-caveat table now defers to
  `akcli-schematic-review` (single source of truth); `render` help/docs state the
  default `<input>.svg` write explicitly (`-o -` for stdout); `expected`'s always-JSON
  output is documented as the one deliberate `--json` exception.
- Skill/doc guidance: `akcli-schematic-review` examples use `--fail-on never` instead of
  the deprecated `--exit-zero`; `jlc datasheet --fetch` examples steer to the project-local
  `--out datasheets` (where `review analyze` auto-discovers); `akcli-release-gating` states
  that simulation is not a preflight gate and CPL export stays in KiCad;
  `akcli-circuit-debug` documents the generic `[[waiver]]` table; `docs/SPEC.md`,
  `ROADMAP.md` and `akcli-altium-interop` drop the Altium live bridge entirely (see
  Removed) and state that there is no Altium write path, live or offline.

### Removed
- **The Altium live-bridge subsystem is deleted** — `src/akcli/drivers/altium_live/`
  (`bridge.py`, the DelphiScript `altium_api.pas`/`.PrjScr`, its README) and
  `tests/test_bridge.py` are gone. It had **no CLI wiring** and contradicted the 0.14
  KiCad-first positioning (Altium is import-only); it was already shelved indefinitely.
  Consequences: `ops list --json` drops the deprecated per-op `altium_live` duplicate key
  (`altium` remains, informational), `ops list` text says `altium:` instead of
  `altium-live:`, and `ops.capabilities.json` (both mirrors) replaces
  `altium_live_wired`/`altium_live_note` with `altium_write_wired: false` /
  `altium_write_note` — `akcli capabilities --json` now publishes
  `ops.altium_write_wired`. The `BRIDGE_BUSY`/`BRIDGE_TIMEOUT` error codes stay
  **registered but unraised** (reserved) so the frozen error/exit tables do not change.

## [0.16.0] - 2026-07-23

### Added
- **Japanese README** (`.github/README.ja.md`) — a full localization on the
  current KiCad-first positioning; the English and both Chinese READMEs gain
  the 日本語 language-switcher link.
- **Groups-envelope authoring is now first-class in the plugin skills.**
  `akcli-schematic-authoring` teaches the op-list `groups` envelope
  (group-local coordinates, macro tag propagation, persisted `Group`
  properties), `akcli groups --frame` module borders, `akcli bbox` spacing
  planning, the newer ops (`route_net`, `add_rectangle`, `add_text_box`,
  `set_title_block`, the `place_array` macro), `plan --render` visual
  previews, the `arrange --groups --page-width/--frames/--propose-labels`
  repair loop, `calc --ops` op-list drafts, sim corner sweeps
  (`--sweep`/`--wave`) plus `review testbench`, the project-local datasheet
  facts store, and the write-the-intent-file-before-drawing discipline.

### Changed
- **Skills and slash commands caught up to the 0.13–0.15 surface**
  (audit-driven): `akcli-setup` documents the PyPI install
  (`pipx install akcli-kicad`) and the doctor workspace probe;
  `akcli-schematic-review` documents the `signal.power_protect` power-entry
  rules (`REVIEW_FUSE_*` / `REVIEW_REVPOL_*`); `akcli-altium-interop` marks
  the Windows live bridge **SHELVED** and frames Altium as the read-only
  import surface; `akcli-circuit-design` is reframed KiCad-first and gains
  the newer ops, `groups`/`bbox`/`view live`, and the exit-8 row.
  Slash commands strengthened: `/circuit-draw` (read the Net-changes block;
  `--strict-nets` + `--note` on apply), `/circuit-review` (`review
  testbench`, markdown report, facts-store autodiscovery, `--fail-on never`),
  `/circuit-diff` (`--bom` line-level delta), `/circuit-parts`
  (`[paths] parts_dir`, BOM lockfile), `/circuit-pinmap` (uses
  `akcli expected` instead of in-repo adapter scripts).
- `akcli-schematic-authoring` trimmed back inside the skill size budget:
  conditional deep-dives (hierarchical sheets, review-proposal adoption)
  defer to `docs/op-list-authoring.md` and the review skills.
- CLAUDE.md's op-vocabulary lockstep list now includes the
  authoring/circuit-design skill prose, so op changes can't drift ahead of
  the skills again.

### Fixed
- `akcli-circuit-debug` wrongly claimed a `net`/`component` miss exits 0 —
  corrected to exit 8 (`QUERY_MISS`, structured `error` object under
  `--json`) and the exit-code table gains the missing 7-network note and
  8 row.

## [0.15.0] - 2026-07-22

### Added
- **First PyPI release — `pip install akcli-kicad`.** The distribution is named
  `akcli-kicad`; the import package and CLI command stay `akcli`, so you install
  `akcli-kicad` but run `akcli`. Publishing uses OIDC trusted publishing (no API
  token) via the `publish-pypi` job in `.github/workflows/release.yml`, gated on
  the `PYPI_TRUSTED_PUBLISHING` **repository** variable (a job-level `if:` is
  evaluated before the job's environment resolves, so an environment-scoped
  variable would leave the job silently skipped) and bound to the `pypi` GitHub
  Environment for OIDC. GitHub Releases continue to attach the sdist + wheel on
  every tag regardless.

### Changed
- Documentation refreshed across the READMEs (×3), `INSTALL.md`, `ROADMAP.md`,
  `CLAUDE.md`, and `docs/` for the `akcli-kicad` distribution name and current
  project status.

## [0.14.0] - 2026-07-22

### Added
- **Power-entry protection review (`signal.power_protect`, 5 rules — 44 total).**
  Nets named as board inputs (`VBAT`/`VIN`/`VBUS`/`DCIN`… as the WHOLE name, or
  rail-classified) seed a series chain walk (crossing only fuses/diodes/
  inductors, never into ground) judged for a series fuse
  (`REVIEW_FUSE_MISSING`) and reverse-polarity protection
  (`REVIEW_REVPOL_UNPROTECTED`; a shunt crowbar without its series fuse →
  `REVIEW_REVPOL_SHUNT_NO_FUSE`). Fuse sizing goes through
  `calc fuse-derating` and only with a facts-store `i_load`
  (`REVIEW_FUSE_UNDERSIZED`, datasheet_backed); an unparseable rating is
  `REVIEW_FUSE_UNRATED` (insufficient_evidence). Closes the M2/M3
  fuse-sizing + reverse-polarity backlog.
- **Faithful symbol artwork in `akcli render`** (`render_art.SymbolArt`): KiCad
  sources now draw the real embedded `lib_symbols` graphics — rectangles,
  polylines, circles, three-point arcs, beziers, pin stubs and (when the
  symbol doesn't hide them) pin names/numbers — every point through the net
  engine's own Y-flip + rotate-then-mirror transform, so artwork can never
  disagree with connectivity. Altium sources, multi-unit parts and unresolved
  symbols fall back to the synthesized pin-box body. New reader entry point
  `readers.kicad.read_embedded_library()`; render goldens regenerated.
- **Corpus board #2: `power_entry`** — akcli-authored calibration pair
  (protected VBAT: 500 mA fuse → series diode → bulk + decoupling; deliberately
  unprotected VBUS sense branch), with census/behavior pins in
  `tests/test_corpus_power_entry.py`, golden `nets/check/review/render`
  snapshots, and the replay baseline extended (the groups board honestly gains
  `REVIEW_FUSE_MISSING`/`REVIEW_REVPOL_UNPROTECTED` on its unfused VBAT).
- **Agent-eval task 08 (`protected-power-entry`)** — draws the fuse + series
  diode entry discipline through the real safety rails; fixture symbol
  library gains `Device:D` (pin 1 = K, pin 2 = A) and `Device:Fuse`.
  References stay CI-pinned at 100 % (8/8).
- **Output placement policy (SPEC §10)** — every file akcli writes is classified
  state / cache / deliverable, with an enforcement gate in
  `tests/test_output_policy.py` (state-root self-ignore, cache-defaults-outside-CWD,
  facts-store resolution, CWD-default literal census).
- `.akcli/` state roots are now **self-ignoring**: creating one drops a
  `.gitignore` containing `*` inside it, so the user's `.gitignore` never needs
  an entry and `git status` stays clean. Legacy roots self-heal on the next
  journaled write; `doctor` recognizes the internal ignore.
- `[paths].parts_dir` in `akcli.toml` — default output root for `jlc add`
  and `library import-altium` converted libraries (`--out` still wins;
  fallback default stays `./akcli-parts` / `<name>.pretty`).
- **Bare `akcli view`** — the dashboard no longer requires a file: `akcli view`
  alone serves the hub (calculators available, `/live` idle), and `view live`
  with no path auto-discovers the single `.kicad_sch` in the current directory
  (none or several candidates → explicit usage error, exit 2). CLI-layer
  contract in `tests/test_view_cli.py`.

### Changed
- **KiCad-first repositioning (docs + metadata).** READMEs, ROADMAP, SPEC
  mirrors, and all plugin/package descriptions now lead with "AI-native
  schematic design, purpose-built for KiCad"; "no EDA install" framing is
  dropped (the facts live in the READMEs' "Optional external tools" section),
  Altium is framed as read-only import into the KiCad flow, and the Windows
  Altium live bridge is **shelved indefinitely** (recorded under ROADMAP
  "Deferred by decision").
- The datasheet facts store is formally classified **deliverable** (not cache):
  `review facts` keeps its project-local `./datasheets` default — facts are a
  committed, code-reviewed team asset that survives handoff and feeds CI —
  with resolution centralized in one documented choke point, and review/release
  are gated (by test) from ever silently reading a personal XDG cache. Fetched
  PDFs remain cache-class (`~/.cache/akcli/datasheets`; `jlc datasheet --out
  datasheets` co-locates them with an in-repo store).

## [0.13.0] - 2026-07-18

### Added — repair loop, contract gates, real-board corpus
- **`arrange --groups --propose-labels OUT.json`** — turns a net-preservation
  refusal into a repair draft: `add_net_label` on every member pin of each
  named multi-bundle net, `delete_object` for the wire clusters those labels
  make redundant, and re-seating of stranded `#` satellites (PWR_FLAG,
  mid-wire ports) onto a pin of their net. Never touches the file — the
  draft goes back through `plan`/`draw`. Proven end-to-end on the real
  88-part/10-group production board, now a committed corpus fixture
  (`tests/fixtures/corpus/groups_board.kicad_sch` + `tests/test_corpus_groups.py`).
- **`check --intent` (bare)** falls back to `[paths] intent` in `akcli.toml`
  — the project's standing intent contract.
- **`akcli doctor` workspace probe** — flags legacy beside-the-file backup
  stacks, leftover KiCad `~*.lck` lock files, and an un-gitignored `.akcli/`
  (advisory; never `--require`-gatable).
- **Agent-eval task 07 (safe re-pack)** + a `postcheck_arrange.json` harness
  hook: the drawn sheet must survive `arrange --groups --apply` (the
  net-preservation gate) with the ground-truth nets intact.
- **Config-surface + schema-table conformance gates**: every accepted
  `akcli.toml` top-level table must appear in SPEC §3.11 AND
  `examples/akcli.toml.example` (which must load); every `schemas/*.json`
  must appear in SPEC §3.12 — both were stale, both are now gated.
- **mypy beachhead** extends to `config.py` and `arrange.py`.

### Fixed
- **`arrange --groups` label carrying is now collision-free**: moves run in
  two phases through a staging band (a later mover's original pins can no
  longer capture labels an earlier mover parked at its final slot), and at
  genuinely coincident pin tips the writer leaves one label behind per
  staying pin — both failure modes were observed on the real pod board.
- SPEC §3.11/§3.12 and `examples/akcli.toml.example` caught up with the
  actual config surface (`[check]`, `[bom]`, `[[waiver]]`, `[arrange]`,
  `grid`, `backup_depth`) and the 12 published schemas.

## [0.12.0] - 2026-07-17

### Added — group layout: 2D packing + clearance policy
- **`arrange --groups --page-width MIL`** — group blocks pack side by side in
  two dimensions (wrap past the page width) with `--group-gap` guaranteed on
  BOTH axes, instead of the single-column stack; the plan/`--json` report now
  carries each block's `at`/`size`. Driven by the pod-board case where the
  single-column layout was applied and immediately undone.
- **`[arrange]` config table** (`group_margin`/`group_gap`/`row_width`/
  `page_width`) pins the layout policy per project — e.g. `group_gap = 1000`
  guarantees a >= 1000 mil channel between functional groups on every re-pack.
- **`[check] group_clearance = N`** — `check --layout` flags any pair of
  groups closer than N mils (`LAYOUT_GROUP_CLEARANCE`, advisory), catching
  manual moves that squeeze the routing channel without overlapping.

### Fixed
- **`arrange --groups --apply` now enforces its net-preservation contract**:
  the rigid moves are dry-applied to a temp copy and the write is REFUSED
  (exit 6, split/merge lines on stderr) when the before/after netlists are
  not equivalent — previously a sheet wired across group boundaries could be
  re-packed into silent net splits/merges (observed on a real board: a
  rail-to-GND merge). `--allow-net-changes` is the explicit override.

### Added — workspace state engineering + pinout book
- **`akcli doc <file> [-o book.md] [--refs GLOBS] [--json]`** — the ROADMAP v0.10
  pinout book: per-IC/connector pin tables (pin number/name/type + the net each
  pin actually landed on), the `review tree` power-rail summary, and a grouped
  BOM, as deterministic Markdown (no timestamps; same input bytes -> same
  output bytes) or a `--json` payload.
- **Journal intent notes**: write commands (`plan`/`draw`/`arrange`/`undo`)
  accept `--note TEXT` to record *why* an edit was made next to the
  mechanical facts; `akcli log` prints the note under the entry. Sessions do
  not identify themselves — the design is the shared object.
- **`docs/agent-state.md`** — the workspace-state contract: where state lives
  (`.akcli/`, `akcli.toml`, intent snapshots, facts store), design-intent
  notes, and the session-resume ritual.

### Changed
- **Rotated draw backups moved into `.akcli/backups/`** (previously
  `<name>.bak` beside the edited file): `draw`/`arrange`/`sim fit-diode
  --write`/`jlc bom --fix`/`relink-symbols`/`groups --frames` all write there;
  journal entries and status lines reference the new path. `akcli undo` walks
  the new location and **falls back to a legacy beside-the-file stack** when
  `.akcli/backups/` holds no snapshots for the target, so existing workspaces
  keep their undo history. (`library repair` backups stay next to the repaired
  lib-table files — those live outside schematic workspaces.)

## [0.11.0] - 2026-07-17

### Added — BOM completeness (safety, semantics, workflow, reporting)
- **P0 safety**: `parts/value_parse.py` normalized R/C/L value parsing —
  suggestion confidence is now strict numeric equality (cross-unit spellings
  equal, dielectric conflicts downgrade, unparseable never "high"); the
  historical wrong-part incidents (5.1k -> 1k, 2.2 µF -> 22 µF, TP -> SMPS IC)
  are pinned as regressions. Structural refdes classes (TP/FID/MH/H/LOGO)
  never enter the suggestion flow. Every explicit C-number is
  **reverse-verified** against the schematic's value + package
  (`BOM_LCSC_MISMATCH`; nothing comparable = honest `unverified`).
- **P1 semantic model**: `model.Component` gains `dnp`/`in_bom`/`on_board`
  (parsed from KiCad); `bom_policy.classify()` sorts every part into
  fitted / dnp / external / no-part (Sourcing/BOM_Sourcing parameters,
  `akcli.toml [bom]` classes) across `check --bom`, `jlc bom`, CSV and
  totals. New findings: `BOM_CLASS_SUMMARY` (INFO), per-line
  `BOM_MISSING_PART_ID` (NOTE, fitted only — aggregate coverage no longer
  hides individual holes), `BOM_DNP_HAS_ORDER_ID`, `BOM_CPL_INCONSISTENT`.
  Coverage uses the fitted denominator; thresholds live in `[bom]`.
- **P2 workflow**: `--lock`/`--against-lock` BOM lockfiles (price/stock/EOL/
  id drift, exit 1); multi-board carts (`jlc bom main.kicad_sch
  aux.kicad_sch` — merged tier pricing, per-board breakdowns); `LCSC_ALT`
  second sources with automatic verified fallback + `--alternates`;
  assembly economics (Basic/Extended counts, feeder-fee estimate,
  Basic-swap advisories); `--offline` cache-only mode (misses degrade to
  `unverified`, never exit 7); `akcli diff a b --bom` per-component BOM
  delta.
- **P3 reporting**: waivers are transparent (`waived: CODE [refs] — reason`
  per silenced finding + `config_waived_detail`); `jlc bom --md` Markdown
  report; order CSV upgraded to the JLC-EDA template (No./Quantity/Comment/
  Designator/Footprint/Value/Manufacturer Part/Manufacturer/Supplier
  Part/Supplier + Note) with EVERY class kept and annotated (user decision:
  no silent filtering; consigned parts labeled; dead C-numbers still never
  leak).

### Changed
- `check --bom` on boards with structural/dnp/external parts now reports
  differently by design: no-part components are excluded (counted in
  `BOM_CLASS_SUMMARY`), dnp parts skip the sourcing checks, and param-less
  fitted parts each get a `BOM_MISSING_PART_ID` NOTE (goldens regenerated).
- `jlc bom` CSV header/columns changed to the JLC-EDA template (was the
  4-column `Comment,Designator,Footprint,LCSC Part #`).

### Fixed
- **Windows CI (the 0.10.0 release run)**: text written with `Path.write_text`
  was newline-translated on Windows (`\n` -> `\r\n`), inflating `--render`
  preview files past their reported byte count and breaking the renderer's
  cross-platform determinism promise. Every text write in `src/akcli` now pins
  `encoding="utf-8", newline="\n"` (webui state files gained the missing
  `encoding=` too), and the new AST gate `tests/test_text_io_portability.py`
  makes unpinned text I/O a CI failure on every platform — the whole
  cp1252/CRLF class (0.8.0, 0.9.0, 0.10.0 regressions) is now structural,
  not whack-a-mole. Also annotated `render_svg._render_grid` for the mypy
  beachhead (the other red job).
- **Release runbook order** (`docs/releasing.md`): push `main`, wait for CI
  green on that commit, and only then push the tag — the tag-triggered
  Release workflow does not wait for CI, so tagging together with main
  published 0.4.0/0.8.0/0.9.0/0.10.0 before `windows-latest` had ever run
  the release commit. New root `CLAUDE.md` records the portability rules,
  the CI-parity gates (ruff + mypy + pytest) and the op-vocabulary lockstep
  checklist.

## [0.10.0] - 2026-07-17

### Added — modular schematic authoring (functional groups & layout experience)
- **Functional groups**: op-list envelope `groups` (`{NAME: {origin, title?, frame?}}`) + a
  universal per-op `group` tag — grouped coordinates are GROUP-LOCAL (resolved by the new
  `ops.resolve_groups` pre-pass after macro expansion; pin anchors never translate; macros
  propagate the tag), membership persists as a hidden `Group` symbol property, new error codes
  `GROUP_UNKNOWN` / `GROUP_NO_ORIGIN` with remediation.
- **`akcli groups <sch>`** — list modules (members, world bbox, frame state) recovered from the
  sheet; **`--frame --apply`** draws a border rectangle + title per group through the standard
  draw pipeline, self-refreshing via stable annotation `key`s (re-run replaces, never
  accumulates).
- **`arrange --groups` (bare)** derives the module map from `Group` properties (a FILE still
  overrides); **`--frames`** redraws borders after packing; net-preservation unchanged.
- **Relative placement**: `place_component`/`move_component` (and `place_decoupling`/
  `place_pullup`) take `anchor` (`"REF.PIN"` or bare `"REF"`) + world-frame `offset_mil` as the
  position — resolved at execution time, so the anchor may be placed earlier in the same list.
- **`place_array` macro** — N identical parts in a row/column at a fixed pitch with per-element
  values; prefix collisions land in the standard duplicate lint.
- **`route_net` core op** — deterministic orthogonal L/Z auto-route whose corner provably avoids
  every placed pin tip (a coincident corner silently merges nets); optional mid-wire `label`.
- **Graphics/annotation ops**: `add_rectangle` (border boxes) and `add_text_box` (bordered
  multi-line notes, grammar fixture-verified against a real kicad-cli), both
  connectivity-neutral with optional stable `key` handles; `add_text` gains `key` too.
- **`set_title_block`** — edit title/date/rev/company/comment1..9 (previously write-once via
  `new --title`); find-or-create keeps the node where eeschema expects it.
- **`plan`/`draw --render OUT.svg`** — render the WOULD-BE sheet from the same temp dry-apply
  the net diff uses (look before `--apply`); non-fatal by contract; `--json` gains
  `preview: {path, bytes}`.
- **`akcli bbox <lib_id>`** — body box + full box (body ∪ pin tips) per unit for a hypothetical
  placement, via the new shared `geometry.world_box_from_extent` (autoplacement, layout lint and
  bbox can no longer drift apart).
- **`render --grid`** — world-mil gridlines + coordinate captions + origin cross;
  `--render` previews always include it.
- **Layout lints**: `LAYOUT_GROUP_OVERLAP` (WARNING), `LAYOUT_FRAME_STALE` (NOTE),
  `LAYOUT_TEXTBOX_OVER_SYMBOL` (NOTE) — all advisory, fire-and-clear tested.
- **Agent eval**: new `06-modular-blocks` task exercising groups + anchors + `route_net`
  through the real rails (reference scores 1.0). Vocabulary now **22 ops + 10 macros**.

### Fixed
- **Windows CI portability**: the PreToolUse draw guard now splits the `AKCLI` command with
  non-POSIX rules on Windows (POSIX splitting ate the backslashes in `C:\...\python.exe`,
  silently failing the guard open on every call), and `nets --json` emits its `source` path
  with forward slashes (`as_posix()`) so the golden nets snapshots are byte-stable across
  platforms; the hook tests inherit the real environment (a child Python needs `SYSTEMROOT`
  on Windows) while still pinning `AKCLI` explicitly.

## [0.9.0] - 2026-07-16

### Added
- **`--json` on every exit path (agent contract)**: a failing command that would previously
  leave stdout empty now emits a `schema_version`-stamped
  `{"error": {code, message, exit, remediation}}` on stdout under `--json` (structured
  `AkcliError`s, missing files, and usage-style `_ExitWith` failures alike); when an `_ExitWith`
  message wraps a real error code (`ERROR: TARGET_LOCKED: ...`), the envelope recovers the
  specific code + its remediation instead of the generic EXIT category, and EXIT-name
  pseudo-codes (`USAGE`, `QUERY_MISS`, ...) carry generic hints of their own.
  `plan`/`draw` structural op-list errors emit the normal draw-result shape with
  `status: "refused"` and per-op `remediation` through the same single payload builder as a
  normal run (document-level errors clamp `op_index` to 0, keeping the payload schema-valid).
  The envelope is suppressed if the handler already wrote data (never two JSON documents on
  one stdout — raw data writes route through the same tracked emit chokepoint). Human `ERROR:`
  lines keep going to stderr.
- **Remediation for every error code**: the per-op hint table moved to `errors.REMEDIATION`,
  now covering all 29 `ERROR_CODES` (readers, external tooling, bridge, config — not just
  writer ops) and asserted complete in CI; `errors.remediation_for()` is the one lookup every
  surface (op results, the `--json` error envelope) shares.
- **`schema_version` on every JSON object payload**: `verify` (both modes), `fab check`,
  `doctor`, `log`, `library audit|repair|import-altium|check-lock`, `undo`, `new`, `arrange`,
  `relink-symbols`, `render`, `jlc bom|show|add|datasheet`, `calc <name>`, `nets`, `pins`,
  the `net`/`component` query-miss payloads, `review explain|facts|diff|tree`, and the `sim`
  family now stamp `schema_version` (or already carry a family version field); the
  `capabilities` `conventions.version_stamps` claim was reworded to the exact guarantee (array
  payloads and name-keyed tables are the documented exceptions) and is enforced twice in CI:
  behaviorally (`tests/test_json_version_stamps.py`) and mechanically (an AST scan failing any
  new `_dumps({...})` object payload that neither carries a `*_version` key nor routes through
  `_stamp()`).
- **`read --match GLOB` / `--limit N`**: graduated context-budget throttling on the full
  export (components / library symbols / PCB footprints), applied to the model before any
  renderer runs so text, `--md` and `--json` honor the flags identically; `--json` carries the
  same `total/matched/returned/truncated` envelope as `nets` under a `"listing"` key — the
  in-between of full `read` and the all-or-nothing `read --summary`. Flagless output is
  byte-identical to before.
- **Published op-vocabulary constraints**: `capabilities --json` `ops.constraints` now states
  the hard limits up front (`rotation_enum` 0/90/180/270, `wire_orthogonal_only`,
  `grid_mil` 50, `hierarchy: flat_v1_only` — ops cannot target a child sheet) so an agent can
  branch before attempting an op instead of learning each rule from a failed `draw`; the values
  are read from `ops.py`'s validator constants and `ops.capabilities.json` (single sources of
  truth), never hardcoded copies.
- **Altium-live honesty flag**: `capabilities --json` and `ops.capabilities.json` gain
  `altium_live_wired: false` — the per-op `"altium"` support matrix describes the experimental
  live-bridge executor, which has **no CLI wiring** (`plan`/`draw` always build
  `target_format: "kicad"`); an agent must not try to select an Altium write target.
- **Review-rule calibration baseline in CI**: `tools/corpus_replay.py` now has a committed
  baseline (`tests/golden/corpus_replay_baseline.json`) replayed on every CI run
  (`tests/test_corpus_replay.py`) — a detector change that shifts corpus findings leaves an
  auditable baseline diff in the PR, operationalizing the "replay → measure → then allowlist"
  promotion path for `release preflight --review-policy`.
- **Agent-loop eval harness** (`tools/agent_eval/`): five natural-language design tasks with
  committed ground-truth netlists and reference op-lists; `run_eval.py` scores any agent's
  op-list (validate → `draw --apply --strict-nets` → named-net membership match) via
  `--agent-cmd`/`--ops-dir`, and `--reference` self-checks the harness. CI pins the harness to
  real CLI behavior by asserting every reference solution scores 100 %
  (`tests/test_agent_eval.py`) — the first automated signal for "does an agent following the
  skills actually succeed".

- **`akcli capabilities`** — the self-describing surface manifest: every subcommand + flag
  (introspected from the live `argparse` parser, so it cannot drift), the frozen exit/error-code
  tables, the op-list vocabulary with per-executor support, the calculator registry, the packaged
  JSON Schemas with version fields, and the tool conventions — one `--json` document an agent
  reads to drive the tool blind.
- **Exit code `8` (`QUERY_MISS`)**: `akcli net <file> NAME` and
  `akcli component <file> REF` misses are now machine-detectable — stderr gains a
  did-you-mean hint, `--json` emits `{"found": false, "query": ..., "kind": ..., "source": ...}`
  on stdout, and the exit code distinguishes "entity absent" (8) from "file absent" (4).
- **Output throttling (the context-budget contract)**: `read --summary` prints counts +
  metadata only (never the full object arrays); `nets`/`component` gain `--match GLOB` and
  `--limit N`, with a `total`/`matched`/`returned`/`truncated` envelope in `--json` and a stderr
  `note:` in text mode so a cut listing can never be mistaken for the whole document.
  `component` without `REF` now lists all components (previously a usage error, despite the
  documented behavior).
- **Workspace write journal + `akcli log`**: every write-path command (`plan`/`draw`/
  `arrange`/`undo`/`relink-symbols`) appends a JSONL entry to `<dir>/.akcli/journal.jsonl`
  (timestamp, command, status, op-list sha256, net-diff verdict, backup name); `akcli log`
  reads it back with `--limit`/`--cmd`/per-file filters. Stateless CLI, stateful workspace —
  journaling never fails the parent command, and `AKCLI_JOURNAL=off` disables it.
- **`akcli ops validate` + a PreToolUse draw guard**: target-free structural op-list
  validation as its own subcommand (exit 6 + full error list, `--json` envelope with
  `ops_sha256`), and a plugin hook that runs it before any `draw --apply` — an invalid
  op-list is blocked at the harness layer, and a missing prior `plan` (checked against the
  workspace journal) warns. Fail-open by design: every CLI-side gate still stands.
- **Every JSON payload family now has a published schema**: `diff.schema.json`,
  `pinmap.schema.json` and `draw-result.schema.json` join the existing seven; `plan`/`draw
  --json` payloads are now `schema_version`-stamped. All schemas share one `$id` host, the
  packaged mirror now carries **all** canonical schemas byte-identically (previously
  `netlist`/`schematic` were missing from the wheel), and a CI gate validates the live
  `diff`/`pinmap`/`plan`/`draw` outputs against them.
- **Structured remediation on op failures**: every failed op in `plan`/`draw` output now
  carries a machine-readable `remediation` field (and a `hint:` line in text mode) telling
  the agent what to *do* — e.g. `SYMBOL_NOT_FOUND` points at `--symbols`, `OFF_GRID` at the
  50-mil snap rule, `OP_UNSUPPORTED` at `akcli ops list`/`ops validate`.
- **`akcli render` — install-free SVG schematic rendering** (pulled forward from the v0.10
  roadmap): pure-stdlib, format-agnostic (Altium `.SchDoc` renders too), deterministic, and
  connectivity-true — wires/buses/junctions/labels/No-ERC marks plus synthesized component
  bodies with refdes/value; hierarchical designs render one titled block per sheet. The visual
  feedback channel for a multimodal agent after `draw --apply`.
- **Differential-pair / bus continuity checks** (`check --pairs`, in the default family set):
  `PAIR_INCOMPLETE` (a `_P`/`_DP`/`_H`/`+` net without its partner — asymmetric by design,
  lone `_N`/`_L` active-low names never fire), `PAIR_PIN_MISMATCH`, and `BUS_GAP`
  (internal index holes in `D0..D7`-style families). Configurable via `[check]`
  (`pairs`/`pair_suffixes`/`bus_min_family`).
- **`draw --no-erc`**: honestly skip the advisory post-apply `kicad-cli` ERC run (logged at
  `-v`); akcli's own connectivity gate always runs.
- **ERC pin-type conflict matrix**: `ERC_PIN_CONFLICT` covers the high-signal cells of
  KiCad's default pin matrix (open-collector / open-emitter / tri-state vs push-pull and
  power drivers), and `ERC_POWER_IN_UNDRIVEN` flags a supply pin on a net that is neither a
  name-recognized rail nor driven by any `power_out` pin. Both type-confidence-gated
  (demoted to NOTE on mostly-Passive imports) with waiver tokens
  `pin_conflict`/`power_in_undriven`.
- **Golden-file regression corpus** (`tests/golden/` + `tools/golden_regen.py`): frozen
  `nets`/`check`/`diff`/`review analyze`/`render` outputs over the committed fixture boards,
  byte-compared in CI — any behavior drift fails with a readable diff and is either a caught
  regression or a deliberate, reviewed regeneration.
- **Real-board corpus** (`tests/fixtures/corpus/`): `analog_frontend` — a 13-component,
  8-net analog front-end (π-filter, decoupling, I²C pull-ups, reference + sensor dividers,
  RC anti-alias) **authored by akcli itself** from a committed op-list (a reproducibility
  test re-derives it), with its honest findings frozen into the golden corpus.
- **`akcli review testbench` — auto-generated subcircuit SPICE testbenches** (the M7
  backlog item): a quantitative review finding (an RC corner, a divider ratio) becomes a
  runnable cone testbench — the finding's components are cut out of the schematic, stimuli
  and pass/fail bounds are synthesized from its calc evidence (**recomputed from the live
  schematic, never copied** — a stale findings file cannot vacuously pass), and ngspice
  delivers the verdict. Generators: `REVIEW_RC_CUTOFF` (AC −3 dB crossing, ±8 %) and the
  divider family (tap voltage, ±2 %); non-rederivable topologies are skipped with a
  reason. `--findings` reuses a saved envelope, `--deck-only --out` emits engine-free
  decks, lint-style exit. On the corpus board, ngspice confirms the detector's 15.92 kHz
  claim at 15.88 kHz measured.
- **Sim deepening**: the builtin behavioral library grows `AKCLI_OPAMP` (single-pole,
  100 dB / GBW ≈ 1 MHz, soft rail clamp) and `AKCLI_NMOS_SW`/`AKCLI_PMOS_SW` (smooth
  Ron/Roff switches, `vth`/`ron` params) — engine-validated in CI (a unity-gain buffer must
  track; the switch must switch); two new deck diagnostics, `SIM_ZERO_PASSIVE` (a 0-valued
  R/C/L is a solver trap) and `SIM_STIMULUS_SHORTED` (both source terminals on one node).
- **`/circuit-parts` slash command**: `jlc search → show → add → plan → draw` as one
  documented, stage-gated flow (networked + writes files; user-triggered only).
- **Frozen live-bridge error codes**: `BRIDGE_BUSY` (exit 6, a held single-flight lock) and
  `BRIDGE_TIMEOUT` (exit 7, no response within the deadline) join the error registry;
  `BridgeBusy`/`BridgeTimeout` stay `RuntimeError`/`TimeoutError` subclasses for existing callers.

### Fixed
- **`commands/circuit-draw.md` op-list drift**: the slash command claimed "16 ops" and omitted
  `add_sheet`/`rename_net`; corrected to the real 18-op vocabulary, and `commands/*.md` is now
  scanned by the docs-conformance gate (fence + count) so plugin command prose can no longer
  drift silently.
- **`jlc bom --fix` now records to the workspace journal** (`jlc-bom-fix`, op count, backup
  name) like every other write path — `akcli log` is no longer blind to BOM auto-fix writes.
- **`akcli-parts/` run output untracked**: an accidentally committed `jlc add` output directory
  was removed from version control, and `akcli-parts/`/`fpout/` are gitignored.

### Changed
- **mypy beachhead widened** to the typed core: `model`, `errors`, `units`, `safety`,
  `netdiff`, `journal`, `render_svg`, `kicad_escape` join `parts/` and `calc/` under
  `disallow_untyped_defs` in CI.
- **One lint-style exit policy**: `--fail-on {info,note,warning,error,never}` is now accepted
  by every findings-emitting command (`check`, `diff`, `pinmap`, `library audit`, `fab check`),
  with `--exit-zero` kept everywhere as the deprecated alias — an agent learns the flag once.
- `akcli ops list --json` now reports per-op Altium support under `"altium"` (matching
  `ops.capabilities.json`'s executor key); `"altium_live"` remains as a deprecated duplicate
  for one release.
- `akcli export --json` now wraps the rendered netlist in a
  `{schema_version, source, format, content}` envelope instead of refusing with exit 2
  (the `content` string is byte-identical to the plain output).
- The advisory post-apply `kicad-cli` ERC no longer swallows every failure silently; a broken
  integration surfaces as a `note:` line on stderr (still never fatal).

## [0.8.0] - 2026-07-15

### Added
- **`akcli review` — engineering design review (review track M1–M3)**.
  Detectors run on the normalized model (per-rule specification and
  literature citations in `docs/review-rules.md`), so every rule reviews
  KiCad `.kicad_sch` **and** Altium `.SchDoc` inputs alike:
  - `review analyze <sch> [--pcb] [--profile fast|standard|deep]
    [--detector NAME] [--out FILE] [--fail-on SEV]` — **advisory by
    default** (exit 0 whatever it finds; `--fail-on` opts a CI job into
    gating). Config `[[waiver]]` entries apply as they do to `check`.
  - `review report <findings.json> --format text|json|sarif|junit|markdown`
    — re-render a findings file; `markdown` is a new render format with a
    per-confidence trust rollup.
  - `review explain <CODE>` — the rule's spec: check, formula, confidence,
    upstream provenance.
  - Signal family (M2): feedback/plain divider review (implied Vref
    plausibility, tap-name mismatch, unvalued-resistor
    insufficient-evidence), RC low-pass corner (via `calc rc`, envelope +
    citation carried in evidence), crystal load caps (missing / asymmetric /
    computed CL with stated C_stray assumption), connector ESD/TVS coverage,
    op-amp gain topology (non-inverting / inverting / unity buffer /
    open-loop warning).
  - Validation family (M3): I²C pull-up window via `calc i2c-pullup`
    (missing / below R_min / above R_max-with-stated-C_b / SDA-SCL
    mismatch), cross-voltage-domain signals (level-shifter aware), floating
    enable/shutdown pins.
  - PCB family (M5, runs with `--pcb`, `standard` profile): copper-geometry
    engine (per-net union-find over pads/tracks/vias with layer awareness;
    zones merge by bbox — over-merge only, so unrouted verdicts stay true
    positives) powering `REVIEW_PCB_UNROUTED`; decoupling-distance review
    (`REVIEW_DECAP_DISTANCE`, measured mm in evidence); exposed-pad thermal
    vias (`REVIEW_THERMAL_VIA`); junction-temperature estimate
    (`REVIEW_THERMAL_JUNCTION` — facts `theta_ja`/`power_dissipation`/
    `t_j_max` → datasheet_backed, typical-package θ_JA table fallback →
    heuristic, no recorded dissipation → no estimate); power-rail trace
    ampacity (`REVIEW_TRACE_WIDTH`, IPC-2221 with the `calc trackwidth`
    envelope as round-trip oracle). Without `--pcb` the family lands in
    `detectors_skipped`, never runs vacuously.
  - Agent surface for the review track: three new plugin skills —
    `akcli-datasheet-facts` (the agent-side extraction loop: read the PDF,
    `review facts add --method llm` with page+quote, `verify` as the honesty
    gate), `akcli-deep-review` (candidate-generation discipline for
    `review validate`: anchor everything, own namespace, store-backed
    datasheet claims, honest quarantine reporting), `akcli-release-gating`
    (preflight manifest doctrine + review-policy calibration governance +
    gerber staleness gate). Upgraded: `akcli-schematic-review` (engine-first
    protocol, confidence doctrine, `review tree`/`diff` integration),
    `akcli-schematic-authoring` (proposal-adoption loop, group re-layout,
    check-lock), `akcli-setup` (pdftotext capability row),
    `akcli-parts-sourcing` (datasheet→facts chain), and the
    `/circuit-review` slash command (review engine first). Skill count
    9 → 12.
  - Gerber package review (M9): `readers/gerber.py` — fab-output directory
    reader (RS-274X with X2 FileFunction role detection + filename fallback,
    units/format/extents; Excellon tools/holes/extents; ambiguous
    implied-decimal coordinates yield a warning and no bbox, never a guess).
    `review analyze --gerbers DIR` + `release preflight --gerbers DIR` run
    the package checks: minimum fab set, copper count vs the board stackup,
    bbox registration, mixed units, and **staleness** (outline gerber vs the
    board file's Edge.Cuts extent — catches ordering an outdated export).
  - Deep-review gate + blocking policy (M8): `review validate` — four
    deterministic gates over LLM candidates (schema / anchor existence /
    datasheet sha256+page+quote / deterministic-rule masquerade), failures
    quarantined with reasons, accepted candidates stamped `llm_reviewed`
    observations that can never block. `release preflight --review-policy`
    — the ONLY path by which review findings gate a release: an explicit
    `[review] allow = [codes]` allowlist (policy sha256 + list recorded in
    the manifest); everything unlisted stays advisory. First domain-family
    detector: USB-C CC termination (`REVIEW_USB_CC_MISSING`/`_VALUE`,
    Rd = 5.1 kΩ; controller-handled CC nets skipped).
    `tools/corpus_replay.py` (dev-only): corpus snapshot/drift harness —
    the calibration step before allowlisting a rule.
  - Closed loop (M7): `review propose` — findings → declarative candidate
    changes (`schemas/proposals.schema.json`, wheel-mirrored): value fixes
    recomputed + E-series-snapped via `calc eseries` into protocol-1 op-list
    drafts (run through `plan`/`draw --apply`), contract drafts carrying the
    datasheet sha256+page into `evidence`, sim-assertion drafts; the
    schema itself enforces "open `requires_confirmation` ⇒ no op-list
    draft", and PCB fixes are `layout` proposals (akcli writes schematics
    only). `review diff` — fingerprint-aligned drift between two findings
    files (added/resolved/changed/persisting, `--fail-on-new`).
    `review tree` — per-rail power structure (regulator via its feedback
    divider, consumers, decoupling count).
  - EMC family (M6, `--pcb` + `deep` profile): eight pre-compliance rules
    in three batches — ground-pour presence/coverage, ground-via stitching
    (λ/20 at a stated 1 GHz assumption), board-edge + clock-edge routing
    (Edge.Cuts bbox; no outline → silent, never a pass), differential-pair
    intra-pair skew (25 ps budget, short side named in `fix_params`),
    TVS-to-connector clamp distance, adjacent-signal-layer stackup. The
    report metadata gains an advisory `emc` block (severity-weighted
    `risk_score`, `probe_points`, and the standing "risk analysis, not a
    compliance verdict" note) whenever the family runs — a quiet board
    scores 0 with the block present.
  - Datasheet facts store (M4): `review facts add|verify|lookup` — one
    audited JSON per MPN under `datasheets/extracted/`, every fact pinned to
    its source PDF by sha256 + page (+ optional verbatim quote);
    `schemas/datasheet-facts.schema.json` shipped + wheel-mirrored. `verify`
    audits schema / PDF presence / sha256 staleness / quote presence (via a
    new optional `pdftotext` driver; absent tool → NOTE, never a silent
    skip). `review analyze --facts DIR` (auto-discovers
    `<sch dir>/datasheets`) upgrades detectors to `datasheet_backed`:
    feedback-divider Vref mismatch (`REVIEW_FB_DIVIDER_VREF_MISMATCH`),
    crystal CL mismatch with suggested cap value
    (`REVIEW_XTAL_LOAD_MISMATCH`), and voltage-domain adjudication
    (`abs_max_io` proves a pin tolerant → INFO, or confirms the violation).
  - BOM (M3): `BOM_MPN_COVERAGE` in `check --bom` — MPN/distributor-field
    sourcing coverage below 50 % on a ≥10-part sheet, with the
    `_PART_ID_PARAMS` table extended to common distributor field
    conventions (DigiKey/Mouser/LCSC/JLCPCB/Farnell).
  - Engine guarantees: per-detector containment (`REVIEW_DETECTOR_ERROR`,
    quarantined), deterministic ordering, every finding stamped with
    detector + wording-immune fingerprint; metadata always reports
    detectors run/skipped and a trust summary.
- **Findings evidence envelope + published schema** (closes the ROADMAP v0.8
  "findings.schema.json" item). `Finding` gains optional
  `detector` / `confidence` (deterministic | heuristic | datasheet_backed |
  llm_reviewed) / `evidence` / `rule_version` / `fingerprint` /
  `remediation` / `fix_params` / `status` — serialized only when set, so
  pre-review findings keep their historical JSON shape byte-for-byte.
  `schemas/findings.schema.json` (mirrored into the wheel) validates every
  `check`/`review` JSON report and structurally enforces
  "`datasheet_backed` ⇒ `evidence.datasheet.{sha256,page}`". SARIF gains a
  wording-immune `akcliFinding/v2` fingerprint alongside v1.
- **Rigid, net-preserving re-layout** — the atomic operation a functional-block
  re-layout needs:
  - `move_component` gains optional `carry_labels` / `carry_wires` (booleans,
    default false — no `protocol_version` bump). A moved part now takes the net
    labels (and wire endpoints) anchored on its pins along with it, by the same
    delta. With the label-on-pin connectivity pattern this makes the move
    **provably net-preserving** — every pin keeps the label that names its net —
    instead of silently stranding labels at the old coordinates.
  - `akcli arrange --groups groups.toml` relocates whole functional blocks into
    their own shelf-packed regions (wide channel between groups), moving each
    part plus the power symbols riding on its pins as a rigid bundle via carried
    `move_component` ops. TOML or JSON `group-name → [refdes, …]`; `--group-gap`
    / `--row-width` tune spacing. Goes through the standard draw pipeline
    (`.bak` + connectivity re-verify + `undo`) and **refuses to write on any net
    change**. Documented in `docs/kicad-format-gotchas.md`.
- **`akcli library check-lock [project]`** — reports which KiCad files are open
  in the GUI (`~<name>.lck`) and exits 6 (`TARGET_LOCKED`) if any are locked, so
  external flows (hand scripts, `sed`) can gate on the same guard akcli's own
  writes use: `akcli library check-lock . && ./relayout.sh`.
- **KiCad format gotchas doc** — `docs/kicad-format-gotchas.md` consolidates the
  rules any layout/edit op must obey: `{…}` name escaping, absolute property
  `(at)` coordinates, the nested `(type "Table")` global lib-table, the
  rigid-move ops, the "KiCad open ⇒ external writes unsafe" caveat, and the
  3D-model path-policy trade-offs.
- **Altium `.PcbLib` reading** — `akcli read part.PcbLib` decodes footprint
  storages into the new `FootprintDef`/`FootprintPad` library model (pads with
  position/size/drill/shape/rotation, NPTH vs plated, per-type
  `UNSUPPORTED_PRIMITIVE` warnings for undecoded graphics). Pad geometry is
  cross-validated against KiCad's own Altium importer (43/43 pads on a real
  vendor module, zero mismatches). `.kicad_mod` and `.pretty` read into the
  same model.
- **`akcli library` namespace** — the project library workspace as a
  first-class object:
  - `library audit` cross-checks schematics ↔ `sym-lib-table`/`fp-lib-table`
    ↔ library contents ↔ 3D models: unregistered footprint nicknames (the
    "cannot find footprint" trap), missing footprints/symbols, unresolvable
    table URIs, missing/unportable 3D model paths, and legacy pre-v6
    footprints that parse via API but are invisible to the KiCad GUI.
  - `library repair` productizes the two historically hand-`sed`-ed fixes as
    reviewable plans: `--rename-footprint-lib OLD=NEW` (Footprint-field
    nickname rewrite via the lossless S-expression parser) and
    `--3d-path absolute|'${VAR}'`; dry-run by default, `--apply` writes
    atomically with `.bak` and re-audits.
  - `library import-altium part.PcbLib` converts to a `.pretty` library —
    pads verbatim (never recomputed), optional declared `--courtyard`,
    filesystem-safe renames reported, and a `provenance.json` recording the
    source SHA-256, converter version, options and every warning.
- **Deep `.kicad_pcb` reading (schema 1.2)** — pad-level net bindings (both
  `(net N "name")` and KiCad 10 `(net "name")` dialects), absolute pad
  positions (rotation validated against `pcbnew` on a real 4-layer board),
  tracks, vias (through/blind/micro), zones, board setup (copper layers,
  thickness, setup values) and the Edge.Cuts outline bbox.
- **Schematic ↔ PCB equivalence** — `akcli verify sch.kicad_sch
  board.kicad_pcb` compares refdes presence, footprint/value assignment and
  the pad-level net PARTITION (net names untrusted): `SCHPCB_NET_SPLIT`,
  `SCHPCB_NET_MERGE`, `SCHPCB_PAD_MISSING` and friends, each located to the
  designator/pad. `#PWR`/`#FLG` pseudo-components are excluded.
- **Design contracts** — `akcli check --contract contract.toml` asserts
  datasheet-backed topology rules ERC cannot express: require/forbid pin-net,
  require/forbid same-net pin pairs, component values, NC pins — plus
  approved exceptions with owner/reason/expiry. PASS, FAIL and
  SKIPPED-BY-EXCEPTION are all explicit findings; an expired exception warns
  instead of silently passing.
- **Fab profiles** — `akcli fab check board.kicad_pcb --profile p.toml`
  checks a versioned, evidence-carrying vendor policy (TOML with mandatory
  `[source]` urls): free-via geometry boundaries, tenting drill cap,
  via-in-pad (with registered `thermal_via` exceptions), blind/buried bans,
  stackup drift, and cost thresholds (board size/area, drill density, fine
  traces). `--order order.toml` validates the declared purchase intent
  (delivery format, finish, via covering, …) — never guessed from the PCB.
  `fab explain CODE` prints the rule, fix direction and profile sources.
- **Release gate** — `akcli release preflight --sch … [--pcb --contract
  --fab-profile --order --intent]` runs every applicable gate
  (check/intent/contract/library-audit/sch-pcb/fab/order/git-clean), skips
  explicitly with reasons, and writes a manifest binding input SHA-256s,
  tool version, git revision/dirtiness and per-gate findings (`--out`).
- **Fail-loud format detection** — `.PcbLib` is detected by extension; a
  bare OLE2 container is classified by its storage layout (`Board6` → PcbDoc,
  `Library` → PcbLib, `FileHeader` sniff for SchDoc/SchLib) instead of being
  assumed a schematic; an unknown layout exits 5. `read` stamps
  `detected_format`/`detection_method`/`object_counts` into the JSON
  metadata, and a non-empty source normalizing to nothing raises an
  `EMPTY_IMPORT` warning (`read --strict` makes it exit 1).
- **GUI-open write guard** — `draw/arrange/undo --apply` refuse with
  `TARGET_LOCKED` (exit 6) while KiCad's `~<name>.lck` is present;
  `--allow-open` is explicit risk acceptance and successful applies under an
  open GUI print a File>Revert reminder.
- **`jlc add` library integration** — `--footprint-lib NICKNAME` controls
  both the output directory and the nickname written into the symbol's
  Footprint field (previously hardcoded `footprint:` — the #1 "cannot find
  footprint" cause); `--3d-path relative|absolute|'${VAR}'` sets the 3D
  model path policy, with the portability/usability trade-off stated on
  stderr.

### Changed
- `schema_version` 1.2 → **1.3** (additive): findings gain the optional
  review evidence-envelope fields above; consumers that ignore unknown keys
  are unaffected.
- **Package version → `0.8.0`** and `akcli --version` now reports the
  version of the code actually running. A co-located `pyproject.toml`
  (`project.name == "akcli"`, i.e. a source checkout) is authoritative over the
  installed dist metadata, so bumping the working tree always moves
  `--version` — an in-development build no longer masquerades as the last
  release (the `0.7.0`/design-integrity overlap). An installed wheel is
  unaffected (no sibling `pyproject.toml`).
- `schema_version` 1.1 → **1.2** (additive): `Pcb` gains
  `zones`/`board`/`warnings`/`metadata`, `Library` gains
  `footprints`/`warnings`/`metadata`, new `FootprintDef`/`FootprintPad`.
- New frozen error code `TARGET_LOCKED` (exit 6).

## [0.7.0] - 2026-07-14

### Changed
- **Renamed the project to `akcli`** — the distribution, the Python import
  package (`altium_kicad_cli` → `akcli`), and the Claude Code / Codex plugin
  name. The `akcli` CLI command is unchanged. Positioning is now stated
  honestly: an **AI-native KiCad design agent** (KiCad is the writable target;
  Altium `.SchDoc`/`.SchLib`/`.PcbDoc` are read/imported into the same model,
  with an optional Windows live bridge to a running Altium instance) — not a
  symmetric Altium↔KiCad converter. The config file is now `akcli.toml`; the
  legacy `altium-kicad-cli.toml` is still read as a fallback. Import sites must
  update `import altium_kicad_cli` → `import akcli`. Plugin/marketplace name and
  the `/<plugin>:` command namespace are now `akcli` (install with
  `akcli@akcli`).
- **ROADMAP.md rewritten around the repositioning** — re-anchored from the stale
  "v0.1.0" snapshot to v0.6.0 reality: an honest shipped-history table (the
  planned "v0.5 Altium alive" arc was displaced by KiCad-native depth), ~8
  silently-completed items acknowledged (net-diff delta, waivers, fuzzing,
  kicad-cli parity, BOM pricing, SPICE→full sim, hierarchical write, watch→view),
  forward milestones re-planned (v0.7 agent-contract completeness → v1.0), the
  Altium-interop items demoted to a demand-driven optional track, and the
  PyPI/MCP deferrals recorded as decisions. README roadmap sections (en/zh-Hant/
  zh-Hans) synced.

### Added
- **`akcli doctor`** — one-shot environment report (python / akcli install /
  packaged schemas / kicad-cli / ngspice / config, opt-in `--network` probe of
  the jlc endpoint), each probed exactly the way the features discover them,
  with a remediation hint per missing item. `--require kicad-cli,ngspice,...`
  turns it into a CI gate (exit 1 on a missing capability).
- **`akcli-setup` skill** (9 skills now) — probe-first environment setup and
  repair: drives `akcli doctor`, per-OS remediation for kicad-cli/libngspice/
  network/config, PEP 668 guidance, and a failure-mode quick table.
- **JLCPCB manufacturing-handoff docs** — `docs/jlc.md` and the
  `akcli-jlcpcb-capabilities` skill now cover producing all four order
  artifacts from KiCad: headless `kicad-cli` Gerber/drill/CPL exports (flags
  verified on KiCad 10) + `akcli jlc bom --csv` for the BOM, the CPL header
  renames JLCPCB expects, and links to JLCPCB's own KiCad 8 GUI guides.

### Changed
- **README (en/zh-Hant/zh-Hans) restructured KiCad-first** — the body now leads
  with authoring (op-list write → checks → sim → parts → calculators), reading
  moves late, and "Read Altium files" became **"Import Altium designs"** with
  the read-only wording clarified (file access read-only; the optional live
  bridge drives a running instance). Highlights re-led with the agent-defining
  bullets (AI-agent native, net-diff rails, sim).
- **One kicad-cli discovery ladder** — `drivers.kicad_cli.find()` (KICAD_CLI
  env → PATH → known bundle/install locations, Windows versions numerically
  sorted) now backs the advisory ERC, the `view live` exporter, and
  `akcli doctor`. Real fix: advisory ERC after `draw --apply` previously
  probed PATH only and silently skipped on bundle-only installs (macOS).
- **docs-conformance gate widened** to `INSTALL.md` and `ROADMAP.md` (it
  immediately caught a stale historical calculator count); `INSTALL.md`'s
  verify section now leads with `akcli doctor`.
- **Every skill now carries the `akcli-` prefix** (`akcli-circuit-design`,
  `akcli-design-calc`, ... `akcli-setup`) so loose-folder installs group
  together and users can discover them by typing `akcli` — matching the
  plugin's `/akcli:` command namespace.

### Fixed
- **`sim/builtin.lib` is now shipped in the wheel** — it is read at runtime via
  `Path(__file__)`, but `package-data` declared only `webui/*.html` and
  `schemas/*.json`, so a non-editable (pip) install had no builtin SPICE models
  and `akcli sim` with a builtin comparator/555/phototransistor failed. Added
  `akcli.sim = ["*.lib"]` (and the vendored JLC2KiCadLib `LICENSE`/`PROVENANCE`
  for compliance); a fresh-wheel install now loads all three builtin subckts. A
  packaging test asserts every `Path(__file__)`-loaded resource is declared.
  (Pre-existing gap, surfaced by fresh-wheel verification during the rename.)

## [0.6.0] - 2026-07-13

### Added
- **`akcli sim <sch>` — simulate a schematic with libngspice and assert on the results.** The
  schematic is rendered to a SPICE deck (net → node mapping, component → device via a first-hit-wins
  model-resolution ladder of `Sim.*` KiCad-native fields → `sim.json` `models` overrides → prefix
  heuristic), run through libngspice **in an isolated child subprocess** (crash- and timeout-safe),
  and its `.meas` results are compared against pass/fail bounds (`gt`/`lt`/`ge`/`le`/`approx`+`tol`,
  engineering notation accepted) declared in a `sim.json` spec. `--deck-only` emits the deck and
  exits `0` without any engine (engine-free plan/review mode); `--wave OUT.csv` dumps simulated
  vectors; `--gnd NET` names SPICE node `0`; `--timeout`, `--exit-zero`, and `--json` round out the
  flags. Findings: `SIM_ASSERT_FAIL`/`SIM_MEAS_FAILED` (ERROR), `SIM_UNMODELED`/`SIM_DANGLING_PIN`/
  `SIM_BAD_STIMULUS` (WARNING). The engine is discovered via `AKCLI_NGSPICE` (a path, or `off`) →
  macOS KiCad bundle → `find_library` → Linux sonames → Windows KiCad; a missing engine exits `7`
  (`NGSPICE_MISSING`). `sim.json` is validated by `schemas/sim.schema.json`. The deck builder
  centralizes SPICE's `M`-means-milli / mega-is-`MEG` value fix and the title-line and B-source
  whitespace gotchas. Includes `sim.models.fit_diode()` for turning datasheet forward-voltage points
  into a `.model` line (table-anchored single-point fit preferred over eyeballed curve fits). See
  [docs/sim.md](docs/sim.md).
- **`akcli sim fit-diode` — datasheet → SPICE model, on the command line.** Fit a diode `.model`
  from `--point V@I` datasheet forward-voltage points (`--rs-point` for series resistance, `--cjo`
  for junction capacitance, `--n-prior`/`--name`); prints the `.model` card + `Sim.Params` (or
  `--json`). `--apply SCH --designator REF` plans a native `set_component_parameters` write that
  stamps `Sim.Device`/`Sim.Params` onto that component — a **dry run** printing the op-list unless
  `--write` (which commits through the KiCad writer with a rotated `.bak`). Closes the datasheet →
  model loop with `jlc datasheet` (+ `jlc datasheet --resolve-mpn`, below).
- **`akcli sim --sweep NAME=v1,v2,...` — corner matrices.** Re-runs the assert pass across a
  repeatable Cartesian corner matrix (component-value override `R21=2.2k,3.3k` on a schematic deep
  copy, or `temp=0,25,60` injected as `.option temp=`); ≤64 corners, engine-only. Prints a
  per-corner verdict table (or `{corners:[...]}` JSON) and exits `1` if any corner fails.
- **Two-sided asserts** — a `sim.json` assert entry may now carry both a lower (`gt`/`ge`) **and** an
  upper (`lt`/`le`) bound in one entry (e.g. `{"ge":"3.0","le":"3.6"}` for a rail window); the
  violated side is named on failure. `approx` stays exclusive.
- **`--wave OUT.csv` now writes a tidy CSV** — a single `time` column plus one verbatim column per
  `options.wave_vectors` entry (ngspice's `wrdata` repeats the scale column in front of every vector;
  `sim/wave.rewrite_wrdata()` collapses it). Paths with spaces are handled.
- **Sim floating-node hardening.** The deck builder detects a net with no DC path to ground
  (`SIM_FLOATING_NODE`, naming the stranding parts) and, under the default `options.rshunt` policy
  (absent/`"auto"`), auto-appends `.option rshunt=1e12` to keep the DC operating point solvable
  (`SIM_RSHUNT_ADDED` NOTE); `false` never emits it, a number/string always does. A power-named net
  (`+*`/`VCC*`/`VDD*`/`VBAT*`/`VSUP*`) with no voltage-source drive warns `SIM_UNDRIVEN_RAIL`.
- **`akcli jlc datasheet --resolve-mpn`** (schematic target) — MPN-only BOM lines get an exact-match
  catalog lookup (same policy as `jlc bom`) to pin a C-number before the EasyEDA resolve; misses stay
  `not-found` with a nearest-MPN note, network errors exit `7`.
- **Anti-drift docs gate** (`tests/test_docs_conformance.py`) — every ` ```-fenced `akcli …` line in
  the docs is validated against a live `build_parser()` (unknown subcommand or flag fails the test),
  and the `N ops` / `N macros` / `N calculators` counts (plus their zh-Hans/zh-Hant forms) are
  asserted equal to the live registries. A ` # doc-noqa` comment opts a genuinely non-command fenced
  line out.

### Changed
- **`akcli jlc datasheet` output now cross-links the datasheet → model loop** and its structured
  positions coverage was extended: the `intent`, `bom`, and `libsync` checkers now attach structured
  `pos`/`anchors` to their findings (SARIF fingerprints stay byte-stable for genuinely positionless
  cases).
- **CI: mypy beachhead extended to `src/akcli/calc/`** (added to `[tool.mypy] files` in
  `pyproject.toml` — enforced by the existing bare-`mypy` CI step) alongside `parts/`.
- **CI: GitHub Actions bumped** to current major versions (`actions/checkout` v7, `setup-python` v6,
  `setup-node` v6, `upload-artifact` v7, `download-artifact` v8, `softprops/action-gh-release` v3),
  and the Linux `kicad-cli` job now installs libngspice and **fails** if any libngspice-gated live sim
  test is skipped (no silent skips).
- **`bus_alias` is documented as an arbitrated no-op** — ground-truth tested against `kicad-cli`
  10.0.4, a `(bus_alias ...)` declaration is ignored for netlisting (an alias-labeled bus is
  member-less), so `akcli` matches it by design.

### Fixed
- **docs/sim.md gained a floating-node troubleshooting section** — skipping an IC can strand cap-only nets (555 CV, regulator VIN), the DC operating point goes singular, and the transient starts unconverged; the documented `options.extra_cards: [".option rshunt=1e12"]` remedy was live-validated (spiro rev C channel matches its reference sim to 6 significant figures).
- **`akcli sim` adversarial-review round.** (1) **Diode/BJT polarity** — the deck builder now derives
  a diode's/transistor's SPICE terminal order from pin **names** (`A`/`K`, `C`/`B`/`E`), not pin
  numbers; KiCad numbers stock diode pins `K`=1/`A`=2, so the old pin-number order silently reversed
  polarity (a peak/envelope detector charged the wrong way). Ambiguous names now emit a
  `SIM_PIN_ORDER_ASSUMED` warning. (2) A net literally named `N1` is no longer merged with a generated
  unnamed-net node — the generator skips reserved tokens (`SIM_NODE_COLLISION` on a true clash).
  (3) A `spec.models` entry with a missing/unknown `device` infers the element letter from its
  `model_card`/subckt (or fails `BAD_CONFIG`) instead of emitting a bare designator that ngspice
  mis-parses. (4) A deck ngspice cannot parse is now an engine failure (exit `7` with the parse error
  visible) instead of a false clean exit, even for a zero-assertion spec. (5) `engine.run` resolves a
  relative `workdir` before spawning the child. (6) A stimulus `node` matching no net warns
  `SIM_UNKNOWN_STIMULUS_NODE` with close-match suggestions. (7) Stimulus `name` is now required,
  identifier-shaped (`^[A-Za-z][A-Za-z0-9_]*$`), and unique. (8) `fit_diode` validates/clamps its
  single-point `n_prior` and rejects non-positive `rs_point` inputs. (9) Windows KiCad discovery sorts
  install versions numerically (`10.0` > `9.0`). (10) Node sanitizing restricts to ASCII
  `[A-Za-z0-9_]`. (11) `--wave` paths containing spaces now work. (12) The unmodeled-device hint points
  at real surfaces (`Sim.*`, `spec.models`, `models.fit_diode`) instead of a then-nonexistent
  `akcli sim fit-diode` subcommand (which this cycle now ships — see Added above).

## [0.5.0] - 2026-07-13

### Added
- **`akcli jlc datasheet <C-number|MPN|sch> [--fetch] [--out DIR] [--force]`**
  — datasheet PDF resolution and download. Targets one C-number, one exact
  MPN (catalog-matched like `jlc bom`), or a schematic (every BOM line with
  an `LCSC` parameter, batch). Resolution reads the part's EasyEDA component
  record — the jlcsearch mirror never carries datasheet links and lcsc.com
  bot-gates plain fetches, but the record embeds the szlcsc-hosted PDF URL
  in `head.c_para.link` (symbol *or* footprint side; both checked, which
  also fixed `jlc show --easyeda` missing footprint-side links). `--fetch`
  verifies the `%PDF` magic so an HTML challenge page is never saved as a
  `.pdf`, writes atomically under `--out`/`AKCLI_DATASHEET_DIR`/
  `~/.cache/akcli/datasheets/` as `C<digits>_<MPN>.pdf`, and treats existing
  files as a cache (`--force` refetches). Links are **classified**, not
  trusted: direct `.pdf` → `resolved`; product/viewer pages (JS shells,
  bot-gated hosts) → `page-link` with the URL surfaced for a browser-grade
  fetcher; search-engine junk in the EasyEDA data → `no-link` with the LCSC
  product-page hint. Lint-style exits: `1` while anything short of a PDF
  remains, `7` on network failure, `no-lcsc` lines are advisory.
- **skills**: `schematic-authoring` gained a datasheet-driven design loop
  (fetch → read electrical characteristics → cross-check with `akcli calc`
  → margin-check against absolute maximum ratings); `design-calc` now points
  calculator inputs at datasheet values instead of guesses.
- **`akcli new [path] [--paper SIZE] [--title T] [--force]`** — bootstrap a
  minimal blank `.kicad_sch` (root uuid + paper + optional title block) that
  `draw`/`plan` can append to immediately, replacing the hand-seeded skeleton.
  `--json` = `{created, target, paper, title, status}`.
- **Multi-level `akcli undo`** — `draw --apply` now rotates up to 3 backups
  (`<name>.bak` newest, `.bak2`, `.bak3`). `undo --list` prints the stack
  (level/path/size/mtime, newest first); `undo --steps N` walks back N snapshots
  leaving one redo available.
- **`akcli add_sheet` op (18 ops now)** — hierarchical child-sheet reference:
  emits `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, and
  edge-computed sheet pins (`pins:[{name, type, side, offset_mil}]`). `at` =
  top-left mils; wires attach to a sheet pin BY COORDINATE (no `Sheet.Pin`
  endpoint). A cross-sheet net is a parent sheet-pin paired with the child's
  same-name hierarchical label (matches eeschema). KiCad only; the child
  `.kicad_sch` is authored separately (`akcli new`).
- **`check --fail-on {info,note,warning,error,never}`** — sets the minimum
  (post-waiver) finding severity that exits non-zero (default `warning` = prior
  behavior). `--exit-zero` is now a deprecated alias for `--fail-on never`.
- **`[[waiver]]` config table** (checker-agnostic, independent of the ERC-only
  `[[erc_waiver]]`): `{code (fnmatch), refs? (fnmatch glob/list), severity?
  off|note|info, reason?}` — `off` drops, `note`/`info` demote a finding,
  applied centrally before rendering and the exit decision. Bad shape →
  `BAD_CONFIG`. The metadata header always prints `config-waived: N (M demoted)`.
- **Structured finding positions** — findings may carry `pos` (`[x_mil, y_mil]`,
  model frame) and `anchors` (`{kind,id[,pos]}`, kind ∈ component|pin|net|label).
  Text appends ` @ (x,y)`; JSON emits `pos`/`anchors` only when present (positionless
  findings keep their exact prior shape); SARIF adds `logicalLocations` +
  `properties.akcli.{pos,anchors}` while `partialFingerprints` stay byte-stable.
- **Design-intent v2** (`check --intent`, additive; `protocol_version` stays 1):
  per-net mode override (`{"members":[…], "mode":"exact"|"subset"}` overrides the
  document mode for one net) and `fnmatch` wildcards on the REF part of a member
  (`"R*.2"`, `"U?.1"` — pin literal). A wildcard is an existence assertion (≥1
  matching pin; zero → `INTENT_MISSING_MEMBER` naming the pattern) and is ignored
  when computing `INTENT_EXTRA_MEMBER` in exact mode.
- **Live web UI lint overlay** — the `/live` dashboard gained a dashed-square
  findings overlay (toolbar `lint` / key `G`) drawn from a new
  `GET /api/findings` (fast offline nets+geom+layout lint, mtime-cached, mils→mm
  via `MIL_TO_MM = 0.0254`), plus per-line **datasheet links** on the networked
  BOM `?check=1` view (resolved via `parts.datasheet.resolve`, per-line tolerant).
- **Real bus netlist semantics (stage 2)** — `netbuild` models buses arbitrated
  against `kicad-cli`: `(bus_entry)` conduction, labeled-rip member selection
  (unlabeled rips float), `NAME[a..b]` inclusive vector expansion, and
  scope-correct cross-sheet bus-name merging. `akcli net`/`diff` on imported
  KiCad bus designs now report bus-carried nets. `model.NetPrimitives` gained
  `buses`/`bus_entries` (default-empty; Altium reader untouched).

### Changed
- **`netbuild` is near-linear** — a shared `O(log n + k)` orthogonal-segment
  index replaces the O(n²) geometric scans in net building and the connectivity
  writer; a ~5100-segment sheet now builds in a fraction of a second with
  byte-identical semantics.
- **`battery-life` default `derating` 0.7 → 0.8** — now aligned with `battery`,
  so the two give identical hours for the same capacity/current (override with
  `derating=`). `capacity=2500 i_avg=10` → 200 h / 8.33 d.
- **calc input-suffix rule** — a parameter whose declared unit is already
  milli-denominated (`battery-life`'s `capacity` in mAh, `i_avg` in mA) now
  takes a bare number and **rejects** a trailing engineering `m`
  (`capacity=2000m` → `ERROR: capacity is already in mAh`) instead of silently
  applying a compounding 1000× milli. The generic length unit `m` (meters) is
  unaffected.

### Fixed
- **`akcli view` port auto-increment now works on Windows** — the server
  inherited `allow_reuse_address` (SO_REUSEADDR), which on Windows means
  "bind even while another socket is LISTENING": a second `akcli view`
  silently shared/hijacked port 8765 instead of moving to 8766. The flag is
  now POSIX-only (there it merely skips TIME_WAIT). CI hardening from the
  same round: the webui test kicad-cli stub runs through a per-OS launcher
  (Windows CreateProcess cannot exec shebang scripts — WinError 193), and
  the browser regression asserts the theme *flips* instead of hardcoding
  the end state (the initial theme follows the runner's
  `prefers-color-scheme`).

## [0.4.0] - 2026-07-11

### Added
- **Net-connectivity diff on every `plan`/`draw`** — the op-list is
  dry-applied to a temp copy and the before/after netlists are diffed by
  **pin membership** (never by name, so renames can't masquerade as
  remove+create). A deterministic "Net changes:" block prints splits,
  merges, membership edits, renames, created and removed nets most-severe
  first (`! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.2>(2)`); `(none)`
  when connectivity is provably unchanged. `--no-net-diff` opts out;
  `draw --apply --strict-nets` **refuses the write** (exit 6) when a
  split/merge touches a named net. `--json` carries
  `net_diff: {equivalent, risk, lines}`.
- **`akcli nets <sch>`** — every net → sorted members on one line each
  (`--json` for machines); `--intent-snapshot OUT.json` writes the netlist
  as a design-intent document (`--include-unnamed` keys unnamed nets by
  stable id).
- **`akcli check --intent FILE`** — first-class **design-intent
  assertions**: a JSON file (`{"protocol_version":1, "mode":"exact"|"subset",
  "nets": {"SWCLK": ["U1.4","J2.2"]}}`) is asserted against the built
  netlist, matched by pin membership. Finding codes: `INTENT_PIN_UNKNOWN`,
  `INTENT_NET_NOT_FOUND`, `INTENT_MISSING_MEMBER`, `INTENT_EXTRA_MEMBER`
  (exact mode), `INTENT_NETS_SHORTED`. Snapshot → edit → assert round-trips
  cleanly; `--intent` alone runs as a pure selector like `--erc`.
- **`akcli relink-symbols <sch>`** — automated re-embed of stale
  `lib_symbols` cache entries from fresh `.kicad_sym` libraries (`--libs`
  dirs/files, default KiCad.app SharedSupport; `--only` scopes nicknames).
  Dry-run by default; `--apply` is gated by a **net-membership equivalence
  proof** (a moved pin in the new library refuses with `VERIFY_FAILED`,
  file untouched) and leaves `<name>.bak`. Companion check:
  `check --libsync [--symbols DIR]` warns `LIB_EMBED_STALE` on
  pin-signature drift (graphics-only drift stays silent) or notes
  `LIB_EMBED_OLD_FORMAT` without sources (opt-in only).
- **`rename_net` core op** (17 ops now) — rewrites matching label texts and
  power-port net Values; optional `scope` restricts the label kind; zero
  matches is a replay-safe note; match count reported. KiCad only.
- **`delete_component` `cascade: true`** — also deletes wires ending on any
  deleted pin's coordinate plus labels/no-connects/junctions anchored
  there (cascaded uuids reported). **`delete_object` `match`** selector
  (`{kind, name?, at?}`) addresses an object without a uuid —
  exactly-one semantics (0 = replay-safe note, >1 = error listing
  candidates).
- **`mid(REF.PIN,REF.PIN)` anchors** — `add_net_label` and the power-port
  ops accept the midpoint of two axis-aligned pins (25-mil tolerance,
  grid-snapped along the wire axis, clamped into the span); labels
  auto-orient along the wire.
- **Three connectivity macros** (9 macros now): `connect_and_label`
  (pin-to-pin wire + ONE mid-wire label — the fix for facing-pin label
  collisions), `place_pwr_flag` (`power:PWR_FLAG` placed MID-WIRE, never
  on-pin), `terminate_unused_unit` (place a spare op-amp/comparator unit +
  tie the inputs + no-connect the output in one op).
- **Op-list validator hardening** — unknown fields are now errors with a
  did-you-mean suggestion (`_`-prefixed keys stay annotation-safe);
  per-op field types are enforced with the op index and field named;
  duplicate `(designator, unit)` placements are a lint error
  (`delete_component` releases the designator); a crashing op handler is
  contained as a per-op `INTERNAL` error result, never a traceback.
- **New offline checks** (all advisory): `check --nets` on `.kicad_sch`
  gains `NET_PIN_MIDSPAN_TOUCH` (pin tip on a wire mid-span with no
  junction — NOT connected in eeschema), `NET_LABEL_UNATTACHED`, and
  `NET_WIRE_CORNER_ON_PIN` (the L-wire short trap); `check --layout` gains
  `LAYOUT_POWER_ON_PIN` (a PWR_FLAG/power symbol anchored on another
  symbol's pin tip), `LAYOUT_WIRE_THROUGH_SYMBOL`, and
  `LAYOUT_LABEL_OVER_WIRE`; `check --erc` gains `ERC_UNPLACED_UNIT`
  (unplaced units of a multi-unit part, `.kicad_sch` only; waiver token
  `unplaced_unit`).
- **Configurable schematic grid** — `[project] grid` in
  `akcli.toml` (bare number = mils, or `"50mil"`/`"1.27mm"`/
  `"0.5mm"`; default 50 mil); `check --nets` compares in exact integer
  nanometres, so metric grids are first-class.
- **Bus-entry connectivity gate** — wires may terminate on a bus entry's
  ends without false-dangling, and every bus entry end must land on a bus
  or a wire: a floating end is a new `DANGLING_BUS_ENTRY` ERROR that
  refuses the write like `DANGLING_ENDPOINT`.
- **`jlc bom --csv OUT.csv`** — JLCPCB upload BOM CSV
  (`Comment,Designator,Footprint,LCSC Part #`; refs comma-joined in one
  quoted cell; unresolved lines get a blank LCSC cell so a dead C-number
  never lands in an order file; `'-'` = stdout). **`--fix` is now
  confidence-gated** (writes only when the package matched AND the value
  is visible in the candidate description/MPN); **`--fix-all`** also
  writes low-confidence suggestions.
- **jlc network resilience** — transient failures (URLError/timeout/HTTP
  429/5xx) retry with exponential backoff honoring `Retry-After`; when
  retries are exhausted a stale cached response is served with a stderr
  warning (`AKCLI_JLC_CACHE_STALE=off` restores hard failure); cache
  writes are atomic.
- **Four more calculators (60 total)** — `battery-life` (datasheet mAh →
  runtime, ANSI C18.1M), `comparator-hysteresis` (open-drain-aware
  thresholds, TI SLVA954), `envelope-detector` (RC validity verdict),
  `ldo-headroom` (go/no-go + dissipation, TI SLVA079).
- **CLI UX** — one unambiguous status line on plan/draw
  (`dry-run — nothing written` / `APPLIED — wrote ... (backup ...; akcli
  undo reverts)` / `REFUSED — nothing written`); did-you-mean suggestions
  for mistyped calc and ops names; duplicate-designator reader warnings
  now print to stderr; `ops list`/`arrange`/`undo` honor `--json`.
- **Packaging & CI honesty** — `[project.urls]`, setuptools ≥ 77 floor
  (PEP 639), Python 3.14 classifier + CI matrix entry, a fast `ruff` lint
  job, and a wheel-smoke job (build → install into a fresh venv →
  `akcli --version && akcli ops list && akcli ops template add_wire`);
  the ops schema ships inside the wheel
  (`akcli/schemas/`, repo-root `schemas/` stays canonical).
- **`akcli arrange <sch>`** — closes the layout loop: nudges **free**
  components (no wire endpoints or label anchors on any pin — moving
  anchored parts would strand their connectivity) until no symbol boxes
  overlap. Greedy first-fit in reading order, `--grid`/`--margin` tune the
  packing, dry-run by default; `--apply` writes through the standard draw
  pipeline (`.bak` + connectivity re-verify), so `akcli undo` reverts it.
- **`jlc bom --qty N`** — purchasability at build quantity: each line needs
  `qty × refs` pieces, stock is checked against that, the applicable
  **price tier** is selected at that quantity, and the table gains
  NEED/UNIT/EXT columns plus an estimated parts cost per run
  (`totals` in `--json`).
- **`jlc bom --suggest` / `--fix`** — for `not-found` / `no-part-id` lines,
  search the catalog by value + footprint package (`100n` + `C_0402_…` →
  `100nF 0402`; candidates must match the package, in-stock Basic parts
  win) and print the best replacement; `--fix` writes the C-number back
  into the schematic's LCSC parameter (same key when one existed) through
  the draw pipeline (`.bak`, undo-able), then re-checks. Suggestions are
  heuristics — verify the datasheet.
- **jlc HTTP cache on by default** — `search`/`show`/`bom` reuse the
  existing on-disk cache (1 h TTL) under `~/.cache/akcli/jlc`
  (`AKCLI_JLC_CACHE` relocates or disables it; tests isolate via conftest).
- **Four more macro ops** — `place_pullup`, `place_led_indicator`,
  `place_rc_filter`, `place_crystal` (ST AN2867 topology), all label-on-pin.
  The validator now accepts **un-expanded** macro documents (checking macro
  required fields), so externally validated op-lists may carry macros.
- **`calc --ops` emits macros** — `vdivider-design`, `led`, `i2c-pullup`
  and `crystal-caps` now produce compound ops with placeholder net names
  (edit, `plan`, draw) instead of loose part strips: the parts arrive
  connected.
- **Live dashboard BOM panel** — `b` / the `bom` toolbar button opens the
  watched sheet's BOM (offline); *check purchasability* triggers the ONE
  networked action (`GET /live/bom?check=1`) with stock/price/est-cost.
- **CFBF/Altium fuzz suite** — seeded mutations (header damage, FAT
  surgery, truncation, byte noise) over the OLE2 container reader; the
  `ALTIUM_*` guard rails held with zero findings.
- **Schema contract tests** — `read --json` exports of KiCad *and* Altium
  fixtures now validate against `schemas/schematic.schema.json` in CI, and
  the schema's `schema_version` const is pinned to the model's.

- **`akcli view` — ONE dashboard server** (HTML ships as package data; binds
  127.0.0.1, zero deps): `/calc` and `/live` are served by a single process
  on port 8765 (auto-increments when busy). `/` is the **hub entry page** the
  browser opens on launch — one card per dashboard, the live card streaming
  the watched file, step count and latest ERC state over SSE; all pages
  cross-link. `view calc` serves the bench alone;
  `view live <sch>` (or the shorthand `view <sch.kicad_sch>`) additionally
  watches the schematic: each change exports every sheet's SVG via
  `kicad-cli`, counts parts/nets with the in-process reader, and appends a
  timeline step **immediately** — KiCad's JSON ERC back-fills the step
  seconds later, so a draw shows up in ~3 s instead of ~15 s. Updates are
  pushed over Server-Sent Events (`/live/events`); responses gzip when
  accepted; step SVGs serve as immutable. New endpoints: `POST /live/note`
  (annotate the next step from the UI) and `POST /live/clear`; new flag
  `--max-steps N` bounds the timeline (default 500, oldest SVGs deleted).
  The standalone `tools/calc-view/` and `tools/live-view/` directories are
  removed.
- **`view` bench UI (`/calc`)** — full dashboard rebuild: home launcher over
  all groups, ⌘K command palette + fuzzy sidebar filter, debounced
  auto-compute with live engineering-notation parse hints (`4k7` → `= 4.7 kΩ`)
  and field-level error highlighting, defaults shown in typed-back notation
  (`35u`, not `0.000035`), per-result change chips vs the previous run,
  click-to-copy exact values (with mm/mil tooltips), copy-as-markdown/JSON/CLI,
  diagram captions annotated with the computed values (Z0, Vout, τ/fc, …),
  a persistent session log, an `⤓ op-list` button (backed by a new
  `GET /api/ops`, the web twin of `calc --ops`) on the 8 mappable calculators,
  pinned/recent lists, shareable URL hashes, dark bench / light datasheet
  themes (theme-aware SVG illustrations), print stylesheet, and a status bar
  that mirrors the equivalent `akcli calc` command. `/api/list` now carries
  `meta` (count, version, watched file) and a per-calculator `mappable` flag.
- **`view` watch UI (`/live`)** — same rebuild: per-step **ERC violation
  panel** (each step stores the KiCad JSON ERC findings; **NEW** findings vs
  the previous step are tagged and counted, resolved ones reported; click a
  finding to zoom to its marker on the sheet), ERC marker overlay, **diff
  mode** (previous step ghosted in red under the current one), sheet tabs for
  hierarchical designs, timeline replay, parts/nets delta per step,
  parts-trend sparkline, PNG export of the current view, `ink` dark-paper
  mode, an in-UI note box for the next step, a clear-timeline action,
  relative timestamps (steps carry an epoch `ts`), and a keyboard map
  (`←/→ L F C D E I space ?`).
- **Browser UI regression suite** — `tools/ui-test/` drives the system Chrome
  (puppeteer-core, no download) through ~35 checks on all three pages;
  `tests/test_webui_browser.py` wires it into pytest (auto-skips when
  node/Chrome are absent) and CI runs it on the macOS runner.
- **Shared bench chrome** — hub, `/calc` and `/live` now carry the identical
  top bar: the `⌂ akcli` mark and a `⌂ | calc | live` page switcher on the
  left, theme + help on the right, `h` returns to the hub from any page.
  One layout to learn; no page is ever a dead end.
- **`akcli verify <a> <b>`** — a net-equivalence proof between two schematics
  (e.g. an Altium original vs its KiCad conversion): PASS iff the component
  set matches and every net's pin membership is identical; net renames are
  reported but do not fail; `--strict` also fails value/footprint drift.
  Exit 0/1; `--json` carries the full diff report.
- **`akcli undo <sch>`** — swaps a `.kicad_sch` with the `<name>.bak` that
  `draw --apply` leaves beside it (dry-run preview by default, `--apply` to
  swap; undo twice = redo). The preview shows the part/net delta.
- **Macro ops** — `place_divider` and `place_decoupling` expand to core ops
  before validation (never touching `protocol_version`, the schema, or the
  executors), using the collision-proof label-on-pin pattern for
  connectivity. `ops list` shows them; `ops template <macro>` works.
- **`akcli check --nets`** — connectivity-hygiene checks, in the default
  check set: `NET_SINGLE_PIN` (floating label / undriven power port) and
  `NET_OFF_GRID` (pins off the 50-mil grid — the classic wire-that-touches-
  but-never-connects trap).
- **Parser fuzzing** — seeded stdlib fuzz of the s-expression parser
  (truncation, paren storms, quote damage, Unicode noise, depth bombs);
  the contract is "SNode or structured AkcliError, never a crash".

- **`akcli jlc bom <sch>`** — BOM → JLCPCB purchasability bridge: every BOM
  line resolves to a catalog part (explicit LCSC C-number parameter first,
  then exact-MPN search preferring in-stock Basic parts) and reports stock /
  price / Basic-Preferred, with `low-stock` (`--min-stock N`),
  `out-of-stock`, `not-found` and advisory `no-part-id` statuses. Lines
  group by identity (one lookup per part, `QTY` = ref count), `#`-virtual
  parts are excluded, Altium and KiCad inputs both work. Lint-style exit 1
  on problems, exit 7 on network errors, `--json` for machines.
- `AKCLI_JLC_BASE_URL` overrides the jlcsearch endpoint (self-hosted
  instance, moved service, or exercising the `NETWORK`/exit-7 path in tests).

- **`akcli check --layout`** — geometric-overlap lint for `.kicad_sch` (also in
  the default check set): estimates world-space boxes for symbol bodies (from
  the embedded `lib_symbols` graphics) and label text, then reports
  `LAYOUT_SYMBOL_OVERLAP`, `LAYOUT_LABEL_OVER_SYMBOL`, `LAYOUT_LABEL_OVERLAP`,
  and `LAYOUT_COINCIDENT_TEXT` findings. A schematic can pass ERC with every
  label drawn on top of the part it names — ERC never checks graphics.
- **`"at": "REF.PIN"` anchors for labels and power ports** — `add_net_label`,
  `place_power_port`, `place_gnd`, `place_vcc` accept a pin reference as `at`
  (exact world coordinate, never grid-snapped), making the collision-proof
  label-on-pin pattern first-class.
- **Net labels auto-orient away from the symbol** — a label anchored on a pin
  (via `"REF.PIN"` or a coordinate that hits a pin tip) with no explicit
  `orientation` is rotated so the text extends away from the body
  (`geometry.label_angle_away`; `model.Pin` now records the lib-frame pin
  orientation). Explicit `orientation` always wins.
- `readers.kicad_lib.body_extent_mil()` / `is_power_symbol()` — shared
  symbol-body extent (graphics, not pins) and power-marker detection, used by
  text autoplacement and the layout lint.

- **`akcli pins <lib_id>`** — op-list authoring helper that prints every pin's
  number, name, electrical type, and **world coordinate** for a
  `--at`/`--rotation`/`--mirror` placement, resolved from the same symbol sources
  the writer uses (`--symbols` / config `.kicad_sym`) and computed with the writer's
  own `geometry.pin_world`. Removes the guesswork of targeting pin coordinates when
  hand-authoring wires/labels/power ports. `--json` for machine output.
- **20 more calculators (56 total)**, all standards-cited: differential pairs
  (IPC-2141A over Hammerstad–Jensen/Cohn single-ended), `tracktemp` (IPC-2221 solved for ΔT),
  unit conversions (dBm/W/Vrms per IEEE Std 100; mil/mm exact 25.4; oz/µm copper nominal +
  pure-Cu physics), comparator hysteresis analysis+design (TI SLVA954, round-trip-tested),
  RS-485 fail-safe bias (TIA-485-A/SLLA070D), CAN split termination (ISO 11898-2), LDO
  dissipation/dropout/thermal, MOSFET gate drive (TI SLUA618A), current-sense shunts
  (TI SBOA170), Sallen–Key equal-component design (TI SLOA024B), ADC LSB/SNR/settling
  (MT-001), TVS selection (IEC 61000-4-5 surge), fuse derating on the IEC 60127 R10 ladder,
  NTC inrush sizing (TDK/EPCOS guide), L- and PI-section matching (Pozar §5.1/Bowick),
  flyback first-order design (Erickson ch. 6). **IPC-2152 deliberately not faked** —
  chart-based licensed data with no public closed form.
- **`akcli calc` tooling:** `calc batch <file|->` (JSON job list → envelope array, exit 1
  if any job fails), `--md` (markdown result table), and `--ops FILE` — design-type
  calculators emit a schema-valid `place_component` op-list with the computed E-series
  values filled in, validated against `ops.validate_oplist` in tests.
- **`tools/calc-view/`:** local web UI (stdlib server, localhost-only) for all 56
  calculators — grouped sidebar with filter, auto-generated forms, results with units and
  the formal reference, ~45 physical-style SVG illustrations (via/trace cross-sections,
  stackups, LM317/555/I²C/RS-485/CAN/flyback networks; the resistor color-code diagram
  colors its bands from the actual result), pinned/recent calculators, and shareable URLs
  (the hash carries calculator + inputs and re-runs on load).
- **`akcli calc` — 36 offline engineering calculators**, each stamped with its formal
  reference: E-series snap + 2–4-resistor combination search (IEC 60063:2015, tabulated
  E1–E24 + formula E48/E96/E192 with the 9.20 exception), dividers/LED/RC/LC
  (Horowitz & Hill 3rd ed.), LM317/FB regulator networks with exhaustive worst-case corner
  analysis (TI SLVS044Y), IPC-2221B §6.2 track width ↔ current and Table 6-1 clearance
  (incl. >500 V slopes), via R/thermal/ampacity/L/C/rise (Johnson & Graham 1993),
  Onderdonk/Preece fusing, ASTM B258 AWG, microstrip (Hammerstad–Jensen 1980), stripline
  (Cohn 1954 exact via AGM), coax/twin-lead (Pozar), PI/TEE/bridged-TEE attenuators,
  buck/boost stages (TI SLVA477B/372C), NE555 (SLFS022I), op-amp pairs (SLOD006B),
  I²C pull-up window (NXP UM10204 §7.1), crystal caps (ST AN2867), JESD51 thermal, battery
  life, resistor color/SMD/EIA-96 codes (IEC 60062:2016), galvanic pairs (MIL-STD-889C).
  Inputs take engineering notation (`4k7`, `100n`); `--json` returns
  `{calc, inputs, results, reference}`. Numerics cross-validated against KiCad's
  pcb_calculator outputs (independent clean reimplementation — no GPL code) and published
  handbook values in `tests/test_calc.py`. New **`design-calc` skill** teaches agents to
  compute-then-place E-series values instead of guessing.
- **Binary `.PcbDoc` copper decoded:** `Tracks6`/`Vias6`/`Arcs6`/`Pads6` are now parsed
  (new `readers/altium_pcb_bin.py` — packed little-endian records, coordinates in mils,
  native +Y-up frame) and land on the `Pcb` model as `tracks`/`vias`/`arcs`/`pads`;
  `--json` schema is now **1.1**. Layouts were cross-validated item-by-item against KiCad's
  own Altium importer (`pcbnew`) on real boards from KiCad's QA corpus: 778/778 board-level
  copper tracks, 20/20 vias, 236/236 arcs, 48/48 pads (names, sizes, drills, positions exact
  modulo ±3 nm importer rounding); a second board from a different AD version (3661 tracks /
  321 vias / 88 arcs / 468 pads) decodes with zero errors. Unknown record types inside a
  known section fail `ALTIUM_UNSUPPORTED`; truncated records fail `ALTIUM_MALFORMED` —
  nothing is silently skipped anymore. `Fills6`/`Regions6`/`Texts6`/`Polygons6` remain out
  of scope (still skipped, documented).
- **Hierarchical sheets (Altium reader):** a `.SchDoc` root recurses into sheet symbols
  (RECORD 15 + name/file 32/33), each instance in its own geometric namespace, with
  sheet-entry (RECORD 16: `Name`/`Side`/`DistanceFromTop`) ↔ child-PORT pairing per Altium's
  *Automatic* net-identifier scope — ports merge globally only in designs WITHOUT sheet symbols,
  so two children exposing the same port name stay separate; flat designs read exactly as
  before. `.PrjPcb` is accepted as input: akcli finds the top sheet (the one no sheet symbol
  references) and honors `PowerPortNamesTakePriority`. The previous RECORD-16 handling was dead
  code (it read `Text`/`Location` — real entries carry `Name`/`Side`/`DistanceFromTop`).
  Runtime-generated hierarchical fixtures; sheet-entry position scale follows the documented
  convention, real-AD validation flagged as pending.
- **Op-list authoring kit:** `docs/op-list-authoring.md` (envelope, coordinate contract, all
  16 ops with notes, pipeline, idempotency rules) plus `akcli ops list` (vocabulary + required
  fields + executor support) and `akcli ops template <op>` (fill-in JSON skeleton); the in-code
  tables are drift-guarded against `schemas/ops.schema.json` by tests.
- **Autoplace collision avoidance:** visible Reference/Value anchors register in a per-apply
  registry; a new label landing within one label extent of an existing one bumps outward
  deterministically (replays stay byte-identical). Fixes neighboring parts' texts stacking
  (the `+3V3`-on-`C2` case).
- **`check --format sarif|junit`:** SARIF 2.1.0 output for GitHub code scanning (stable
  `partialFingerprints`, schematic path as artifact URI, rule table) and JUnit XML for CI test
  reporters (WARNING+ findings as failed testcases; NOTE/INFO as passed cases with
  `system-out`; clean runs emit one passed case). Lint-style exit semantics unchanged.

### Fixed
- **Rotation transform now matches eeschema exactly** — a file angle of
  +90° rotates counter-clockwise on screen (`(x,y) → (y,−x)` in the
  +Y-down frame); akcli's writer and reader each implemented a different
  wrong order (the truth is rotate-by-minus-angle THEN mirror). Both now
  share one transform, locked by a 12-combo rotation/mirror truth table
  verified against kicad-cli's own netlister. **This corrects netlists of
  schematics with rotated polarized parts** (e.g. LEDs at 270° had
  anode/cathode swapped in the derived netlist); on the reference design
  the corrected netlist now matches kicad-cli's export exactly.
- **KiCad junction dialect** — eeschema does NOT connect a wire end
  touching another wire's mid-span without a junction node; the KiCad
  reader now matches (the Altium reader keeps Altium's bare-T-connects
  dialect). Same-sheet local-label ↔ global/power merging was verified
  against eeschema and kept: a local label DOES merge with a same-name
  global label or power port on the same sheet even when physically
  disconnected, and never across sheets.
- **Duplicate designators are no longer silently merged** — a re-placement
  of the same unit under an existing designator is kept as a distinct
  component (with a reader warning and an `akcli_duplicate` parameter), so
  `BOM_DUPLICATE_DESIGNATOR` now fires for KiCad inputs, matching how
  eeschema netlists such placements.
- **`check --nets` measures in exact integer nanometres** — off-grid and
  coincidence comparisons no longer accumulate float error; the grid is
  configurable (see `[project] grid`).
- **Wires ending on a bus entry no longer report `DANGLING_ENDPOINT`**
  (bus entries are now connection anchors; see `DANGLING_BUS_ENTRY`).
- **`akcli view` hardening** — the live dashboard rejects non-loopback
  `Host` headers (DNS-rebinding) and cross-origin POSTs (CSRF); a watcher
  crash now surfaces as a dashboard banner instead of silently stopping;
  step SVGs are cache-fingerprinted so a cleared timeline can never serve
  stale renders; `/live/bom` failures return structured errors; timeline
  steps are keyboard-accessible; `view <sch>` opens the browser on
  `/live` directly.
- **`set_component_parameters` no longer piles visible text on the symbol**
  — a NEW property node sits at the symbol anchor, so custom fields (LCSC,
  MPN, …) rendered as raw text over the body. The writer now creates every
  field except Reference/Value hidden, matching KiCad's own default.
- `schemas/schematic.schema.json` declared net `name` as `string`, but
  unnamed nets export `null` (`is_named: false`) — the schema now matches
  the long-standing export shape.

- **BOM checks no longer flag `#`-prefixed virtual parts** — power ports and
  PWR_FLAG have no value/footprint by design and never appear on a BOM;
  `#PWR01 has no footprint` warnings were pure noise.
- **netbuild: local labels now join same-name power/global nets on the same
  sheet** — KiCad merges a local `+3V3` label into the `+3V3` power net;
  akcli kept them as two nets, so label-on-pin connections to rails were
  invisible to `net`/`export`/`diff`/checks. Verified against kicad-cli
  netlist output.
- **sexpr: Unicode whitespace no longer crashes the tokenizer** — the bare-
  atom regex used `\s` (Unicode) while the scanner skips ASCII whitespace
  only; an NBSP between tokens raised a raw AttributeError instead of a
  structured error (found by the new fuzz suite).

- **Labels now carry the `(justify ...)` their angle needs.** KiCad never
  draws text upside-down: a global label at 180° WITHOUT `(justify right)`
  still renders its text toward +X — i.e. straight over the symbol it names.
  The writer now emits eeschema's exact angle/justify pairs
  ((0,left) (90,left) (180,right) (270,right); local labels add `bottom`).
- **Reference/Value of rotated instances render level and clear of the body.**
  Property text angle now counter-rotates the instance rotation (mod 180 — a
  180° property would render inverted), and autoplacement works from the pin
  box UNION the drawn body extent, so a rotated resistor's value no longer
  prints vertically through its own body, and a connector's value (pins all on
  one side) no longer lands inside the outline.
- **Any `(power)` symbol hides its Reference** — a `PWR_FLAG` placed as
  `FLG1` (no `#` prefix) printed its designator into the schematic. Power-port
  Value text is now placed past the side the body extends to (a +5V arrow's
  name above it, GND's below), matching eeschema.
- **`power:PWR_FLAG` no longer merges rails in net inference.** A `PWR_FLAG` power
  symbol is meant only to mark a net as driven for ERC; the KiCad reader was
  injecting a `"PWR_FLAG"` power-net name at its pin, so two flags (e.g. one on
  +5V, one on GND) unioned every rail they touched into a single net — a false
  +5V↔GND short in `akcli net`/`check` (KiCad ERC was unaffected). The reader now
  emits the flag's pin (keeping it electrically on its net, satisfying KiCad's
  `power_pin_not_driven`) but never names/merges a net from it.
- **`calc` output never SI-prefixes non-base units:** values already carrying a prefixed or
  compound unit (mm, °C/W, m², Ω/km) print plain — the clearance table rendered 0.2 mm as
  "200 mmm".

## [0.3.1] - 2026-07-07

### Added
- **`akcli expected` subcommand:** extract an expected pin→signal table from a Zephyr
  devicetree source/overlay (gpio phandles + Nordic `NRF_PSEL` pinctrl) or a markdown pinout
  table, as the JSON `pinmap --expected` consumes. Empty extraction exits `1` (a vacuous
  table must not read as success).
- **CLI-layer offline tests for `jlc add`** (flag validation, exit-code mapping, `--place`
  op-list emission) against the captured EasyEDA fixtures.
- **Altium fixture invariant sweep:** auto-discovering tests over every `.SchDoc` fixture —
  net members must reference real component pins, membership sorted/duplicate-free, reads
  deterministic, CSV/Protel exports agree with the inferred netlist, and the malformed corpus
  fails loudly. New fixtures are swept automatically. (The independent cross-check against
  Altium's own netlist export still requires a real AD install; KiCad's Altium importer is
  GUI-only and cannot be driven headless.)

## [0.3.0] - 2026-07-06

### Added
- **`tools/live-view/`:** a localhost dashboard that watches a `.kicad_sch` while akcli draws
  it — per-step SVG (inline, auto-cropped), KiCad ERC badges, part/net counts, notes, zoom/pan,
  follow-live; optional macOS auto-revert of an open KiCad editor. See its README.
- **New `jlcpcb-capabilities` skill:** manufacturing limits to design against, with
  **嘉立創 (jlc.com) as the primary source** — 1–64 layers, HDI blind/buried vias, 0.1 mm
  microvias, up to 6 oz copper, FPC and 經濟/標準 SMT gates — plus a difference table against
  JLCPCB-international (32 layers, no blind/buried, BGA ≥0.35 mm, ...) and the intl stencil
  specs. Sources + snapshot date stated in the skill; includes apply-while-drawing guidance
  (comfortable defaults vs 極限值, schematic-time package gating, AD/PADS export gotchas).
- **Converted libraries import into Altium Designer natively:** the footprint writer now
  emits the **KiCad 6 dialect** (`(layer)(width)` graphics, version `20211014`) — readable by
  every KiCad from 6 to 10 *and* by Altium Designer's built-in **Import Wizard » KiCad Design
  Files** (whose KiCad support is pinned to 6.0x), which converts the produced
  `.kicad_sym`/`.kicad_mod` to a native `.SchLib`/`.PcbLib`. This replaces the dead
  npnp `--to altium` path with a vendor-supported one.
- **`akcli jlc add` is back — in-process, zero-install:** LCSC → KiCad symbol/footprint/3D
  conversion now runs inside akcli via the vendored MIT conversion core of
  **JLC2KiCadLib** (TousstNicolas; license + provenance in
  `src/akcli/_vendor/jlc2kicadlib/` and `THIRD_PARTY_NOTICES.md`). Upstream's two
  dependencies are deliberately not vendored: `requests` is replaced by a stdlib shim and the
  GPLv3 `KicadModTree` by a clean-room `.kicad_mod` writer that emits the modern
  `(footprint ...)` dialect. `--place` emits a `place_component` op-list as before; no external
  binary and no pip dependency required.

### Removed
- **`akcli jlc add` (external library conversion):** the upstream `nlbn`/`npnp` converter
  repositories are no longer available, so the delegation, the pinned auto-downloader, and the
  `--place` op-list emission were removed. `jlc search`/`jlc show` (and `--easyeda` metadata)
  are unchanged. Symbols/footprints now come from the official KiCad libraries or project
  `.kicad_sym` files.

## [0.2.0] - 2026-07-06

Not yet published to PyPI; install from source (see `INSTALL.md`).

### Added
- **Readers:** Altium `.SchDoc` / `.SchLib` and ASCII `.PcbDoc`; KiCad `.kicad_sch` (v7/v8) via a
  bounded, non-recursive S-expression parser with pin-type resolution from `lib_symbols`.
- **Net inference** (`netbuild`) shared across both formats: same-name merge, junctions/T-junctions,
  No-ERC handling.
- **CLI:** `read`, `net`, `component`, `check` (ERC/power/BOM), `diff` (net-membership), `pinmap`,
  `export`.
- **KiCad write/draw:** `plan` / `draw` from a versioned JSON op-list — atomic, idempotent (UUIDv5),
  connectivity-verified.
- **JLCPCB/LCSC parts:** `jlc search` / `show` / `add` (conversion via external `nlbn` / `npnp`).
- **Claude Code plugin:** circuit-design skill + `circuit-review` / `circuit-pinmap` / `circuit-draw` /
  `circuit-diff` commands; DTS / pinout adapters.
- **Altium live driver (preview):** Python file-based JSON bridge; the Windows DelphiScript half is a
  scaffold pending validation.
- Documentation (`README.md`, `INSTALL.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, `docs/SPEC.md`,
  `docs/cli-reference.md`), reference config, and CI matrix.

- **`BOM_CORRUPT_TEXT` check (NOTE):** components whose value/parameters contain the U+FFFD
  replacement character are surfaced with an aggregated finding instead of silently printing `�`.
  Root-cause analysis on real-world files showed the corruption is baked into the `.SchDoc` at
  export time (a legacy-code-page value pushed through a lossy UTF-8 decode by the authoring tool --
  both the ANSI field and its `%UTF8%` twin carry the damage), so no decoder can recover it; the
  finding says so and points at re-export.

### Added
- **Hierarchical sheets (KiCad reader):** `read`/`net`/`check`/`diff`/`pinmap` on a root
  `.kicad_sch` now recurse into `(sheet ...)` children (paths relative to the parent file,
  cycle- and depth-guarded). Every sheet INSTANCE is its own geometric namespace — a file
  instantiated twice contributes its components once per instance with designators resolved
  from the matching `(instances (path ...))` entry — and connectivity crosses sheets only
  through sheet-pin↔hierarchical-label pairs (strictly parent↔child, never global), global
  labels, and power ports. The writer stays flat-only v1.
- **`delete_component` / `delete_object` / `move_component` ops:** delete removes all placed
  instances of a designator (or one object by uuid) — attached wires are left for the
  connectivity gate to flag, so stale wiring is cleaned up explicitly, and deleting an
  already-absent target is a replay-safe no-op; move repositions one instance (designator +
  optional unit) with its properties travelling along, wires intentionally not stretched.
- **Property autoplace:** placed symbols now get eeschema-style field layout — Reference/Value
  beside a tall (vertical-pin) body or above/below a wide one, `Footprint`/`Datasheet`/
  `Description` created hidden, power symbols with hidden `#PWR` references and the value
  below the anchor. Previously every field rendered at the component origin (the synthesized
  Reference even at absolute 0,0), stacking raw text over the body.
- **Multi-unit placement:** `place_component` takes an optional `"unit": N` — each unit is
  its own placed instance sharing the designator (74xx gate A/B/...). `"REF.PIN"` endpoints
  resolve against the instance whose unit owns the pin; wiring a pin on an **unplaced** unit
  fails loudly with the unit to place, instead of silently snapping to another unit's body.

### Fixed
- **Placed instances expose only their own unit's pins** (reader, writer, verifier): every
  unit of a multi-unit symbol shares local pin geometry, so treating all units' pins as
  present at one instance mapped all four 74xx gates onto one body — `akcli net` merged
  unrelated gate pins into one net while eeschema saw two, and phantom pin points masked
  real dangling wires in the connectivity gate. Instances of one designator now merge into
  a single component on read (no false `BOM_DUPLICATE_DESIGNATOR`).
- **Multi-line/control text is escaped KiCad-style:** `_q` escaped only `\` and `"`, so an
  `add_text` with a newline wrote a file KiCad refused to parse while every akcli gate
  passed (akcli's lexer tolerates a raw newline in a quoted atom; eeschema does not).
  `\n`/`\r`/`\t` are now escaped in all writer quoting helpers.
- **Pin taps now follow eeschema connectivity:** a pin tip touching a wire's **mid-span**
  connects only when a junction marks that point (or at a segment endpoint) — both in net
  inference (`netbuild`) and, constructively, in the writer: `auto_junctions` now also
  considers pins lying on a segment interior, so a placed part tapping a rail gets its
  junction automatically (previously the mid-span-pin rule never fired because candidates
  were wire endpoints only, and `akcli net` claimed connectivity KiCad rejected).
- **Replaying an op-list is byte-identical after ONE apply:** idempotent replay now replaces
  same-uuid nodes **in place** instead of remove-then-append, which migrated every op node to
  the end of the file while auto-junctions stayed put — the first re-apply reordered the
  document and byte-idempotency only converged on the second apply.
- **Large op-lists are no longer quadratic:** each placement re-parsed the whole (growing)
  inline `lib_symbols` cache to resolve its symbol; symbols now resolve once, from just their
  own cached body, memoized per apply run. A 478-placement sheet went from >120 s (timeout)
  to 1.7 s.
- **Duplicate pin numbers across units no longer collide:** multi-unit parts with shared pads
  (e.g. dual DirectFETs — unit A pins 1,2,3 / unit B pins 1,4,5) legitimately repeat a pin
  number, but the writer seeded every per-pin UUID with just `designator.pin<N>`, so the two
  `(pin "1" ...)` nodes got the same UUID and the connectivity gate refused the write
  (`DUPLICATE_UUID`). Later occurrences now carry a `#k` suffix in the seed; first occurrences
  keep the historical seed, so existing schematics replay byte-identically. Found by the
  library-wide sweep (`Transistor_FET:IRL6297SD` was the one failure in 478).
- **Alternate (DeMorgan) body styles no longer duplicate every pin:** the KiCad library
  reader collected pins from every `Name_<unit>_<style>` sub-symbol, including the `_<unit>_2`
  DeMorgan re-drawing of the same physical unit — so a 74xx-style symbol resolved with each
  gate pin twice, the writer emitted colliding per-pin UUIDs, and the connectivity gate refused
  the placement (`DUPLICATE_UUID`, exit 6). Only body style 1 is collected now, and each pin
  records its owning unit in `owner_part_id` (`_0_*` common sub-symbols map to unit 1). Found by
  a library-wide sweep placing every derived symbol in KiCad's official 74xx library.
- **`(extends)`-derived symbols are now FLATTENED into the written `lib_symbols` cache**
  (KiCad-save style): the base's units/pins/graphics are inlined under the derived name (unit
  sub-symbols renamed `Base_u_s` → `Derived_u_s`), the derived symbol's own properties/settings
  overlaid, and the `extends` clause dropped — no base is cached separately. Previously the cache
  kept a bare `(extends "Base")` next to a library-qualified `Nick:Base` entry, which KiCad's
  loader does **not** resolve: eeschema reported `lib_symbol_mismatch`, the derived part lost all
  its pins, every wire to it dangled (`unconnected_wire_endpoint`), and KiCad's netlist omitted
  the part entirely — while akcli's own verifier and netlist looked clean. Found by running a
  drawn AMS1117-3.3 LDO block through KiCad 10's own ERC; regression-tested in
  `test_e2e_draw.py` (cache shape everywhere; pins-on-net via real `kicad-cli` in the KiCad CI job).
- **`kicad-cli` advisory runs work on KiCad 10 again:** KiCad 10's argument parser rejects the
  `--` end-of-options separator (`Unknown argument: --`), so every advisory ERC/netlist run was
  silently degrading to `report: null` (exit 1). Paths are now passed absolute instead of behind
  `--`, keeping the option-injection guard (an absolute path cannot start with `-`).
- **CFBF DIFAT spillover (> 109 FAT sectors) is now walked**, not refused: the spillover chain is
  read under the header-declared count, a cycle set, and the global sector cap (hostile input still
  fails with `ALTIUM_FAT_CYCLE` / `ALTIUM_ALLOC_GUARD` / `ALTIUM_MALFORMED`). Large real-world
  `.PcbDoc` containers now open.
- **`BOM_MISSING_VALUE` no longer fires on vendor-library parts** whose value lives in the part
  identity: a part-number parameter (`Manufacturer Part`, `LCSC Part Name`, ...) or a digit-bearing
  `library_ref` (e.g. `AO2301`) now substitutes for a blank `Comment`/`Value`. Generic symbols with
  no identity still report.
- **`ERC_NO_POWER` / `ERC_NO_GROUND` skip `U`-prefixed parts with fewer than 3 pins** (2-pin
  headers/jumper stubs designated `U*` are not ICs).
- **`pinmap` without a configured MCU** now says how to fix it (`--mcu <REF>` or
  `[project].mcu_designator`) instead of a bare warning.
- **Footprints** now resolve via the model-link chain (RECORD-45 model → RECORD-44 implementation →
  RECORD-1 component): the owner keying was wrong, so the model-link footprint was never found; the
  RECORD-41 `Footprint` / `Supplier Footprint` parameter is the fallback. Removes false
  `BOM_MISSING_FOOTPRINT` (80/80 components resolved on the reference board).
- **Rail voltage inference** no longer mis-fires on underscore-suffixed rails (`V3V3_AUX`, `V3V3_IO`):
  the trailing word-boundary that `_` defeated is replaced; logic is now shared in `checks/_rails.py`,
  and configured `[[rail]]` names match `<rail>_suffix` too. Fixes false `ERC_NO_POWER`.
- **`export --json`** now errors (exit 2) with guidance instead of emitting non-JSON at exit 0.
- **`.SchLib` / `.PcbDoc`** binary records now surface as `ALTIUM_UNSUPPORTED` (exit 5, *unsupported*)
  instead of `ALTIUM_MALFORMED` (exit 3, *parse error*).
- **`pinmap --expected`** unmatched pins are now `WARNING` (non-zero exit) instead of a silent NOTE.
- **`-C/--config`** (and other global flags) are accepted before *or* after the subcommand.
- `schema_version` now stamped on every machine-readable command: `check` / `diff` / `pinmap` reports,
  plus `read` and `component --json` (`net` stays a bare array, as documented).
- **`draw --apply`** writes a `<target>.bak` next to the file (the doc'd backup was never wired up).
- `tarfile` extraction uses `filter="data"` (Python 3.14-ready, hardened).

### Known limitations
- A value whose Ω/µ/± was already written as the U+FFFD replacement bytes (`EF BF BD`) by an upstream
  tool on a non-UTF-8 locale is corrupted **at export** and cannot be recovered on read by any codec.
- `draw` snaps off-grid / non-orthogonal geometry rather than rejecting it.
- The Windows Altium *live driver* (DelphiScript half) needs a Windows + Altium 22+ box to validate.

### Notes
- Baselines at the first tagged release: package `0.1.0`, `schema_version = "1.0"`,
  `protocol_version = 1`.

[0.7.0]: https://github.com/tipoLi5890/akcli/commits/main
