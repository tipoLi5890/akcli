# KiCad format gotchas & safe editing

Everything here is a rule any layout/edit op **must** obey, and the reason a
naive regex over `(at ...)` corrupts a schematic. akcli's own write ops already
follow these rules; this page exists so that hand scripts, `sed`, and anyone
reading `akcli read --json` coordinates do too. Each item notes whether it is
**measured** on real files or **inferred** from the format.

## A1 — Names are `{...}`-escaped (measured)

A symbol or `lib_id` name escapes special characters with `{token}` tokens, so
the on-disk bytes differ from the logical name:

| on disk (`.kicad_sym` / `lib_id`) | logical name |
|---|---|
| `19-237{slash}R6GHBHC-A04{slash}2T` | `19-237/R6GHBHC-A04/2T` |
| `SMD1812P050TF{slash}30` | `SMD1812P050TF/30` |

Tokens include `{slash}`=`/`, `{dblquote}`, `{backslash}`, `{lt}`/`{gt}`,
`{colon}`, `{tab}`, `{return}`, `{newline}`, `{brace}`. **Compare and write
names in their escaped form**, or a component whose name contains `/` looks
"missing" when it is not. akcli normalizes names to the unescaped form on read
(so `library audit` / `relink` match correctly) and re-emits the escaped form on
write, so a subsequent KiCad save produces **zero diff**. The single source of
truth is `akcli.kicad_escape` (`escape_lib_id` / `unescape_string`).

Oracle: KiCad's own ERC reports `0` `lib_symbol_issues` for these parts —
matching only works after unescaping.

## A2 — Property `(at)` is an ABSOLUTE page coordinate (measured)

`(property "Reference" … (at X Y))` on a placed symbol is **not** relative to the
symbol origin — it is an absolute page coordinate. So moving a component means
translating the body `(at)` **and every property's `(at)` by the same delta**.
The single most common hand-script bug is moving only the body `(at)` and
leaving 100+ `Reference`/`Value` fields floating at their old spots.

Conversely, a placed instance's `(pin "N" (uuid …))` entries carry **no** `(at)`
— pin geometry comes from `lib_symbols` and follows the body `(at)` transform.
**Do not** add or move pin coordinates on an instance.

`akcli`'s `move_component` handles both correctly; prefer it (or the carry-aware
form below) over editing `(at)` by hand.

## A3 — The global lib-table can be a nested `(type "Table")` (measured)

KiCad 10's per-user global `fp-lib-table` / `sym-lib-table` may contain a single
**indirection** entry pointing at the bundled default table rather than the
libraries themselves:

```
(fp_lib_table (version 7)
  (lib (name "KiCad") (type "Table")
       (uri "/Applications/KiCad/KiCad.app/Contents/SharedSupport/template/fp-lib-table")))
```

A reader that does not **recursively expand `type "Table"`** (read the URI's
table, merge its entries) sees zero libraries even after loading the global
table, and every standard nickname (`Device`, `Connector`, `power`, …) looks
unregistered. akcli's `libtable.read_table()` expands the indirection (cycle and
depth guarded); `discover()` reads the platform global table
(`~/Library/Preferences/kicad/<ver>/` on macOS,
`~/.config/kicad/<ver>/` on Linux, `%APPDATA%/kicad/<ver>/` on Windows,
respecting `KICAD_CONFIG_HOME`). A nickname is an error only when **both** the
project and global tables lack it.

---

## Rigid, net-preserving re-layout

Because A1–A3 make hand-editing coordinates hazardous, use the ops that respect
them.

### `move_component` with `carry_labels` / `carry_wires`

By default `move_component` moves the body and its property fields but leaves
labels and wires where they were. A wire endpoint the move stranded trips the
connectivity gate (`DANGLING_ENDPOINT`, ERROR) and the write is refused; a
stranded *label* is invisible to that gate — what makes it loud is `draw`'s
plan-time net diff, printed by default but advisory unless `--strict-nets`.
Two optional booleans promote it to a
**rigid-body** relocation that takes connectivity along:

```json
{ "op": "move_component", "designator": "R1", "x_mil": 5000, "y_mil": 3000,
  "carry_labels": true, "carry_wires": true }
```

- `carry_labels` — every net label anchored on one of the part's pin tips is
  shifted by the **same** delta, so each pin keeps the label that names its net.
  One exception: on a tip shared with a *stationary* part's pin, one label per
  foreign pin is left behind, so the part that stays keeps its net name.
- `carry_wires` — a wire endpoint on a moved pin follows it; an endpoint on
  another part's pin stays put, so the segment stretches and both nets stay
  connected.

