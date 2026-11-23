# Stability contract

What akcli treats as public contract, where each surface is defined, which test gates it, and how
it may change.

Status: audit for the 1.0 contract freeze.

- Sections 1–3 describe the current tree.
- Section 4 is the deprecation policy, adopted 2026-10-12 and in force from 1.0.
- Section 5 lists the pre-freeze decisions; every item was reproduced on the CLI. Decisions that
  need a code change are scheduled for the contract-freeze batch (ROADMAP), not made here.

## 1. Contract surfaces

A surface is contract when a consumer (an agent, a CI job, a script) can depend on it across
releases. "Gate" names the test that fails on drift. "One-way" means the gate proves the document
matches the code but not that the document is complete.

| Surface | Source of truth | Published view | Gate |
|---|---|---|---|
| Commands and flags | `build_parser()` in `src/akcli/cli.py`, `src/akcli/commands/` | `akcli capabilities --json` (`commands`, `global_flags`); [cli-reference.md](cli-reference.md) | `tests/test_capabilities.py`: the manifest is introspected from the live parser and covers every subcommand. `tests/test_docs_conformance.py`: every fenced `akcli …` line in the docs resolves to a real subcommand and real flags (one-way). |
| Exit codes `0`–`8` | `EXIT` in `src/akcli/errors.py` | `capabilities` `exit_codes`; cli-reference "Exit codes"; SPEC §8 | `tests/test_errors.py` pins the nine values. `tests/test_capabilities.py` pins the manifest table. The two prose tables are not machine-compared. |
| Error codes | `ERROR_CODES`, `_CODE_EXIT`, `REMEDIATION` in `src/akcli/errors.py` | `capabilities` `error_codes`; cli-reference "Structured errors"; SPEC §3.1 | `tests/test_errors.py`: census, one exit mapping and one remediation per code. `tests/test_docs_code_census.py`: both doc recitals equal the registry, two-way. |
| `--json` error envelope | `src/akcli/cli.py` | cli-reference "Structured errors" | `tests/test_cli_json_errors.py`; `tests/test_json_version_stamps.py` |
| `protocol_version` families | `PROTOCOL_VERSIONS` in `src/akcli/contract.py` | section 3 | `tests/test_contract_registry.py`; `python3 tools/schemas.py check` |
| `schema_version` families | `SCHEMA_VERSIONS` in `src/akcli/contract.py` | section 3 | `tests/test_contract_registry.py`; `tests/test_json_version_stamps.py` (every JSON object payload carries a stamp) |
| Published schemas | `schemas/*.json`, mirrored in the wheel | `akcli schema <name>`; SPEC §3.12 | `tests/test_schema_exports.py` (mirror byte-identity), `tests/test_schema_cmd.py`, `tests/test_docs_conformance.py` (SPEC table, schema count) |
| Op vocabulary | `src/akcli/opspec.py` | `schemas/ops.schema.json`, `schemas/ops.capabilities.json`, `akcli ops list`; [op-list-authoring.md](op-list-authoring.md) | `python3 tools/schemas.py check`; `tests/test_ops_kit.py`; `tests/test_ops.py`; `tests/test_docs_conformance.py` (op and macro counts) |
| Journal entries | `src/akcli/journal.py`, `JOURNAL_VERSION` | `akcli log --json`; [agent-state.md](agent-state.md) | `tests/test_journal.py`; `tests/test_contract_registry.py` |
| Config keys | `src/akcli/config.py` | SPEC §3.11; `examples/akcli.toml.example` | `tests/test_docs_conformance.py`: every top-level table is in SPEC and in the example, and the example loads. Keys inside a table are not gated. |
| Finding codes | emit sites in `src/akcli/checks/`, `review/`, `sim/`, `commands/` | cli-reference; [review-rules.md](review-rules.md); [sim.md](sim.md) | See "Finding codes" below. |

### Commands and flags

- A documented invocation must exist. The reverse is not gated: a registered flag can ship
  without a cli-reference entry.
- stdout carries data and stderr carries logs. `--json` is accepted by every subcommand.

### Exit codes

`0` success, `1` findings at or above the threshold, `2` usage or config error, `3` parse error,
`4` unusable path, `5` unsupported format, `6` op-list or verify failure, `7` external tool or
network unavailable, `8` query miss. Each error code maps to exactly one exit code.

