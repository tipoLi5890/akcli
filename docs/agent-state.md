# Workspace state & the multi-agent contract

akcli is **stateless per invocation** by design: there is no daemon, no lock
service, no hidden database. Everything an agent (or a human) needs to resume
work — or to coordinate with *another* agent working in the same project —
lives in reviewable files. This page is the contract for where each kind of
state lives and how a new session picks it up.

## Where state lives

| State | File | Owner |
|---|---|---|
| **The design itself** | `*.kicad_sch` | KiCad / the draw pipeline |
| **What happened** (edit history) | `.akcli/journal.jsonl` | every write command |
| **Undo snapshots** | `.akcli/backups/<name>.bak…bakN` | every `--apply`/`--write` schematic edit (`draw`, `arrange`, `groups --frame`, `relink-symbols`, `jlc bom --fix`, `sim fit-diode`); `undo` swaps within the stack rather than growing it |
| **Policy & decisions** (grid, rails, waivers + reasons) | `akcli.toml` | you, in git |
| **Designer intent** (asserted netlist) | intent JSON (`nets --intent-snapshot`) | you, in git |
| **Audited component facts** | `datasheets/extracted/<MPN>.json` | `review facts add` |

The schematic file is the **single source of truth**: deterministic UUIDs and
byte-identical re-apply mean re-reading it is always a reliable way to recover
state. Everything in `.akcli/` is *derived* convenience (history, snapshots)
and **self-ignoring**: whichever command creates the directory drops a
`.gitignore` containing `*` inside it, so your own `.gitignore` never needs an
entry and `git status` stays clean without akcli touching a user file.
`akcli.toml`, intent files, and `datasheets/` are design decisions and belong
in git.

## The `.akcli/` directory

One per workspace (the directory holding the edited file):

- `journal.jsonl` — append-only JSONL write journal. Every write that reaches
  the file appends one entry: timestamp, command, target, status
  (`dry-run`/`applied`/`refused`), op-list sha256, op count, net-diff verdict,
  backup name. `plan` and `draw` also journal their dry runs; `arrange`,
  `undo`, `groups --frame`, `relink-symbols` and `jlc bom --fix` journal only
  when they apply. The `cmd` field is the journal's own name for the
  operation, which is what `akcli log --cmd NAME` filters on: `plan`, `draw`,
  `arrange`, `arrange-groups`, `groups-frame`, `undo`, `relink-symbols`,
  `jlc-bom-fix`. Size-capped with
  one rotation; corrupt lines are skipped; journaling never fails the parent
  command. Query with `akcli log`.
- `backups/` — the rotated undo stack (`<name>.bak` newest … `.bakN`, depth
  from `[project] backup_depth`, default 3). Walked by `akcli undo`
  (`--list`/`--steps`). Legacy stacks beside the file (pre-0.12) are still
  found when this directory holds none for the target.

## Design intent in the journal

Sessions do not identify themselves — the design is the shared object, not
the editors. What a later session needs is *why* an edit was made, so the
main write commands accept `--note TEXT`, recorded next to the mechanical
facts. `plan`, `draw`, `arrange` and `undo` take it today; the other
journaling writers (`relink-symbols`, `jlc bom --fix`, `groups --frame`)
record the mechanical facts only, so put the *why* for those into a commit
message or a waiver reason instead:

```
akcli draw board.kicad_sch --ops decoupling.json --apply --note "add 100n per rail pin (check --power finding POWER_NO_DECOUPLING)"
akcli log .
```

Concurrent *writes* are already safe without any coordination protocol: the
writer is atomic (temp → verify → `os.replace`), re-checks the file's sha256
against its read-time snapshot and refuses with `VERIFY_FAILED` if someone
else wrote in between, and refuses when the KiCad GUI holds the file open
(lock file, `--allow-open` overrides). The journal is append-only.

## Session-resume ritual

A fresh session (new agent, new conversation, next morning) reconstructs
context from files — never from memory of a previous conversation:

1. `akcli log .` — what did the last session(s) do here? Any refused writes?
   Read the `note:` lines for intent.
2. `akcli read board.kicad_sch` (or `query`/`nets`) — the current truth.
3. `akcli check board.kicad_sch --intent intent.json` — does the design still
   satisfy the asserted intent? (Create the snapshot with
   `akcli nets board.kicad_sch --intent-snapshot intent.json` at a known-good
   point.)
4. `akcli undo board.kicad_sch --list` — how deep is the rollback runway?

Anything worth telling the *next* session goes into a file it will read:
a `--note` on the write, a waiver (with `reason`) in `akcli.toml`, an updated
intent snapshot, or a fact in the datasheet store. Do not park design context
in conversation memory — files are the only channel every agent shares.

## What deliberately does NOT exist

- **No free-form context blob** (`.akcli/context.md` or similar). Mutable
  scratch state would fight the derive-everything-from-files worldview and
  duplicate the harness's own memory. Design rationale belongs in reviewable,
  diffable artifacts: intent JSON, waiver reasons, notes, facts.
- **No cross-workspace/global state.** Each workspace's `.akcli/` is
  self-contained; deleting it loses history and undo, never the design.
