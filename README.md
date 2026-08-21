<div align="center">

<img src="docs/assets/hero.png" alt="akcli — KiCad-native design CLI for humans and AI agents" width="820">

<p><strong>AI-native schematic design, purpose-built for KiCad — a zero-dependency (pure-stdlib Python) CLI.</strong></p>

<p>
  <a href="https://github.com/joanbono/awesome-kicad"><img src="https://awesome.re/mentioned-badge.svg" alt="Mentioned in Awesome KiCad"></a>
  <a href="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml"><img src="https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/dependencies-0-brightgreen" alt="Zero runtime dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT">
</p>

<p><strong>English</strong> · <a href=".github/README.zh-Hant.md">繁體中文</a> · <a href=".github/README.zh-Hans.md">简体中文</a> · <a href=".github/README.ja.md">日本語</a></p>

</div>

---

**akcli** is a zero-dependency Python CLI for **AI-native schematic design on KiCad** — a scriptable
design loop that you or **any AI agent** can drive end to end (bundled plugins/skills ship for Claude
Code, Codex, and OpenCode, but a shell is all it takes). Author a `.kicad_sch` from a JSON op-list,
run ERC / design-review / BOM / schematic ↔ PCB checks, simulate on ngspice, and source real
JLCPCB/LCSC parts. Existing Altium `.SchDoc` / `.SchLib` / `.PcbDoc` / `.PcbLib` designs import
read-only — an on-ramp into the KiCad flow, which is where all development happens.

## Install

Zero runtime dependencies; needs **Python ≥ 3.11** (for stdlib `tomllib`). The distribution is
`akcli-kicad`; the command is `akcli`:

```bash
pipx install akcli-kicad        # or: pip install akcli-kicad
akcli --version

# or run straight from a clone, no install
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # wrapper auto-selects a Python ≥ 3.11
```

Claude Code plugin (marketplace name `akcli`):

```text
/plugin marketplace add tipoLi5890/akcli
/plugin install akcli@akcli
```

Codex plugin (same name `akcli`):

```bash
codex plugin marketplace add tipoLi5890/akcli   # or `add ./` from a clone
codex plugin install akcli@akcli
```

Full details, per-agent setup, and troubleshooting in [INSTALL.md](INSTALL.md).

## Quickstart

Read a design, check it, and draw into it — straight from the shell:

```bash
akcli read  board.kicad_sch --summary                            # normalized JSON, context-budgeted
akcli check board.kicad_sch                                      # ERC-lite + power + BOM + connectivity + integrity
akcli draw  board.kicad_sch --ops ops.json                       # dry-run: shows changes + net diff
akcli draw  board.kicad_sch --ops ops.json --apply --strict-nets # atomic write + verify + backup
akcli undo  board.kicad_sch                                      # revert the last write
```

Every write is a dry run by default; `--apply` goes through an atomic snapshot → temp → verify →
replace pipeline with a pure-Python connectivity gate, and `akcli undo` reverts from a rotated backup
stack. Altium files are read-only offline.

## What it does

One normalized model behind every command, so each check, diff, and report works on a KiCad
`.kicad_sch` — and just the same on an imported Altium `.SchDoc`:

