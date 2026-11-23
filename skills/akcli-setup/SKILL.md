---
name: akcli-setup
description: >-
  Verify and repair the akcli environment before (or when) anything misbehaves —
  run `akcli doctor` to probe Python, the akcli install, packaged schemas,
  kicad-cli, libngspice, config discovery and (opt-in) jlc network reachability,
  then apply the per-OS remediation for whatever is missing. Use this skill when:
  setting up akcli on a new machine or CI runner; a command fails with
  KICAD_CLI_MISSING, NGSPICE_MISSING, exit 7, "command not found", or import
  errors; `akcli sim` cannot find an engine; every `lib_id` is SYMBOL_NOT_FOUND;
  `jlc` cannot reach the network; or
  the user asks to "check the environment" / "安裝" / "環境檢查" / "設定 akcli".
  Triggers on keywords: setup, install, doctor, environment, kicad not found,
  ngspice missing, SYMBOL_NOT_FOUND, KICAD_SYMBOL_DIR, KICAD_CLI, AKCLI_NGSPICE,
  PATH, pipx, PEP 668.
---

# akcli-setup — probe first, then repair exactly what is missing

Never guess at an environment problem: `akcli doctor` probes most optional
capabilities **the same way the features themselves discover them**, so its
verdict matches what the failing command actually saw. (Exception: `pdftotext`
— see the table below.)

## (1) Probe

```bash
akcli doctor                       # offline report: python/akcli/schemas/kicad-cli/ngspice/symbols/config/workspace
akcli doctor --network             # also probe the jlc endpoint (only networked check)
akcli doctor --json                # machine-readable: {checks: {name: {ok, detail, hint}}, ok}
```

No project here yet? `akcli project new` scaffolds a complete one in one pass
(`.kicad_pro`/`.kicad_sch`/`.kicad_pcb`/lib tables/`.gitignore` — the board is
an empty container, a scaffold so pcbnew finds a file instead of a prompt;
akcli writes no PCB content); if a bare `.kicad_sch` already exists without the
rest, `akcli project init` completes it in place instead:

```bash
akcli project new demo --title "Demo board"   # from scratch
akcli project init .                          # complete an existing .kicad_sch
akcli project check demo                      # confirm the result is a coherent project (0 findings on a fresh scaffold)
```

Every `MISSING` row prints its own remediation hint. Only **python** is a hard
requirement — kicad-cli / ngspice / network / config are optional capabilities
that specific features need:

| Capability | Needed by | Without it |
|---|---|---|
| `kicad-cli` | advisory ERC after `draw --apply`, `view live` SVG, parity tests | everything else works; those degrade gracefully |
| `ngspice` (libngspice) | `akcli sim` execution | `sim --deck-only` still emits the SPICE deck |
| `symbols` | `akcli pins` / `bbox` / `place_component` resolving a bare `Device:R` | every unqualified `lib_id` is `SYMBOL_NOT_FOUND` until you pass `--symbols` explicitly |
| `network` | the `jlc` family only | all analysis/authoring/sim stays offline |
| `pdftotext` (poppler) | `review facts verify` quote checks | verify still runs; quoted facts report `FACTS_QUOTE_UNVERIFIED` (NOTE) instead of a real text match — note: unlike the other rows, `akcli doctor` does not currently probe `pdftotext`; check for the binary manually (e.g. `pdftotext -v`) |
| `config` (akcli.toml) | `pinmap` MCU pin, rails, waivers, custom grid | defaults apply |
| `workspace` (advisory) | hygiene of the CWD as a schematic workspace | never CI-gating; flags legacy pre-0.12 beside-the-file `*.bak` stacks, leftover `~*.lck` GUI locks (stale ones force `--allow-open`), and a pre-0.14 `.akcli/` no `.gitignore` covers (0.14+ roots self-ignore) |

## (2) Repair — per capability

**akcli itself missing / wrong version**
```bash
pipx install akcli-kicad                               # PATH-managed install from PyPI
pip install akcli-kicad                                # inside a venv
pipx install git+https://github.com/tipoLi5890/akcli   # development head
# or run straight from a clone with zero install:
./akcli/bin/akcli --version
```
The distribution is `akcli-kicad`; the import package and CLI command stay
`akcli`. `pip install` into a system Python often fails with
*externally-managed-environment* (PEP 668) — prefer pipx or a venv; never
suggest `--break-system-packages` unless the user owns that Python.

