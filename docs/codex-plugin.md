# Codex plugin (`.codex-plugin/`)

This repo ships as an **OpenAI Codex plugin** in addition to a Claude Code plugin. A Codex
plugin bundles the six `akcli` skills (and the Python-version session hook) so Codex can
discover, install, and load them as one unit — no manual copying of skill folders.

Spec reference: <https://developers.openai.com/codex/plugins/build>

## What was added

| File | Role |
|---|---|
| `.codex-plugin/plugin.json` | The Codex **plugin manifest** (required). Declares name/version/metadata and points at the bundled skills and hooks. |
| `.agents/plugins/marketplace.json` | A **repo-scoped marketplace catalog** so Codex can find and install this plugin straight from the repo. |

Nothing else moved. The manifest **reuses the existing directories** — the Codex skill
format (`skills/<name>/SKILL.md` with YAML `name` + `description` frontmatter) is identical
to the Claude skill format, and the Codex hook format matches `hooks/hooks.json` as-is. So
one set of skills serves Claude Code, Codex, and OpenCode.

## Plugin layout

```
altium-kicad-cli/                 # ← plugin root
├── .codex-plugin/
│   └── plugin.json               # Codex manifest (this is what makes it a Codex plugin)
├── .agents/plugins/
│   └── marketplace.json          # repo-scoped catalog for `codex plugin marketplace add`
├── skills/                       # 7 skills, shared with Claude & OpenCode
│   ├── circuit-design/SKILL.md   #   core read/analyze/draw mechanics (start here)
│   ├── schematic-authoring/SKILL.md
│   ├── schematic-review/SKILL.md
│   ├── circuit-debug/SKILL.md
│   ├── altium-interop/SKILL.md
│   ├── parts-sourcing/SKILL.md
│   └── jlcpcb-capabilities/SKILL.md
├── hooks/
│   └── hooks.json                # SessionStart Python≥3.11 advisory (portable one-liner)
├── .claude-plugin/               # Claude Code manifest + marketplace (unchanged)
└── … (src/, bin/, commands/, docs/, schemas/, …)
```

## The manifest (`.codex-plugin/plugin.json`)

Required fields: `name` (kebab-case, stable), `version` (semver), `description`.

Component pointers are `./`-prefixed paths resolved from the plugin root:

- `"skills": "./skills/"` — the folder of skill subdirectories. Each skill's `name` becomes a
  callable identifier inside Codex.
- `"hooks": "./hooks/hooks.json"` — lifecycle hooks. The bundled `SessionStart` hook only
  prints a warning when the interpreter is < 3.11; it writes nothing.

This plugin declares no `mcpServers` and no `apps` — `akcli` is a plain CLI Codex drives
through its built-in shell, so there is no MCP server or app integration to point at.

The `interface` object controls how the plugin looks on the install surface (`displayName`,
`shortDescription`, `category`, `capabilities`, `defaultPrompt`, `brandColor`, …). It is
optional metadata; removing it does not change behavior.

## Skills ⇄ Claude slash commands

Claude Code exposes four slash commands (`/circuit-review`, `/circuit-diff`,
`/circuit-pinmap`, `/circuit-draw`) from `commands/`. Codex plugins have no separate
`commands` concept — **skills are the callable units**. The same workflows are covered by the
skills, which Codex loads on demand:

| Claude command | Codex skill that covers it |
|---|---|
| `/circuit-review` | `schematic-review` |
| `/circuit-diff` | `schematic-review` (revision-diff step) / `circuit-design` |
| `/circuit-pinmap` | `schematic-review` (pinmap step) / `circuit-design` |
| `/circuit-draw` | `schematic-authoring` |

## Install & test

`akcli` must be on `PATH` first (see [INSTALL.md](../INSTALL.md)); the plugin ships the skills
that teach Codex to drive it, not the CLI itself.

**From the repo (repo-scoped marketplace):**

```bash
codex plugin marketplace add ./                 # run from the repo root; reads .agents/plugins/marketplace.json
codex plugin marketplace list                   # confirm "altium-kicad" is listed
codex plugin install altium-kicad@altium-kicad  # <plugin>@<marketplace>
```

**From GitHub (once pushed):**

```bash
codex plugin marketplace add tipoLi5890/altium-kicad-cli
codex plugin install altium-kicad@altium-kicad
```

Enable/disable and per-plugin state live in `~/.codex/config.toml` (set `enabled = false` to
turn it off). Installed plugins cache under
`~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`.

Verify it loaded by asking Codex to run a task the skills own, e.g. *"Read `main.SchDoc` and
summarize the rails"* — Codex should reach for the `circuit-design` skill and call `akcli`.

## Keeping the two manifests in sync

`name` (`altium-kicad`) and `version` (`0.1.0`) are duplicated across
`.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`. When you bump the version or
edit shared metadata, update **both** so Claude Code and Codex agree.