| Command | What it does |
|---|---|
| `read` · `net` · `component` · `pins` | Parse KiCad or Altium into one normalized JSON model; query nets (whole-net or pin-level: `net --at` / `--same` / `--why`), components, and pin coordinates. |
| `new` · `plan` · `draw` · `ops` · `arrange` | Author a `.kicad_sch` — a root file, a child sheet, or **several sheets in one atomic transaction** (the `sheets` envelope + per-op `sheet` routing, with run backups and `undo --run`) — from a versioned **22-op + 10-macro** JSON op-list, behind a net-diff safety gate evaluated over the **whole sheet tree** and one-command `undo`; `ops from-intent` compiles a design document (declared nets + components) straight into an op-list. Symbols resolve from `--symbols`, the config paths, then the project and global `sym-lib-table`. |
| `check` · `verify` · `diff` · `pinmap` | ERC-lite + power + BOM + document-integrity + intent / contract checks, schematic ↔ PCB equivalence, net-membership diff, MCU pin → net map. |
| `review` | Advisory, confidence-graded design review across six detector families (signal / validation / pcb / emc / domain / gerber); DNP parts satisfy no rule. |
| `sim` | Render to a SPICE deck and assert on KiCad's bundled ngspice; sweep corners; fit diode models from datasheet points. Component values are normalized, or refused rather than guessed. |
| `jlc` | Search JLCPCB/LCSC (stock, price, Basic/Extended), convert a part into a KiCad library in-process (typed pins, real `Value`), fetch datasheets. |
| `calc` | **60** standards-cited offline calculators (E-series, IPC-2221, impedance, I²C pull-ups, buck/boost…), each citing its reference. |
| `library` · `fab` · `release` | Library-workspace audit/repair, symbol authoring from a pin table (`new-symbol` — you give the pin table, akcli computes the coordinates) with a standalone lint (`lint-symbols`) and a coincident-pin check that gates the write, versioned fab-profile checks, and a release preflight that writes a traceable manifest. |
| `gate` | Iteration-weight bundle of the four checks re-run after every GUI edit round (the full `check` set, intent assertions, sch ↔ PCB `verify`, BOM-lock drift — offline by default) — zero-argument in a configured project; `release preflight` stays the release-weight gate. |
| `export` · `render` · `doc` · `view` | Netlist export (Protel / KiCad / CSV — the KiCad form carries the UUID sheetpath identity a re-import needs), pure-stdlib SVG render, a Markdown pinout book, and a localhost `/calc` + `/live` dashboard. |
| `capabilities` · `schema` | The self-describing surface manifest — whole, per-section (`--section`), or per-command (`--command draw`) — and every packaged JSON Schema printed byte-for-byte for client-side validation. |

Two headline examples — a design review and a part search:

```bash
akcli review analyze board.kicad_sch --profile deep --pcb board.kicad_pcb   # advisory findings + evidence
akcli jlc search "0.1uF 0402 X7R"                                           # JLCPCB/LCSC catalog (needs network)
```

## Use with AI agents

`akcli` is a plain CLI, so any shell-capable agent can drive it. Commands emit structured JSON with
`--json` (carrying a version stamp — `schema_version`, or a family field like `proposals_version` /
`facts_version`), each op-list carries a `protocol_version`, and `akcli
capabilities` self-describes the entire CLI surface in one JSON document — and every error code ships a
machine-readable `remediation` hint.

- **Claude Code** — install the bundled plugin for 8 `/akcli:circuit-*` commands (review, pinmap,
  draw, diff, parts, sim, datasheet, release) plus 13 skills spanning design, review, authoring, Altium interop, parts
  sourcing, calculators, and release gating.
- **Codex** — install the bundled plugin, or drop the loose skill folders into `.agents/skills/` for
  auto-discovery. See [docs/codex-plugin.md](docs/codex-plugin.md).