**kicad-cli missing** — install KiCad ≥ 10 from kicad.org (macOS: the app bundle
carries it at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`; Linux:
the `ppa:kicad/kicad-10.0-releases` package, since distro builds can be older; Windows: `C:\Program Files\KiCad\<ver>\bin\`). If it is
installed somewhere unusual, point the env var instead of editing PATH:
```bash
export KICAD_CLI=/path/to/kicad-cli
```

**ngspice missing** — KiCad bundles libngspice, so installing KiCad usually fixes
both rows at once (Linux may need `libngspice0` / `libngspice0-kicad` explicitly).
Override discovery with `AKCLI_NGSPICE=/path/to/libngspice.{dylib,so,dll}`;
`AKCLI_NGSPICE=off` disables the engine deliberately (CI decks-only mode).

**symbols not resolving** — a stock KiCad install needs no setup: `sym-lib-table`
addresses the bundled libraries as `${KICAD*_SYMBOL_DIR}/...`, variables KiCad
compiles in rather than exports, so akcli derives them from the located
`kicad-cli` (macOS bundle, Linux `/usr/share/kicad/symbols`, Windows
`<install>\share\kicad\symbols`). Only if `akcli doctor` still reports `symbols`
MISSING — KiCad installed somewhere it cannot infer — set the variable by hand,
matching whichever one your `sym-lib-table` actually uses:
```bash
export KICAD10_SYMBOL_DIR=/path/to/symbols   # an env value always wins over the derivation
```

**network unreachable** — only `jlc` cares. Corporate proxy / offline CI is fine:
everything else runs. `AKCLI_JLC_BASE_URL` points at a self-hosted mirror; cached
responses under `~/.cache/akcli/jlc` keep working when stale-serving kicks in.

**config not found** — optional. Create `akcli.toml` next to the schematic when
`pinmap`/rails/waivers/grid are needed (the legacy `altium-kicad-cli.toml` name
is still honored). See `docs/cli-reference.md` for the schema; a bad key fails
EVERY command in that directory tree with `BAD_CONFIG` — that error means "fix
or remove the toml", not "reinstall".

## (3) Gate (CI / scripted setup)

`--require` turns the report into an assertion — exit 1 lists what is missing:

```bash
akcli doctor --require kicad-cli,ngspice     # sim + parity capable runner
akcli doctor --require symbols               # before any draw/pins job using bare lib_ids
akcli doctor --require network               # before a jlc-dependent job
```

Wire it as the FIRST step of any CI job or agent session that needs an optional
capability, so failures say "ngspice missing" instead of a mid-task exit 7.

## Failure-mode quick table

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: NGSPICE_MISSING`, exit 7 | no libngspice | install KiCad or `AKCLI_NGSPICE=...`; verify `akcli doctor --require ngspice` |
| `SYMBOL_NOT_FOUND` on EVERY `lib_id` (`Device:R` included) | KiCad symbols not reachable from here | `akcli doctor --require symbols` — it resolves `Device:R` down the real path and prints the file; if MISSING, export `KICAD*_SYMBOL_DIR` (above) |
| `ERROR: NETWORK: ...`, exit 7 | jlc endpoint unreachable | proxy/mirror via `AKCLI_JLC_BASE_URL`; cached data may still serve |
| advisory ERC silently absent after `--apply` | kicad-cli not found | it is advisory by design; install KiCad or set `KICAD_CLI` to enable |
| `BAD_CONFIG: unknown key(s)` on EVERY command | stale/typo'd `akcli.toml` up the tree | `akcli doctor` shows which config file was discovered — fix or delete it |
| `akcli: command not found` right after pipx install | shell PATH cache | `hash -r` or reopen the shell; `pipx ensurepath` once |

## When NOT to use this skill

Design-time failures — findings from `check`, `SIM_ASSERT_FAIL`, net splits,
op-list validation errors — are circuit problems, not environment problems:
use akcli-circuit-debug / akcli-schematic-review instead. This skill ends when
`akcli doctor` (with the run's `--require` set) exits 0.
