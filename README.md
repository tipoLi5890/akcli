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
- **AI-agent native.** Ships as a Claude Code plugin with skills/commands, emits structured JSON with
  `schema_version`, and accepts a versioned op-list for deterministic, idempotent edits.

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
ASCII `Nets6`/`Components6`/`Classes6`/`Rules6` sections for now; binary pad/track sections are
refused loudly rather than mis-parsed). All Altium access is **read-only**.

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
akcli check  main.SchDoc                          # ERC-lite + power + BOM hygiene
akcli pinmap main.SchDoc -C altium-kicad-cli.toml # MCU pin -> net (+ optional expected table)
akcli diff   v1.SchDoc v2.SchDoc                   # net-membership diff, not name-based
```

Power/ground detection is **net-name + power-port based**, not purely electrical-type based, because
real boards are dominated by `Passive` pins — a naive type-only ERC produces a vacuous pass. Every
report prints a metadata header (passive-pin ratio, suppressed No-ERC count, unnamed-net count,
fractional-coordinate presence) so a clean result is never mistaken for an empty one.

## Write KiCad schematics from an op-list

`akcli` writes KiCad schematics from a versioned JSON **op-list** (place components, wires, junctions,
labels, power ports, text...). Writes are surgical and idempotent (deterministic UUIDv5), guarded by a
pure-Python connectivity verifier, and require an explicit `--apply` (default is a dry run).

```bash
akcli plan  ops.json --target board.kicad_sch     # validate op-list, show what would change
akcli draw  ops.json --target board.kicad_sch     # dry-run by default (no file written)
akcli draw  ops.json --target board.kicad_sch --apply   # atomic write + verify + backup
```

Altium *write/draw* is available only through the optional Windows live driver (Altium 22+ running);
offline, Altium is analysis-only.

## Find JLCPCB / LCSC parts

`akcli jlc` searches the JLCPCB/LCSC catalog and converts a part into a KiCad or Altium library
(conversion is delegated to the external `nlbn`/`npnp` tools — see [Acknowledgments](#acknowledgments)).

```bash
akcli jlc search "0.1uF 0402 X7R"     # keyword / MPN / category search (needs network)
akcli jlc show   C7593                 # one part by LCSC C-number
akcli jlc add    C7593                 # fetch + convert into a KiCad / Altium library
```

## Use with AI coding agents

`akcli` is a plain CLI, so any agent that can run shell commands can drive it once it is on PATH.
Commands emit structured JSON with `--json` (`read` and the checks carry a `schema_version`; `net` emits
a net array), and the op-list carries a `protocol_version`, so output stays machine-checkable and
idempotent. Note: when piping (`akcli … | head`) the shell reports the *pipe's* exit code, not akcli's —
use `set -o pipefail` if you branch on it.

- **Claude Code** — install the bundled plugin (below) for the `/altium-kicad:circuit-review`,
  `circuit-pinmap`, `circuit-draw`, and `circuit-diff` commands plus six skills: `circuit-design`
  (read/analyze/draw basics), `circuit-debug` (connectivity & tool triage), `schematic-review`
  (severity-ranked design review), `schematic-authoring` (new circuits from an op-list),
  `altium-interop` (working with Altium Designer), and `parts-sourcing` (JLC/LCSC parts).
- **Codex** — install the bundled plugin (below): it packages all six skills plus the session hook.
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

Shipped today: Altium `.SchDoc`/`.SchLib` and KiCad `.kicad_sch` read (version-tolerant), net inference,
ERC/power/BOM/diff/pinmap checks, KiCad write/draw, and JLCPCB/LCSC part search. The full milestone
plan (v0.2 → v1.0, with exit criteria per milestone) lives in **[ROADMAP.md](ROADMAP.md)**. Headline
items still ahead:

- Altium `.PcbDoc` **binary** sections (pads/tracks/vias/arcs/fills/regions) — ASCII sections read today.
- **Offline Altium writing** and Altium-authoritative ERC/netlist (today these need the live driver).
- **Hierarchical / multi-sheet** KiCad writing (flat-only today).
- Altium **live driver** for Windows + Altium 22+ (the DelphiScript half is a scaffold pending validation).
- A native **MCP server**.

---

## Acknowledgments

`akcli jlc` builds on (full attribution and license texts in
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):

- **nlbn** and **npnp** by **linkyourbin** (Apache-2.0) — LCSC → KiCad / Altium library conversion.
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