- **OpenCode** — auto-discovers the bundled skills; see
  [INSTALL.md](INSTALL.md#use-with-ai-coding-agents) for the exact commands.

## Why akcli

- **Zero runtime dependencies.** Standard library only (including `tomllib`) — easy to vendor, sandbox,
  or run in CI.
- **Purpose-built for KiCad.** An iterative KiCad S-expression parser and byte-stable writer at the
  core; Altium designs come in read-only through pure-stdlib OLE2/CFBF record decoding, straight into
  the same KiCad flow. No compiled extensions.
- **Byte-identical re-apply.** Deterministic UUIDv5 + replace-in-place makes every edit idempotent —
  re-running an op-list produces the same bytes.
- **Connectivity is the only hard write gate.** Every `plan`/`draw` prints a before/after net diff
  (splits, merges, renames — matched by pin membership, never by name); `--strict-nets` refuses a
  write that splits or merges a named net.
- **Net inference you can trust.** A rebuilt net layer handles global same-name merges, junctions,
  T-junctions, and No-ERC markers — fixing the classic "same-named nets split into single-pin nets" bug.

## Optional external tools

"Zero-dependency" means zero Python **package** dependencies: `pip install` pulls in nothing, and the
entire core loop (read / plan / draw / check / diff / calc / render) runs on the standard library
alone. A few features can use something outside Python — always detected at runtime, never required
by the core loop:

| Feature | Uses | If missing |
|---|---|---|
| Advisory ERC second opinion, `view live` SVG | `kicad-cli` (local executable) | Skipped silently — non-fatal, results stay advisory. |
| `sim` execution (`--deck-only` needs nothing) | libngspice (bundled with KiCad) | `sim` exits with `NGSPICE_MISSING`; everything else unaffected. |
| Standard symbol nicknames (`Device:R`, `power:*`) | `kicad-cli` install location (derives `${KICAD*_SYMBOL_DIR}`) | Symbols resolve only from explicit `--symbols` / config paths. |
| `jlc` part search / datasheet fetch | Network | The **only** networked command family; all others are fully offline. |

`akcli doctor` probes each of these and prints per-OS remediation.

## Documentation

| Doc | Covers |
|---|---|
| [SPEC.md](docs/SPEC.md) | Data model, config tables, JSON schemas |
| [cli-reference.md](docs/cli-reference.md) | Every command and flag |
| [op-list-authoring.md](docs/op-list-authoring.md) | Op-list authoring bible (ops, macros, groups) |
| [design-integrity.md](docs/design-integrity.md) | Contracts, fab profiles, release preflight |
| [review-rules.md](docs/review-rules.md) | Design-review rule catalogue |
| [sim.md](docs/sim.md) | Simulation reference |
| [ROADMAP.md](ROADMAP.md) | Roadmap to v1.0 with exit criteria |

## Roadmap

**Shipped (v0.20.0):** KiCad write/draw from a 22-op + 10-macro vocabulary (hierarchical `add_sheet`,
writes into a child sheet of an existing hierarchy and across several sheets in one atomic
transaction, a net-diff safety rail evaluated over the whole sheet tree, per-instance references on
a reused sheet, `new` / multi-level `undo` / `undo --run`), net-preserving `arrange --groups` re-layout, the advisory `akcli review` engine (six
detector families, now including power-entry protection, plus a datasheet facts store, `propose` /
`tree` / `validate`), ERC / power / BOM / diff / pinmap / intent / contract checks with waivers
(SARIF from `check --format sarif` / `review report --format sarif`), read-only document `check --integrity`, schematic ↔ PCB `verify`, a project `library`
workspace, versioned `fab` profiles, a `release preflight` gate, `akcli sim` on KiCad's bundled
ngspice, JLCPCB/LCSC sourcing with datasheet fetch, 60 standards-cited calculators, pure-stdlib SVG
rendering that draws the real symbol artwork from KiCad `lib_symbols`, and version-tolerant
Altium/KiCad readers. Standard symbol nicknames resolve on a stock KiCad install: the compiled-in
`${KICAD*_SYMBOL_DIR}` paths are derived from the located `kicad-cli`, and `akcli doctor` reports
whether that resolution works here.

**Ahead (→ v1.0):** the contract-freeze audit. The first PyPI release (`pip install akcli-kicad`) shipped in 0.15.0.
*Deferred by decision:* a GitHub Action gating schematic PRs, the `view` waveform panel, and a native
MCP server (the plain CLI serves agents today). Full plan with exit criteria in
[ROADMAP.md](ROADMAP.md).

## Acknowledgments

`akcli jlc` builds on (full attribution in [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):

- **JLC2KiCadLib** by **TousstNicolas** (MIT) — LCSC → KiCad conversion core, vendored.
- **jlcsearch** (tscircuit, MIT) and **jlcparts** (MIT) — part-search backend.
- **EasyEDA / LCSC / JLCPCB** — component data source.

## Contact

Questions, bugs, or feature requests: please
[open a GitHub issue](https://github.com/tipoLi5890/akcli/issues).

## License

MIT © 2026 Li, ching yu. See [LICENSE](LICENSE); third-party attribution in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and the
security model in [SECURITY.md](SECURITY.md).
