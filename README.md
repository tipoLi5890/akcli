**English** · [繁體中文](.github/README.zh-Hant.md) · [简体中文](.github/README.zh-Hans.md)

# akcli

**akcli** (CLI command `akcli`, import package `akcli`) is a zero-dependency,
**KiCad-native AI design agent** — a Python toolkit and Claude Code plugin that lets an LLM agent
*author* a `.kicad_sch` from a JSON op-list (with net-diff safety rails and one-command undo), run
ERC / design / intent / BOM checks, **simulate on KiCad's bundled ngspice**, source real parts and
fetch datasheets, and **import Altium `.SchDoc` / `.SchLib` / `.PcbDoc`** — all with **no Altium or
KiCad installed**.

KiCad is the writable target; Altium files are imported into the same normalized model for analysis
(a Windows *live bridge* can also drive a running Altium instance). The result is a scriptable,
install-free design loop that an automation pipeline or an AI agent can drive end to end — from an
imported legacy schematic or a blank sheet, all the way to a simulated, part-sourced, order-ready board.

[![CI](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/akcli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Highlights

- **AI-agent native.** Ships as a Claude Code plugin with skills/commands, emits structured JSON with
  `schema_version`, and accepts a versioned op-list for deterministic, idempotent edits.
- **Net-diff safety rails.** Every `plan`/`draw` prints a before/after **net connectivity diff**
  (splits, merges, renames — matched by pin membership, never by name); `draw --apply --strict-nets`
  refuses a write that splits or merges a named net, and `akcli check --intent` asserts a
  design-intent netlist snapshot after any edit.
- **Simulate and assert.** `akcli sim` renders a schematic to a SPICE deck, runs it through KiCad's
  libngspice in a crash-isolated child, and turns `.meas` results into pass/fail findings you gate CI
  on — or emits the deck with `--deck-only` when no engine is installed.
- **Standards-cited calculators.** `akcli calc` answers 60 design questions (E-series, IPC-2221,
  via parasitics, I²C pull-ups, buck/boost, ...) and prints the formal reference with every result.
- **One normalized model.** KiCad `.kicad_sch` and Altium binary `.SchDoc` both parse into the
  same `Schematic`/`Pcb`/`Library` model, so every check, diff, and report is format-agnostic —
  KiCad is the writable target, Altium is imported.
- **Net inference you can trust.** A rebuilt net layer handles global same-name merges, junctions,
  T-junctions, and No-ERC markers — fixing the classic "same-named nets split into single-pin nets" bug.
- **Read-only on Altium, safe writes on KiCad.** Altium files are never modified offline; KiCad writes
  go through an atomic snapshot → temp → verify → replace pipeline with a pure-Python connectivity gate.
- **No EDA install required.** Pure stdlib OLE2/CFBF + Altium record decoding and an iterative KiCad
  S-expression parser. No Altium, no KiCad, no compiled extensions — just Python ≥ 3.11.
- **Zero runtime dependencies.** Standard library only (including `tomllib`). Easy to vendor, sandbox,
  or run in CI.

## Write KiCad schematics from an op-list

`akcli` writes KiCad schematics from a versioned JSON **op-list** (place components, wires, junctions,
labels, power ports, text, hierarchical `add_sheet`, rename/delete...; connectivity macros like
`connect_and_label` and `place_pwr_flag` expand to core ops). `akcli new` bootstraps a blank sheet to
draw into. Writes are surgical and idempotent (deterministic UUIDv5), guarded by a pure-Python
connectivity verifier **and a before/after net diff**, and require an explicit `--apply` (default is a
dry run). `akcli undo` reverts the last write from a rotated backup stack (`undo --list`/`--steps N`).

```bash
akcli plan board.kicad_sch --ops ops.json         # validate op-list, show changes + net diff
akcli draw board.kicad_sch --ops ops.json         # dry-run by default (no file written)
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets  # atomic write + verify + backup;
                                                  # refuses named-net splits/merges
```

`akcli relink-symbols board.kicad_sch` refreshes stale embedded `lib_symbols` from fresh
`.kicad_sym` libraries behind a net-equivalence safety gate. Altium *write/draw* is available only
through the optional Windows live driver (Altium 22+ running); offline, Altium is analysis-only.

## Run checks (ERC, power, pinmap, BOM, diff)

Run an electrical rule check and other design checks without opening any EDA tool:

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM + connectivity hygiene
akcli check  board.kicad_sch --intent intent.json # assert a design-intent netlist snapshot
akcli pinmap main.SchDoc -C akcli.toml # MCU pin -> net (+ optional expected table)
akcli diff   v1.SchDoc v2.SchDoc                   # net-membership diff, not name-based
```

Power/ground detection is **net-name + power-port based**, not purely electrical-type based, because
real boards are dominated by `Passive` pins — a naive type-only ERC produces a vacuous pass. Every
report prints a metadata header (passive-pin ratio, suppressed No-ERC count, unnamed-net count,
fractional-coordinate presence) so a clean result is never mistaken for an empty one. `--fail-on`
tunes the exit-severity gate (`never` always exits 0), and a checker-agnostic `[[waiver]]` config
table drops or demotes findings by code/refs (with the count surfaced in the header). Design-intent
files support per-net modes and `fnmatch` wildcard members; located findings carry `pos`/`anchors`
in JSON/SARIF.

## Simulate and assert

`akcli sim` turns a schematic into a SPICE deck, runs it through KiCad's bundled
**libngspice** (in a crash- and timeout-isolated child subprocess), and compares
the `.meas` results against pass/fail bounds you declare in a `sim.json` — a
failed assertion is a normal non-zero exit you can gate CI on. Components resolve
to SPICE devices through a first-hit-wins ladder (`Sim.*` KiCad fields → `models`
overrides → an R/C/L heuristic that leaves un-modellable parts loudly
`unmodeled`, never guessed). No ngspice installed? `--deck-only` still emits the
deck.

```bash
akcli sim board.kicad_sch --deck-only                  # emit the SPICE deck, no engine
akcli sim board.kicad_sch --sim board.sim.json         # run + assert, exit 1 on failure
akcli sim board.kicad_sch --sim board.sim.json --sweep temp=0,25,60   # corner matrix
akcli sim fit-diode --point 0.37@20m --name DBAT       # datasheet forward point -> .model
```

The engine is auto-discovered (macOS/Linux/Windows KiCad, or force one with
`AKCLI_NGSPICE`); `sim.json` bounds accept engineering notation (`25m`, `4.7k`)
and a lower + upper bound in one entry forms a two-sided window; `--sweep` re-runs
the asserts across a corner matrix; `--wave` writes a tidy CSV; a floating node is
auto-fixed with `.option rshunt`. `akcli sim fit-diode` fits a diode `.model` from
datasheet forward-voltage points and can write it back onto the schematic
(`--apply --write`), closing the datasheet → model loop with `jlc datasheet`. See
[docs/sim.md](docs/sim.md) for the full reference.

## Find JLCPCB / LCSC parts

`akcli jlc` searches the JLCPCB/LCSC catalog (stock, price tiers, Basic/Extended status) and
converts parts into KiCad libraries **in-process** (vendored MIT
[JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) core — no external tool to install;
see [Acknowledgments](#acknowledgments)).

```bash
akcli jlc search "0.1uF 0402 X7R"     # keyword / MPN / category search (needs network)
akcli jlc show   C7593                 # one part by LCSC C-number (--easyeda adds 3D/MPN metadata)
akcli jlc add    C2040 --3d            # LCSC part -> KiCad symbol + footprint + STEP
akcli jlc bom board.kicad_sch --qty 10 --csv order.csv   # stock/price check + JLCPCB upload CSV
akcli jlc datasheet board.kicad_sch --fetch              # datasheet PDFs for the whole BOM
```

## Engineering calculators

`akcli calc` bundles **60 offline calculators** — E-series snapping and resistor combinations
(IEC 60063), voltage dividers, LM317/FB regulator worst-case, IPC-2221 track width and clearance,
via parasitics, fusing current, AWG, microstrip/stripline impedance, RF attenuators, buck/boost
stages, LDO headroom, NE555, op-amp pairs, comparator hysteresis, envelope detectors, I²C pull-ups,
crystal load caps, thermal, battery life, resistor markings, and galvanic compatibility. **Every result prints its formal reference** (the standard, datasheet,
or textbook the formula comes from), and numerics are cross-checked in the test suite against
KiCad's pcb_calculator readings and published handbook values.

```bash
akcli calc list                                  # all calculators, grouped, with references
akcli calc rcombo target=1k series=E24           # synthesize 1 kΩ from stock E24 values
akcli calc trackwidth i=2 dtemp=10               # IPC-2221 width for 2 A
akcli calc i2c-pullup vdd=3.3 cb=100p mode=fast  # NXP UM10204 pull-up window
```

Inputs accept engineering notation (`4k7`, `100n`, `2M2`); `--json` returns
`{calc, inputs, results, reference}`, `--md` a paste-ready table, `calc batch`
runs a JSON job list, and `--ops` turns design results (dividers, regulator
feedback, filters, ...) into a ready `place_component` op-list. `akcli view`
serves ONE local dashboard for both worlds: `/calc` (auto-compute forms,
physical-style SVG diagrams, shareable URLs, op-list export) and `/live` (a
draw-timeline for a watched `.kicad_sch` with per-step ERC findings, diff
ghosting, and SSE push) — localhost-only, zero deps.

## Read KiCad files

The same CLI parses KiCad's S-expression formats with an explicit-stack (non-recursive) tokenizer
that is depth-, atom-, and node-bounded — so a malformed or hostile file can't blow the stack.

```bash
akcli read board.kicad_sch              # .kicad_sch -> normalized JSON
akcli net  board.kicad_sch              # net membership, shared net engine
```

KiCad pin electrical types are resolved from `lib_symbols` at read time (instance pins carry no type),
so ERC has the data it needs. The S-expression reader is version-tolerant — KiCad 7/8 are
fixture-tested and newer formats (9/10) read through the same path.

## Import Altium designs

`akcli` opens Altium binary files directly. It contains a hardened OLE2/CFBF (Compound File Binary
Format) container reader and an Altium record decoder — no Altium Designer, no Windows, no license.

```bash
akcli read   main.SchDoc        # parse a .SchDoc to normalized JSON
akcli net    main.SchDoc         # extract the netlist (net -> pins)
akcli component main.SchDoc U10    # one component's pins -> nets (needs a designator)
```

Supported Altium inputs: `.SchDoc` (schematic), `.SchLib` (symbol library — text-record symbols;
libraries containing binary symbol records are refused with exit 5, *unsupported*), `.PcbDoc` (board —
ASCII `Nets6`/`Components6`/`Classes6`/`Rules6` sections **plus the binary copper sections**
`Tracks6`/`Vias6`/`Arcs6`/`Pads6`; `Fills6`/`Regions6`/`Texts6`/`Polygons6` are skipped, not
mis-parsed). All Altium *file* access is **read-only** (the optional Windows live bridge drives a *running* Altium instance instead).

## Use with AI coding agents

`akcli` is a plain CLI, so any agent that can run shell commands can drive it once it is on PATH.
Commands emit structured JSON with `--json` (`read` and the checks carry a `schema_version`; `net` emits
a net array), and the op-list carries a `protocol_version`, so output stays machine-checkable and
idempotent. Note: when piping (`akcli … | head`) the shell reports the *pipe's* exit code, not akcli's —
use `set -o pipefail` if you branch on it.

- **Claude Code** — install the bundled plugin (below) for the `/akcli:circuit-review`,
  `circuit-pinmap`, `circuit-draw`, and `circuit-diff` commands plus nine skills: `akcli-circuit-design`
  (read/analyze/draw basics), `akcli-circuit-debug` (connectivity & tool triage), `akcli-schematic-review`
  (severity-ranked design review), `akcli-schematic-authoring` (new circuits from an op-list),
  `akcli-altium-interop` (working with Altium Designer), `akcli-parts-sourcing` (JLC/LCSC parts),
  `akcli-jlcpcb-capabilities` (manufacturer limits + KiCad fab-file handoff), `akcli-design-calc`
  (60 standards-cited engineering calculators via `akcli calc`), and `akcli-setup`
  (environment probe & repair via `akcli doctor`).
- **Codex** — install the bundled plugin (below): it packages all eight skills plus the session hook.
  Or drop the loose skill folders into `.agents/skills/` for auto-discovery. See
  [docs/codex-plugin.md](docs/codex-plugin.md).
- **OpenCode** — auto-discovers the bundled skills; drop them into its skills dir and let the agent
  shell out to `akcli`. See
  [INSTALL.md](INSTALL.md#use-with-ai-coding-agents) for the exact commands (and a copy-paste setup prompt).

A native MCP server is on the [roadmap](#roadmap).

## Install

Not on PyPI yet — install from source. Zero runtime dependencies; needs **Python ≥ 3.11** (for stdlib
`tomllib`):

```bash
# run from a clone, no install
git clone https://github.com/tipoLi5890/akcli
./akcli/bin/akcli --help        # wrapper auto-selects a Python ≥ 3.11

# or put the CLI on your PATH with pipx
pipx install git+https://github.com/tipoLi5890/akcli
akcli --version
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

## Roadmap

Shipped today (v0.6.x): KiCad write/draw from an 18-op + 9-macro vocabulary (hierarchical
`add_sheet`, net-diff safety rails, `new`/multi-level `undo`, output arbitrated against KiCad's own
netlister), ERC/power/BOM/diff/pinmap/**intent** checks with waivers and SARIF, **`akcli sim`**
(SPICE decks on KiCad's bundled ngspice, assertions, sweeps, datasheet-fitted models),
JLCPCB/LCSC part search + BOM purchasability + **datasheet fetch**, 60 standards-cited calculators,
the `view` dashboard, and version-tolerant Altium/KiCad readers (KiCad hierarchy, Altium
multi-sheet + binary copper). The forward plan (v0.8 → v1.0, with exit criteria) lives in
**[ROADMAP.md](ROADMAP.md)**. Headline items still ahead:

- Published JSON Schemas for `check`/`diff`/`pinmap` findings; machine-detectable lookup misses.
- Full **ERC pin-type conflict matrix** and a schematic-vs-PCB sync check.
- Pure-stdlib **SVG schematic rendering** and a generated pinout book.
- A GitHub **Action** gating schematic PRs (check + diff + intent + sim assertions).
- *Optional, demand-driven:* the Altium track — binary `.SchLib` decoder, remaining `.PcbDoc`
  sections, and the Windows **live driver** (scaffold pending validation).
- A native **MCP server** (deferred by decision; the plain CLI serves agents today).

---

## Acknowledgments

`akcli jlc` builds on (full attribution and license texts in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):

- **JLC2KiCadLib** by **TousstNicolas** (MIT) — LCSC → KiCad conversion core, vendored (see THIRD_PARTY_NOTICES).
- **jlcsearch** (tscircuit, MIT) and **jlcparts** (MIT) — part-search backend.
- **EasyEDA / LCSC / JLCPCB** — component data source.

---

## Contact

Questions, bugs, or feature requests: please [open a GitHub issue](https://github.com/tipoLi5890/akcli/issues).

---

## License

MIT © 2026 Li, ching yu. See [LICENSE](LICENSE); third-party attribution in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and the
security model in [SECURITY.md](SECURITY.md).