### Error envelope

With `--json`, a failing command that would otherwise leave stdout empty prints:

```json
{"schema_version": "1", "akcli_version": "<version>",
 "error": {"code": "<CODE>", "message": "<text>", "exit": 4, "remediation": "<text>"}}
```

`code` is an `ERROR_CODES` member or a pseudo-code named after an exit category (`USAGE`,
`FILE_NOT_FOUND`, `UNSUPPORTED_FORMAT`, …). Tests assert the envelope keys and the `code` and
`exit` values. The wording of `message` and `remediation` is pinned by no test: every error code
has a non-empty remediation, and that is all the census asserts.

### Published schemas

`akcli schema <name>` prints any of the 15 schemas in `schemas/`. The count includes
`ops.capabilities.json`, a capability matrix rather than a JSON Schema, because `akcli schema`
serves it and the wheel mirrors it.

| Name | Describes | Version pin |
|---|---|---|
| `schematic` | `read --json` export | `schema_version` const |
| `netlist` | net-membership document | `schema_version` const (no producer, see section 5) |
| `diff` | `diff --json` | `schema_version` string, no const |
| `pinmap` | `pinmap --json` | `schema_version` string, no const |
| `findings` | `check --json`, `review analyze --json` | `schema_version` string, no const |
| `draw-result` | `plan --json`, `draw --json` | `schema_version` const |
| `doc` | `doc --json` | `schema_version` const |
| `proposals` | `review propose --json` | `proposals_version` const |
| `datasheet-facts` | facts store files | `facts_version` const |
| `library-baseline` | the library baseline file (`library audit --write-baseline`) | `schema_version` const |
| `ops` | op-list input | `protocol_version` const |
| `ops.capabilities` | per-op executor support | none |
| `design-intent` | `check --intent` and `ops from-intent` input | `protocol_version` const |
| `sim` | `sim --sim` input | `protocol_version` const |
| `symbol-pins` | `library new-symbol` input | `protocol_version` const |

### Finding codes

A finding code is the `code` of a finding, the `[[waiver]]` key and the SARIF `ruleId`.

- **Gated rosters (two-way).** `REVIEW_*` and `FACTS_*` against review-rules.md; `SIM_*` against
  sim.md (`tests/test_docs_code_census.py`). A code cannot ship undocumented and a documented
  code cannot be absent from the code.
- **Ghost gate (one-way).** For every other family, a code named in the docs must exist in the
  code. The reverse is not enforced.
- **No roster.** `check` (`ERC_*`, `POWER_*`, `BOM_*`, `NET_*`, `PAIR_*`, `LAYOUT_*`, `INTENT_*`,
  `CONTRACT_*`, `LIB_*`, the integrity codes), `diff` (`DIFF_*`), `pinmap` (`PINMAP_*`), `verify`
  (`SCHPCB_*`), `library` (`FOOTPRINT_*`, `MODEL_*`, `SYMLIB_*`), `fab` (`FAB_*`, `ORDER_*`),
  `project check` (`PROJECT_*`) and `release preflight` (`RELEASE_*`, `KICAD_DRC_*`). Golden
  snapshots (`tests/golden/`) pin the codes that fire on the fixture boards; no test enumerates
  these families.

## 2. Not contract

These may change in any release.

- **Human-readable output.** Text and Markdown output, stderr logs, notes and warnings. Golden
  snapshots (`tests/golden/`) prove the output is deterministic for one build and one input; they
  are regenerated when wording changes.
- **Finding and error message text.** A finding's identity is its code plus its fingerprint, and
  fingerprints ignore wording (`tests/test_findings_schema.py`). No test pins the wording of an
  error `message` or `remediation`.
- **Which findings a design produces.** A minor release may add checks, review rules and default
  check families, and may retune a review heuristic or threshold. A review finding carries its
  `rule_version`. Pin the akcli version where a verdict must be reproducible.
- **Rendered SVG.** `akcli render` output is deterministic for one build and one input. Geometry,
  markup and styling are not stable across releases.
- **Python modules and import paths.** No Python API is declared. `tests/test_layering.py`
  constrains the internal layering, not module names or signatures. One import is documented
  (jlc.md, "Library use"); section 5 lists it.
