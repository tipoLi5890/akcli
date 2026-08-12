---
name: akcli-net-naming
description: >-
  Predict and debug WHICH name a net gets — akcli follows eeschema's driver
  priority (global label > power port > local label > hierarchical label >
  auto), so the canonical name is not "whichever label you placed". Use this
  skill when: choosing a label scope while authoring an op-list; a net in
  `akcli nets`/`sim`/`diff` output carries an unexpected name; a sim probe,
  intent snapshot or contract rule misses a net you can see; a net shows
  several names (aliases); migrating pre-0.21 files where the canonical pick
  changed (e.g. a local MID becoming the global VOUT); or the user asks
  "why is this net called X" / "網名" / "命名".
---

# akcli-net-naming — which name a net gets, and why

One net can touch several name sources at once: a local label, a global
label, a power symbol, a hierarchical port. Exactly one becomes the
**canonical** name (what `nets`/`diff`/`sim`/netlists print); every other
explicit name survives as an **alias**. akcli resolves the tie the same way
eeschema does, so `kicad-cli sch export netlist` and akcli agree.

## The driver ladder (KiCad sources)

Higher wins; equal rank tie-breaks alphabetically:

| rank | driver | note |
|---|---|---|
| 6 | global label | beats everything |
| 5 | power symbol's net (`+3V3`, `GND`) | beats a local label |
| 4 | local label | |
| 3 | hierarchical label / Altium port | BELOW local — a child sheet's own local label outranks its port name |
| 2 | Altium sheet entry | |
| 0 | none | synthetic auto-name from sorted membership (`net_<hash>`) |

Altium sources keep Altium's own rule instead: labels beat power ports
unless the project sets `PowerPortNamesTakePriority`.

Consequences you must design for:

- **A local label on a power rail does not rename the rail.** Tagging the
  `+3V3` net `VDD_MCU` gives you net `+3V3` with alias `VDD_MCU`.
- **Want to force a name? Use a global label** — it outranks power symbols
  too. (It also merges with every same-named global label in the tree, so
  force names deliberately.)
- **Sheet paths prefix local/hierarchical names only** (`/child/X`);
  global- and power-driven names stay bare.
- Never write a synthetic `net_<hash>` name into an intent/contract file —
  it changes whenever membership changes.

## Old names keep working

Aliases are not second-class: `check --contract` rules, `intent` files and
`sim --probe`/`--gnd` resolve a net by canonical name, alias or source
name. A pre-0.21 intent file that says `MID` still matches the net now
canonically called `VOUT`. Only the *printed* name changed — see
`docs/migration-1.0.md` before pinning names in new files.

## Debugging a surprising name

```bash
akcli nets board.kicad_sch --json
```

shows every net with `name`, `aliases` and `source_names` — if your label
is in `aliases`, a higher-ranked driver took the canonical slot.

```bash
akcli net board.kicad_sch --why R1.2
```

explains one pin's attachment: which net it landed on, how it attached
(wire end / label / junction), and the nearest name sources with distances
— the fastest way to see *which* label actually drives the net a pin
joined.

```bash
akcli net board.kicad_sch VOUT
```

prints the members and aliases of one net (works with an alias too).

## Authoring rules of thumb

- Naming a signal: one **local** label on the pin-adjacent wire is enough;
  don't also add a global one unless you *want* tree-wide merging.
- Naming a rail: place the power symbol and stop — extra descriptive local
  labels become aliases, which is fine for humans but means your sim decks
  and netlists will print the power name.
- Cross-sheet nets: the parent sheet pin ↔ child hierarchical label pair
  carries connectivity; the child's *displayed* net name follows the ladder
  (a local label inside the child outranks the port name).
- After every `draw --apply`, read the names back with `akcli nets` before
  writing intent/contract files or sim probes against them.
