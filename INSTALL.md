# Installing akcli

`akcli` is a **zero-runtime-dependency** Python package. It needs only **Python ≥ 3.11**
(for the stdlib `tomllib` module). There is nothing to compile and no EDA software to install.

- import package: `akcli`
- CLI command: `akcli` (long alias: `akcli`)
- Claude Code plugin / marketplace name: `akcli`

> **Not on PyPI yet** — install from source with one of the options below. Once a release is published,
> `pipx install akcli` / `pip install akcli` will also work.

## Requirements

- **Python ≥ 3.11.** Check with `python3 --version`.
  - macOS ships an older `python3` (often 3.9) — install a newer one with `brew install python@3.12`
    or from [python.org](https://www.python.org/downloads/). The `bin/akcli` wrapper auto-discovers
    `python3.13`/`3.12`/`3.11` if your default `python3` is too old.
- No other runtime dependencies. (Development/testing extras are opt-in: see below.)

## Option A — run from a clone (no install, zero deps)

Because there are no runtime dependencies, you can run straight from a checkout:

```bash
git clone https://github.com/tipoLi5890/akcli
cd akcli
./bin/akcli --help                  # self-locating wrapper; finds Python ≥ 3.11 for you
```

Add it to your PATH if you like:

```bash
export PATH="$PWD/bin:$PATH"        # exposes both `akcli` and `akcli`
```

Or invoke the module directly with any Python ≥ 3.11:

```bash
PYTHONPATH=src python3.12 -m akcli --help
```

## Option B — pipx from git (isolated, on your PATH)

`pipx` installs the CLI into an isolated environment and puts `akcli` on your PATH:

```bash
pipx install git+https://github.com/tipoLi5890/akcli
akcli --version          # package + protocol version
pipx upgrade akcli
pipx uninstall akcli
```

## Option C — pip from git (into a venv)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install git+https://github.com/tipoLi5890/akcli
```

## Use with AI coding agents

`akcli` is a plain CLI, so any agent that can run shell commands drives it once it's on PATH (install it
with one of the options above). The repo also ships **nine skills** under `skills/` that teach the agent
how to drive `akcli`: `akcli-circuit-design` (read/analyze/draw basics), `akcli-circuit-debug` (connectivity & tool
triage), `akcli-schematic-review` (severity-ranked design review), `akcli-schematic-authoring` (new circuits from an
op-list), `akcli-altium-interop` (working with Altium Designer), `akcli-parts-sourcing` (JLC/LCSC parts),
`akcli-jlcpcb-capabilities` (JLCPCB manufacturing limits), and `akcli-design-calc` (standards-cited engineering
calculators via `akcli calc`). Codex
and OpenCode **auto-discover** any skill folder you drop into their skills directory — no plugin or extra
config. Run the `cp` commands below from a clone of this repo.

### Claude Code

Install the plugin — it bundles all nine skills and the slash commands:

```text
/plugin marketplace add tipoLi5890/akcli
/plugin install akcli@akcli
```

You get the nine skills and `/akcli:circuit-review`, `circuit-pinmap`,
`circuit-draw`, and `circuit-diff`, all calling `akcli`.

### Codex

`akcli` runs through Codex's built-in shell once it's on PATH. Install it as a **Codex plugin** —
it bundles all nine skills and the session hook (see [docs/codex-plugin.md](docs/codex-plugin.md)):

```bash
codex plugin marketplace add tipoLi5890/akcli   # or `add ./` from a clone
codex plugin install akcli@akcli
```

Or just drop the loose skill folders in (auto-discovered from `.agents/skills/`, no plugin needed):

```bash
mkdir -p ~/.agents/skills && cp -R skills/* ~/.agents/skills/    # user-global
# per-project instead: mkdir -p .agents/skills && cp -R skills/* .agents/skills/
```

### OpenCode

OpenCode auto-discovers skills too, and also reads Claude-compatible locations:

```bash
mkdir -p ~/.config/opencode/skills && cp -R skills/* ~/.config/opencode/skills/
# OpenCode also reads ~/.claude/skills/ and ~/.agents/skills/, so any of those works
```

### Cover all three at once

Claude Code reads `.claude/skills/`, Codex reads `.agents/skills/`, OpenCode reads both — so copy into
both:

```bash
for d in ~/.claude/skills ~/.agents/skills; do mkdir -p "$d" && cp -R skills/* "$d"/; done
```

### Or let the agent install it

Paste this into a running Codex / OpenCode session (fix the path) and it sets itself up:

```text
I cloned akcli at /ABS/PATH/akcli. Please:
1. Put akcli on PATH: run `pipx install git+https://github.com/tipoLi5890/akcli`
   (or add /ABS/PATH/akcli/bin to PATH); verify with `akcli --version`.
2. Install its bundled skills so you load them automatically: copy every folder under
   /ABS/PATH/akcli/skills/ into your skills directory
   (Codex: ~/.agents/skills/ ; OpenCode: ~/.config/opencode/skills/).
3. Read skills/akcli-circuit-design/SKILL.md first and use akcli for any Altium/KiCad schematic work.
```

### Project instructions (optional)

Add an `AGENTS.md` (repo root, or `~/.codex/AGENTS.md` / `~/.config/opencode/AGENTS.md` globally) so the
agent reaches for `akcli` by default:

```markdown
# Tooling
Altium/KiCad schematics live here. Use the `akcli` CLI to read, check, diff, or draw them
(e.g. `akcli read x.SchDoc`, `akcli check x.kicad_sch`). Prefer `akcli` over ad-hoc parsing.
```

> A native MCP server is on the roadmap; when it ships, register it via each agent's MCP config
> (Codex `~/.codex/config.toml` `[mcp_servers.*]`; OpenCode `opencode.json` `mcp`).

## Development install (tests / contributing)

Dev and test tools are an optional extra (`pytest`, `jsonschema`, `build`, `twine`); the runtime stays
dependency-free.

```bash
git clone https://github.com/tipoLi5890/akcli
cd akcli
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Build and validate the distribution locally:

```bash
python -m build
twine check dist/*
```

## Optional external tools

- **`kicad-cli` (KiCad 8.x):** enables an *optional secondary* ERC/netlist cross-check. `akcli` detects
  it via `PATH`; if it is absent, the pure-Python connectivity verifier is used and nothing breaks.
- **Altium Designer 22+ on Windows:** required only for the optional *live* write/draw driver. Offline,
  Altium files are read-only and need no Altium install.

## Verify your install

```bash
akcli --version          # package version + protocol_version
akcli doctor             # full environment report with a remediation hint per missing item
```

`akcli doctor` probes Python, the install itself, packaged schemas, `kicad-cli`,
libngspice (for `akcli sim`) and config discovery — each the same way the
features themselves discover them — and `--require kicad-cli,ngspice` turns it
into a CI gate (exit 1 when a named capability is missing). Only Python is a
hard requirement; everything else degrades gracefully. The bundled
**akcli-setup** skill walks an agent through the per-OS repairs.

If `akcli` reports it cannot find Python ≥ 3.11, install a newer interpreter (see Requirements) and
re-run; the wrapper will pick it up. See [SECURITY.md](SECURITY.md) for the untrusted-input threat
model before pointing the tool at files from unknown sources.