- **Files under `.akcli/`.** Tool state (SPEC §10): file names, rotation, backup naming and the
  staging directory. Read the journal with `akcli log --json` and restore with `akcli undo`.
- **Caches.** The `jlc` response cache and fetched datasheet PDFs.
- **Network data.** Catalog prices, stock and availability returned by `akcli jlc`.
- **The `akcli view` dashboard.** Its HTML, local HTTP endpoints and event payloads.
- **Bytes of a written `.kicad_sch` across releases.** Re-applying an op-list is byte-identical
  within one build (`tests/test_op_idempotency_census.py`). Section 5 lists the cross-release
  question.

## 3. Versioning rules

### The policy

Restated from the versioning policy at the top of `CHANGELOG.md`, which defines three version
numbers.

**Package version** (SemVer; source of truth `pyproject.toml`).

- MAJOR: a backwards-incompatible change to the public CLI surface, the normalized data model or
  the on-disk JSON exports.
- MINOR: backwards-compatible new subcommands, flags, checks, readers or ops.
- PATCH: backwards-compatible bug fixes and internal changes.

**`schema_version`** (stamped on every `Schematic`/`Pcb`/`Library` JSON export; bumped
independently of the package version).

- A minor bump only adds optional fields. Consumers must ignore unknown keys.
- A major bump may remove or rename fields and coincides with a package MAJOR bump.

**`protocol_version`** (an integer; governs the op-list document).

- Bumped only on a breaking change to op shapes or the result object.
- A new optional op or optional op field does not bump it.
- Executors reject a higher major `protocol_version` with `PROTOCOL_MISMATCH` rather than
  guessing.

When in doubt, the policy prefers additive, backwards-compatible changes that leave the version
contracts untouched.

[migration-0.21.md](migration-0.21.md) applies one further rule: a change that can alter the
value of an existing field is a major schema bump even when the shape is unchanged.

### As implemented

The registry (`src/akcli/contract.py`) holds more families than the policy names. Values are
strings for schema families and integers for protocol families.

| `schema_version` family | Value | Field | Carried by | Schema |
|---|---|---|---|---|
| `schematic` | `2.1` | `schema_version` | `read`, `component`, `export`; the finding envelopes of `check`, `diff`, `pinmap`, `review analyze` | `schematic` (const) |
| `netlist` | `2.0` | `schema_version` | no command output | `netlist` (const) |
| `draw-result` | `1.0` | `schema_version` | `plan`, `draw` | `draw-result` (const) |
| `doc` | `1` | `schema_version` | `doc` | `doc` (const) |
| `capabilities` | `1.0` | `schema_version` | `capabilities` | none |
| `render` | `1.0` | `render_version` | `render` | none |
| `facts` | `1.0` | `facts_version` | facts store files | `datasheet-facts` (const) |
| `proposals` | `1.0` | `proposals_version` | `review propose` | `proposals` (const) |
| `bom-lock` | `1` | `schema_version` | `jlc bom --lock` lockfile | none |
| `library-baseline` | `1.0` | `schema_version` | the library baseline file | `library-baseline` (const) |
| `envelope` | `1` | `schema_version` | the error envelope and every other stamped payload (`net`, `nets`, `verify`, `gate`, `log`, `doctor`, `project info`, …) | none |

The `schema_version` key carries the value of the family that produced the payload, so a consumer
selects by command first and by stamp second.

| `protocol_version` family | Value | Document | Schema | Accepts |
|---|---|---|---|---|
| `oplist` | `2` | op-list (`plan`, `draw`, `ops validate`) | `ops` | exactly `2` |
| `design-intent` | `1` | `check --intent` | `design-intent` | exactly `1` |
| `symbol-spec` | `1` | `library new-symbol` | `symbol-pins` | exactly `1` |
| `contract-check` | `1` | `check --contract` | none | any value up to `1`; the field is optional |
| `sim` | `1` | `sim --sim` | `sim` | a positive integer up to `1` (higher is `PROTOCOL_MISMATCH`, zero or negative is `BAD_CONFIG`) |

`journal_version` is `1.0`, stamped on every journal entry and on `akcli log --json`.

Gate coverage of the pins: `tests/test_contract_registry.py` compares the `schematic`, `netlist`,
`doc`, `draw-result` and `library-baseline` consts with the registry; `ops.schema.json` is
generated from it;
`tests/test_symbol_pins_schema.py` pins `symbol-pins`. The `datasheet-facts`, `proposals`,
`design-intent` and `sim` consts are compared only through payload-validation tests that need the
optional `jsonschema` dev dependency.