**Net-preserving guarantee.** With the label-on-pin connectivity pattern (no
cross-part wires — akcli's canonical style), a rigid translation *cannot* change
the netlist: every pin travels with the label that names it. `carry_labels`
makes that guarantee real for a single move; the connectivity verify still runs
after every apply and refuses the write on any connectivity ERROR (dangling
endpoint, duplicate uuid/designator, stacked power ports). It does not compare
netlists: for that, `draw --strict-nets` refuses when a split/merge touches a
named net, and `arrange --groups` refuses on any net change at all.

### `arrange --groups` — functional-block re-layout

`arrange --groups` relocates whole functional blocks into their own
shelf-packed regions with a wide channel between groups, for a human to refine.
The groups file maps a group name to the designators it owns (TOML or JSON):

```toml
# groups.toml — order sets top-to-bottom stacking on the page
[groups]
power    = ["U1", "C1", "C2", "C3"]
mcu      = ["U2", "Y1", "C4", "C5"]
frontend = ["U3", "R1", "R2"]
```

```
akcli arrange board.kicad_sch --groups groups.toml            # dry-run preview
akcli arrange board.kicad_sch --groups groups.toml --apply    # write (.bak + undo)
```

Each part (plus the power symbols riding on its pins) moves as a rigid bundle
via carried `move_component` ops, so the re-layout is net-preserving by
construction. Unlisted components fall into a trailing `(ungrouped)` block;
`--group-gap` and `--row-width` tune the spacing. Because it goes through the
standard draw pipeline, it re-verifies connectivity and refuses to write on any
net change, writes a `.bak`, and is reverted by `akcli undo`.

---

## KiCad is open? External writes are unsafe

While the KiCad GUI holds a document open it drops a `~<name>.lck` beside it, and
it **rewrites its own config/project files from memory on exit** — so any
external write is a losing race (a later GUI save overwrites it; a custom env var
written to `kicad_common.json` is cleared to `null`). akcli's write ops refuse a
locked target by default (`TARGET_LOCKED`, exit 6; override with `--allow-open`
and `File > Revert` afterwards). Hand scripts and `sed` have no such guard — gate
them on the same check:

```
akcli library check-lock .        # exit 6 if any KiCad file is open in the GUI
```

Use it before any external edit: `akcli library check-lock . && ./relayout.sh`.

---

## Rotating/mirroring a symbol in the GUI can silently reassign a label

A net label anchored **on a pin tip** stays at its coordinate when the symbol
under it is rotated or mirrored in eeschema — but the pins move. On a part
with symmetric pin geometry (any 2-pin passive rotated 180°, many
connectors), each pin lands exactly where the other was: both labels still
sit on pin tips, nothing looks wrong, ERC is clean — and the two nets have
silently swapped pins. This is a **state-comparison** failure mode; no
single-file lint can distinguish "always meant this pin" from "meant the
other one before the rotate" (a rotated symbol with a label on a pin is the
normal case, not a suspicious one). Catch it by comparing against a known
baseline after every GUI edit session, before trusting the netlist:

```
akcli diff before.kicad_sch after.kicad_sch    # e.g. before = git show HEAD:...
akcli verify board.kicad_sch board.kicad_pcb   # sch<->pcb net partition
akcli check board.kicad_sch --intent intent.json   # against a captured snapshot
```

`akcli diff` matches nets by membership, so the swap surfaces as
`DIFF_NET_MEMBERSHIP` changes (on the smallest 2-pin nets it may present as
one removed + one added net near the Jaccard threshold — every affected pin
is still listed). The intent flow (`akcli nets board.kicad_sch
--intent-snapshot` -> `check --intent`) names the exact missing/extra member
per net, but only if the snapshot was captured before the edit.

---

## 3D model path policy — pick per situation

`library repair --3d-path` rewrites **bare-relative** 3D model references (a
value that already starts with `${VAR}` or is absolute is left alone, so
switching an already-policy-managed library from one strategy to another is a
no-op); `--3d-path` accepts `absolute` or a `${VAR}`-style prefix. Each
strategy trades off differently (all three were hit in practice):

| strategy | resolves when | caveat |
|---|---|---|
| `${KIPRJMOD}` prefix | PCB opened inside its project | footprint **viewer is always blank** |
| custom env var (`${MY_3D}`) | wherever the var is set | **KiCad clears the var** in `kicad_common.json` on exit if it was running |
| `absolute` | any context | machine-bound; moving the repo needs a rewrite |

For a portable repo, `absolute` + a one-line move SOP is the pragmatic choice;
`library repair --3d-path absolute --apply` productizes it.
