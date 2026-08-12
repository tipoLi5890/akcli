# Migrating to the 0.21 contract

Three external contracts break together in this release, on purpose: one
migration boundary instead of three. Nothing else about the CLI surface
changes.

| What | Before | After |
|---|---|---|
| Canonical net name | first explicit name the reader saw | eeschema's driver-priority winner |
| op-list `protocol_version` | `1` | `2` (four field renames) |
| export `schema_version` | `1.3` | `2.0` |

Read the section that matches what you consume.

## 1. Canonical net names follow eeschema

A net can legitimately carry more than one explicit name — a power port and a
local label on the same copper, or a global and a local label. akcli used to
report whichever name it read first. It now ranks them the way
`kicad-cli sch export netlist` does (global label > power port > local label >
hierarchical label > sheet pin > auto-name, alphabetical tie-break), so the
name akcli prints is the name eeschema prints.

The corpus fixture `tests/fixtures/kicad/board_v8.kicad_sch` is the worked
example: the divider mid-point carries both `MID` and `VOUT`, and

```bash
akcli net board_v8.kicad_sch --json
```

now reports

```json
{ "name": "VOUT", "aliases": ["MID"] }
```

where it previously reported `"name": "MID", "aliases": ["VOUT"]`. No
connectivity changed — the same three pins are on the same net — only the
name chosen to represent it.

**What you have to do: usually nothing.** Every displaced name is still
carried on the net:

- `aliases` — the explicit names that did not win.
- `source_names` — every explicit name on the net, winner included.

Everything that matches nets BY NAME matches against that full set, not just
the canonical one, so an old name keeps working in:

- `akcli check --intent` net tables,
- `akcli check --contract` net rules,
- sim probes and assertions naming a net.

What does change is text you may have pinned: a stored `akcli net --json`
snapshot, a diff report, or a script that greps for a net name in
human-readable output. Re-take those snapshots. If a name matters to you as a
name, put it on the net as a *global* label or a power port — that is the
rung the ladder picks first, and it now pins the outcome deterministically.

## 2. op-list protocol 2 — four field renames

Set `"protocol_version": 2` in the envelope and rename these fields. Values,
types and behaviour are identical; only the spelling moved.

| op | v1 field | v2 field | What to do |
|---|---|---|---|
| `add_net_label` | `orientation` | `rotation` | rename the key; enum `{0,90,180,270}` unchanged |
| `set_component_parameters` | `reference` | `new_designator` | rename the key; it is still "the new reference designator" |
| `set_component_parameters` | `instance_references` | `instance_designators` | rename the key; same `{"<instance path>": "<REF>"}` map, now spelled the way `place_component` already spelled it |
| `rename_net` | `from` / `to` | `old_name` / `new_name` | rename both keys; still net names |

Two look-alikes that did **not** change:

- `route_net` and `connect_and_label` keep `from`/`to` — theirs are pin
  refs, not net names, and were never the ambiguous pair.
- `connect_and_label` keeps its own `orientation`. It expands into an
  `add_net_label` that carries `rotation`, so the emitted core op speaks
  protocol 2 either way.

### What the errors look like

An op-list still declaring the old protocol is refused at the envelope:

```
ERROR: [-1] PROTOCOL_MISMATCH: protocol_version 1 != 2
```

Once the envelope says `2`, a leftover v1 field name is named explicitly
rather than guessed at:

```
ERROR: [0] OP_UNSUPPORTED: add_net_label: field 'orientation' was renamed to 'rotation' in protocol 2
```

The rename hint is scoped per op, so `connect_and_label`'s `orientation` and
`route_net`'s `from`/`to` are never mislabelled as stale.

Validate a migrated file without touching a target:

```bash
akcli ops validate ops.json --json
```

and re-check any op skeleton you generate against the live vocabulary:

```bash
akcli ops template add_net_label
```

Re-applying a migrated op-list to a sheet it was already applied to is still
byte-identical — node uuids are seeded from op CONTENT (designator, anchor,
label name), and none of the four renamed fields is part of a seed.

## 3. Export `schema_version` 2.0

`schema_version` is stamped on every `Schematic` / `Pcb` / `Library` JSON
export and on the `check` / `review` / `diff` finding envelopes. It moves
`1.3` → `2.0`: a major bump, because the net-name change above can rename a
field's VALUE in existing output, which an additive minor bump is not allowed
to do.

The document SHAPE is unchanged — no key was added, removed or retyped
between 1.3 and 2.0. So a consumer has two honest options:

- **Upgrade.** Accept `"2.0"` wherever it accepted `"1.3"`. Nothing else to
  do, as long as it reads net names as data rather than as identifiers.
- **Branch on the stamp.** Read `schema_version` first and keep a 1.x path if
  you have stored older exports to compare against. Comparing a 1.3 export
  with a 2.0 export of the SAME file may show net-name differences that are
  not design changes.

`akcli.contract.SCHEMA_VERSIONS` is the single source of the value, and
`schemas/schematic.schema.json` / `schemas/netlist.schema.json` pin it as a
`const` — a mismatch between the two fails CI, so the stamp can be trusted.