## 4. Deprecation policy

**Status: adopted 2026-10-12.** It applies from 1.0 to every surface in section 1. Before 1.0,
a `0.x` MINOR release may still make a breaking change, listed under `### Changed` in
`CHANGELOG.md`.

1. **Removal only in a MAJOR release.** A command, flag, exit code, error code, JSON field, schema
   file, op, op field, config key or environment variable is never removed or repurposed in a
   MINOR or PATCH release.
2. **Minimum notice.** Before a MAJOR release removes it, an item has been deprecated for at least
   two MINOR releases and at least 6 months, whichever is longer.
3. **Announcement.** The release that starts the notice period lists the item under a
   `### Deprecated` heading in `CHANGELOG.md`, with its replacement. `--help` and
   cli-reference.md mark it deprecated.
4. **Runtime notice.** Using a deprecated command, flag, config key or environment variable prints
   one line on stderr, `deprecated: <item>; use <replacement>`. stdout, `--json` output and the
   exit code are unchanged. `-q` suppresses the line.
5. **Behaviour during the notice period.** The deprecated item keeps its exact behaviour.
6. **JSON fields.** A deprecated field stays present with its meaning until the next major bump
   of its `schema_version` family. Its replacement is added beside it in a minor bump.
7. **Codes.** An error or finding code is never reused with another meaning. A retired code stays
   registered until the next MAJOR release. A new exit code is a MAJOR change.
8. **Protocols.** A `protocol_version` bump coincides with a package MAJOR release. Within a
   major, a deprecated op or field is still accepted and `akcli ops validate` reports it.

One deprecated alias exists today: `--exit-zero`, documented as a deprecated alias for
`--fail-on never` on `check`, `diff`, `pinmap`, `library audit`, `library lint-symbols`,
`project check`, `fab check` and `gate`. It prints no runtime notice (section 5).

## 5. Decisions before the 1.0 freeze

Each item states what the CLI does today, then the decision. Items that need a code change are
not changed here.

### Behaviour

1. **`project info` / `project check` on a path that is not a project.** A file of another type,
   an empty directory and a missing directory all return `PROJECT_FILE_MISSING`, exit `4`. A
   wrong-type file is arguably a usage error (exit `2`), and a missing directory is the generic
   `FILE_NOT_FOUND` case elsewhere.

   **Decision (2026-10-12):** a file of another type becomes a usage error (exit `2`); a
   missing path becomes `FILE_NOT_FOUND` (exit `4`); `PROJECT_FILE_MISSING` stays for a directory
   that holds no project file.

   ```bash
   akcli project info notes.txt
   akcli project check missing-dir
   ```

2. **`akcli log --limit 0` returns every entry.** So does a negative value. The flag is
   documented as "show only the newest N entries".

   **Decision (2026-10-12):** a value below `1` becomes a usage error (exit `2`).

   ```bash
   akcli log . --limit 0 --json
   ```

3. **`BRIDGE_BUSY` and `BRIDGE_TIMEOUT` are registered but never raised.** Both have an exit
   mapping and a remediation hint. Removing them is possible before 1.0 without a notice period;
   after 1.0 it is a MAJOR change under section 4.

   **Decision (2026-10-12):** both codes are removed before 1.0.
4. **`#PWR` references are allocated per file.** A two-sheet design written by one
   `akcli draw --apply` carries `#PWR01` in both files. On that design kicad-cli 10.0.4
   `sch export netlist` prints its annotation-errors warning (it does not once one reference is
   renamed, nor on a single-sheet design), and akcli's own reader prints
   `warning: duplicate designator '#PWR01'` on stderr on every read. `check --integrity` reports
   no finding, because `#` references are not counted across sheets, and kicad-cli `sch erc`
   lists no violation for it.

   **Decision (2026-10-12):** allocation becomes tree-aware before 1.0. Until then `#`
   references stay out of the cross-sheet duplicate count.
