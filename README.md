**English** · [繁體中文](.github/README.zh-Hant.md) · [简体中文](.github/README.zh-Hans.md)

# altium-kicad-cli

**altium-kicad-cli** (CLI command `akcli`, import package `altium_kicad_cli`) is a zero-dependency
Python toolkit and Claude Code plugin that reads **Altium binary `.SchDoc` / `.SchLib` / `.PcbDoc`**
**and** **KiCad `.kicad_sch` / `.kicad_sym` / `.kicad_pcb`** with **no Altium or KiCad installed**,
runs ERC / power / pinmap / BOM / diff checks from the command line, and draws KiCad schematics from a
JSON op-list. It is built for AI coding agents.

It reads both formats into one normalized model and *analyzes* them — parse, check, diff, and draw —
giving you a scriptable, install-free workflow that an automation pipeline or an LLM agent can drive.

[![CI](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/altium-kicad-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Highlights

- **Two formats, one model.** Altium binary `.SchDoc` and KiCad `.kicad_sch` both normalize into the
  same `Schematic`/`Pcb`/`Library` model, so every check, diff, and report is format-agnostic.
- **No EDA install required.** Pure stdlib OLE2/CFBF + Altium record decoding and an iterative KiCad
  S-expression parser. No Altium, no KiCad, no compiled extensions — just Python ≥ 3.11.
- **Zero runtime dependencies.** Standard library only (including `tomllib`). Easy to vendor, sandbox,
  or run in CI.
- **Net inference you can trust.** A rebuilt net layer handles global same-name merges, junctions,
  T-junctions, and No-ERC markers — fixing the classic "same-named nets split into single-pin nets" bug.
- **Read-only on Altium, safe writes on KiCad.** Altium files are never modified offline; KiCad writes
  go through an atomic snapshot → temp → verify → replace pipeline with a pure-Python connectivity gate.
- **Net-diff safety rails.** Every `plan`/`draw` prints a before/after **net connectivity diff**
  (splits, merges, renames — matched by pin membership, never by name); `draw --apply --strict-nets`
  refuses a write that splits or merges a named net, and `akcli check --intent` asserts a
  design-intent netlist snapshot after any edit.
- **AI-agent native.** Ships as a Claude Code plugin with skills/commands, emits structured JSON with
  `schema_version`, and accepts a versioned op-list for deterministic, idempotent edits.
- **Standards-cited calculators.** `akcli calc` answers 60 design questions (E-series, IPC-2221,
  via parasitics, I²C pull-ups, buck/boost, ...) and prints the formal reference with every result.

## Read Altium files

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
mis-parsed). All Altium access is **read-only**.

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

## Run checks (ERC, power, pinmap, BOM, diff)

Run an electrical rule check and other design checks without opening any EDA tool:

```bash
akcli check  main.SchDoc                          # ERC-lite + power + BOM + connectivity hygiene
akcli check  board.kicad_sch --intent intent.json # assert a design-intent netlist snapshot
akcli pinmap main.SchDoc -C altium-kicad-cli.toml # MCU pin -> net (+ optional expected table)
akcli diff   v1.SchDoc v2.SchDoc                   # net-membership diff, not name-based
```

Power/ground detection is **net-name + power-port based**, not purely electrical-type based, because
real boards are dominated by `Passive` pins — a naive type-only ERC produces a vacuous pass. Every
report prints a metadata header (passive-pin ratio, suppressed No-ERC count, unnamed-net count,
fractional-coordinate presence) so a clean result is never mistaken for an empty one.

## Write KiCad schematics from an op-list

`akcli` writes KiCad schematics from a versioned JSON **op-list** (place components, wires, junctions,
labels, power ports, text, rename/delete...; connectivity macros like `connect_and_label` and
`place_pwr_flag` expand to core ops). Writes are surgical and idempotent (deterministic UUIDv5),
guarded by a pure-Python connectivity verifier **and a before/after net diff**, and require an
explicit `--apply` (default is a dry run). `akcli undo` reverts the last write.

```bash
akcli plan board.kicad_sch --ops ops.json         # validate op-list, show changes + net diff
akcli draw board.kicad_sch --ops ops.json         # dry-run by default (no file written)
akcli draw board.kicad_sch --ops ops.json --apply --strict-nets  # atomic write + verify + backup;
                                                  # refuses named-net splits/merges
```

`akcli relink-symbols board.kicad_sch` refreshes stale embedded `lib_symbols` from fresh
`.kicad_sym` libraries behind a net-equivalence safety gate. Altium *write/draw* is available only
through the optional Windows live driver (Altium 22+ running); offline, Altium is analysis-only.

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

## Use with AI coding agents

`akcli` is a plain CLI, so any agent that can run shell commands can drive it once it is on PATH.
Commands emit structured JSON with `--json` (`read` and the checks carry a `schema_version`; `net` emits
a net array), and the op-list carries a `protocol_version`, so output stays machine-checkable and
idempotent. Note: when piping (`akcli … | head`) the shell reports the *pipe's* exit code, not akcli's —
use `set -o pipefail` if you branch on it.

- **Claude Code** — install the bundled plugin (below) for the `/altium-kicad:circuit-review`,
  `circuit-pinmap`, `circuit-draw`, and `circuit-diff` commands plus eight skills: `circuit-design`
  (read/analyze/draw basics), `circuit-debug` (connectivity & tool triage), `schematic-review`
  (severity-ranked design review), `schematic-authoring` (new circuits from an op-list),
  `altium-interop` (working with Altium Designer), `parts-sourcing` (JLC/LCSC parts),
  `jlcpcb-capabilities` (manufacturer limits to design against), and `design-calc`
  (60 standards-cited engineering calculators via `akcli calc`).
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
git clone https://github.com/tipoLi5890/altium-kicad-cli
./altium-kicad-cli/bin/akcli --help        # wrapper auto-selects a Python ≥ 3.11

# or put the CLI on your PATH with pipx
pipx install git+https://github.com/tipoLi5890/altium-kicad-cli
akcli --version
```

Claude Code plugin (marketplace name `altium-kicad`):

```text
/plugin marketplace add tipoLi5890/altium-kicad-cli
/plugin install altium-kicad@altium-kicad
```

Codex plugin (same name `altium-kicad`):

```bash
codex plugin marketplace add tipoLi5890/altium-kicad-cli   # or `add ./` from a clone
codex plugin install altium-kicad@altium-kicad
```

Full details, per-agent setup, and troubleshooting in [INSTALL.md](INSTALL.md).

## Roadmap

Shipped today: Altium `.SchDoc`/`.SchLib` and KiCad `.kicad_sch` read (version-tolerant, KiCad
**hierarchical sheets included**), net inference, ERC/power/BOM/diff/pinmap/intent checks, KiCad
write/draw (17-op vocabulary + 9 macros incl. delete/move/rename and multi-unit placement, net-diff
safety rails, output verified against KiCad's own netlister), embedded-library relink, and
JLCPCB/LCSC part search with order-CSV export. The full milestone plan (v0.2 → v1.0, with exit
criteria per milestone) lives in **[ROADMAP.md](ROADMAP.md)**. Headline items still ahead:

- Altium `.PcbDoc` remaining **binary** sections (fills/regions/texts/polygons) — ASCII sections
  and binary copper (pads/tracks/vias/arcs) read today.
- **Offline Altium writing** and Altium-authoritative ERC/netlist (today these need the live driver).
- **Hierarchical / multi-sheet** KiCad *writing* (the writer is flat-only; the reader follows hierarchy).
- Altium **live driver** for Windows + Altium 22+ (the DelphiScript half is a scaffold pending validation).
- A native **MCP server**.

---

## Acknowledgments

`akcli jlc` builds on (full attribution and license texts in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):

- **JLC2KiCadLib** by **TousstNicolas** (MIT) — LCSC → KiCad conversion core, vendored (see THIRD_PARTY_NOTICES).
- **jlcsearch** (tscircuit, MIT) and **jlcparts** (MIT) — part-search backend.
- **EasyEDA / LCSC / JLCPCB** — component data source.

---

## Contact

Questions, bugs, or feature requests: please [open a GitHub issue](https://github.com/tipoLi5890/altium-kicad-cli/issues).

---

## License

MIT © 2026 Li, ching yu. See [LICENSE](LICENSE); third-party attribution in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and the
security model in [SECURITY.md](SECURITY.md).
