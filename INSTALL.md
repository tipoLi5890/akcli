# Installing altium-kicad-cli

`altium-kicad-cli` is a **zero-runtime-dependency** Python package. It needs only **Python ≥ 3.11**
(for the stdlib `tomllib` module). There is nothing to compile and no EDA software to install.

- import package: `altium_kicad_cli`
- CLI command: `akcli` (long alias: `altium-kicad-cli`)
- Claude Code plugin / marketplace name: `altium-kicad`

> **Not on PyPI yet** — install from source with one of the options below. Once a release is published,
> `pipx install altium-kicad-cli` / `pip install altium-kicad-cli` will also work.

## Requirements

- **Python ≥ 3.11.** Check with `python3 --version`.
  - macOS ships an older `python3` (often 3.9) — install a newer one with `brew install python@3.12`
    or from [python.org](https://www.python.org/downloads/). The `bin/akcli` wrapper auto-discovers
    `python3.13`/`3.12`/`3.11` if your default `python3` is too old.
- No other runtime dependencies. (Development/testing extras are opt-in: see below.)

## Option A — run from a clone (no install, zero deps)

Because there are no runtime dependencies, you can run straight from a checkout:

```bash
git clone https://github.com/tipoLi5890/altium-kicad-cli
cd altium-kicad-cli
./bin/akcli --help                  # self-locating wrapper; finds Python ≥ 3.11 for you
```

Add it to your PATH if you like:

```bash
export PATH="$PWD/bin:$PATH"        # exposes both `akcli` and `altium-kicad-cli`
```

Or invoke the module directly with any Python ≥ 3.11:

```bash
PYTHONPATH=src python3.12 -m altium_kicad_cli --help
```

## Option B — pipx from git (isolated, on your PATH)

`pipx` installs the CLI into an isolated environment and puts `akcli` on your PATH:

```bash
pipx install git+https://github.com/tipoLi5890/altium-kicad-cli
akcli --version          # package + protocol version
pipx upgrade altium-kicad-cli
pipx uninstall altium-kicad-cli
```

## Option C — pip from git (into a venv)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install git+https://github.com/tipoLi5890/altium-kicad-cli
```

## Use with AI coding agents

`akcli` is a plain CLI, so any agent that can run shell commands drives it once it's on PATH (install it
with one of the options above). The repo also ships **six skills** under `skills/` that teach the agent
how to drive `akcli`: `circuit-design` (read/analyze/draw basics), `circuit-debug` (connectivity & tool
triage), `schematic-review` (severity-ranked design review), `schematic-authoring` (new circuits from an
op-list), `altium-interop` (working with Altium Designer), and `parts-sourcing` (JLC/LCSC parts). Codex
and OpenCode **auto-discover** any skill folder you drop into their skills directory — no plugin or extra
config. Run the `cp` commands below from a clone of this repo.

### Claude Code

Install the plugin — it bundles all six skills and the slash commands:

```text
/plugin marketplace add tipoLi5890/altium-kicad-cli
/plugin install altium-kicad@altium-kicad
```

You get the six skills and `/altium-kicad:circuit-review`, `circuit-pinmap`,
`circuit-draw`, and `circuit-diff`, all calling `akcli`.

### Codex

`akcli` runs through Codex's built-in shell once it's on PATH. Install it as a **Codex plugin** —
it bundles all six skills and the session hook (see [docs/codex-plugin.md](docs/codex-plugin.md)):

```bash
codex plugin marketplace add tipoLi5890/altium-kicad-cli   # or `add ./` from a clone
codex plugin install altium-kicad@altium-kicad
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
I cloned altium-kicad-cli at /ABS/PATH/altium-kicad-cli. Please:
1. Put akcli on PATH: run `pipx install git+https://github.com/tipoLi5890/altium-kicad-cli`
   (or add /ABS/PATH/altium-kicad-cli/bin to PATH); verify with `akcli --version`.
2. Install its bundled skills so you load them automatically: copy every folder under
   /ABS/PATH/altium-kicad-cli/skills/ into your skills directory
   (Codex: ~/.agents/skills/ ; OpenCode: ~/.config/opencode/skills/).
3. Read skills/circuit-design/SKILL.md first and use akcli for any Altium/KiCad schematic work.
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
git clone https://github.com/tipoLi5890/altium-kicad-cli
cd altium-kicad-cli
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
akcli --help             # list all subcommands
```

If `akcli` reports it cannot find Python ≥ 3.11, install a newer interpreter (see Requirements) and
re-run; the wrapper will pick it up. See [SECURITY.md](SECURITY.md) for the untrusted-input threat
model before pointing the tool at files from unknown sources.