5. **`--exit-zero` has no runtime notice and is not deprecated everywhere.** It prints nothing on
   stderr. On `verify`, `sim`, `review facts verify`, `review testbench`, `jlc bom` and
   `jlc datasheet` it is the only report-mode switch (no `--fail-on`) and is not marked
   deprecated. `review analyze` has `--fail-on` only.

   **Decision (2026-10-12):** where `--fail-on` exists, `--exit-zero` gains the section 4
   runtime notice before 1.0. Where it is the only report-mode switch it stays supported and is
   not deprecated.
6. **The long output flag has two spellings.** `-o` works everywhere; the long form is `--out` on
   most subcommands and `--output` on `export`, `render` and `expected`.

   **Decision (2026-10-12):** `--out` is added to `export`, `render` and `expected`;
   `--output` stays accepted there.

### Versioning

7. **Protocol acceptance differs per family** (table in section 3). The CHANGELOG policy says
   executors reject a *higher* version; the op-list, design-intent and symbol-spec loaders refuse
   any other version, lower included, while the contract and sim loaders refuse only a higher one.

   **Decision (2026-10-12):** the per-family acceptance in section 3 is the contract. The
   CHANGELOG policy wording changes to "reject a version they do not support".
8. **The CHANGELOG policy names one schema family and one protocol.** The registry holds several
   of each (section 3). Whether the minor/major rules apply to each family separately is
   unstated. The policy ties a major schema bump to a package MAJOR bump; the `2.0` schema and
   the op-list protocol `2` shipped in 0.21, a MINOR release of the `0.x` series.

   **Decision (2026-10-12):** the minor/major rules apply to each family separately. The
   tie between a major family bump and a package MAJOR release applies from 1.0.
9. **The `netlist` family has no producer.** The registry pins it at `2.0` and
    `netlist.schema.json` requires `source_format`. No command emits that shape: `net --json` and
    `nets --json` stamp the `envelope` version and carry `source`.

    **Decision (2026-10-12):** the family and its schema are removed before 1.0 unless a command
    emits the shape by then.
10. **`testbench_version` lives outside the registry.** `review testbench --json` stamps a
    constant defined in `src/akcli/review/testbench.py`; the registry gates do not see it.

    **Decision (2026-10-12):** it moves into the registry before 1.0.

### Gate coverage

11. **Finding codes have no roster outside review and sim.** At audit time the `BOM_*`, `DIFF_*`,
    `FAB_*`, `LAYOUT_*`, `PINMAP_*` and `POWER_*` families each emit codes that no gated document
    names. Decide whether every finding code is frozen contract, which needs a two-way roster per
    family, or only the gated families.

    **Decision (2026-10-12):** only families with a two-way roster are frozen contract. An
    unrostered family may gain or rename codes in a MINOR release until it has one.
12. **One-way and missing gates.** No test fails when a registered flag has no cli-reference
    entry, when a key inside a config table is undocumented, when an exit-code prose table
    drifts, or when an environment variable (`KICAD_CLI`, `AKCLI_NGSPICE`, `AKCLI_JOURNAL`,
    `AKCLI_JLC_BASE_URL`, `AKCLI_JLC_CACHE`, `AKCLI_JLC_CACHE_STALE`, `AKCLI_DATASHEET_DIR`) is
    added or renamed. One wording difference exists today: the manifest describes exit `4` as
    "file not found", while cli-reference also covers an unreadable path.

    **Decision (2026-10-12):** an environment-variable roster and a flag ↔ cli-reference gate
    land before 1.0. Config keys and the exit-code prose stay ungated.

### Unstated

13. **Identity across releases.** Node uuids are UUIDv5 values seeded from content (SPEC §8,
    risk 7). No test pins the seeds across releases, so whether an op-list re-applied by a newer
    akcli reproduces the same uuids is not promised.

    **Decision (2026-10-12):** from 1.0 the seeds are stable within a MAJOR series, pinned by a
    test that lands before the freeze.
14. **Plugin surface.** Slash-command and skill names (`commands/`, `skills/`) are counted by the
    docs gate but covered by no stability statement.

    **Decision (2026-10-12):** not contract; names may change in a MINOR release.
15. **Python API.** jlc.md documents one import, `from akcli.parts import search` (`search`,
    `get`, `JlcNetworkError`). No other import is documented and no test marks a module public.
    Decide whether that module is contract or the section goes.

    **Decision (2026-10-12):** not contract. The CLI is the only supported interface; jlc.md
    labels the import accordingly before 1.0.
