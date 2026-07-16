---
name: akcli-altium-interop
description: >-
  Work with Altium Designer through the zero-dependency `akcli` CLI: parse Altium
  .SchDoc/.SchLib/.PcbDoc files without an Altium install, export normalized JSON and
  Protel/KiCad/CSV netlists, deliver schematic edits back into Altium (offline human
  draw instructions + importable netlist, or the optional Windows Altium 22+ live
  bridge), and migrate Altium designs to KiCad with netlist-diff verification. Use this
  skill whenever the task involves: reading, exporting, or converting an Altium file;
  getting a design out of Altium into another tool; getting changes into an Altium
  schematic; an Altium-to-KiCad migration or equivalence check; or
  the Altium live bridge. Triggers on keywords: Altium, Altium Designer, SchDoc, SchLib,
  PcbDoc, Protel netlist, OLE2/CFBF, Altium export, Altium import, migrate to KiCad,
  AD to KiCad, live bridge, DelphiScript, X2.EXE.
---

# akcli-altium-interop — Altium Designer in and out through `akcli`

This skill covers the Altium-specific interop surface of `akcli`: what Altium content
parses, how to get designs **out** (JSON / netlists), how to get changes **in** (offline
instructions or the optional Windows live bridge), and how to migrate AD → KiCad with
proof of equivalence. For general read/analyze/draw mechanics (op-list format, `plan`/
`draw`, checks, exit codes, config), **see the akcli-circuit-design skill** — do not
re-derive them here.

## What parses vs. what is refused (and the read-only guarantee)

`akcli` opens Altium OLE2/CFBF containers directly, on any OS, with **strictly
read-only** access — no code path modifies a `.SchDoc`/`.SchLib`/`.PcbDoc`, ever.

Parses:
- **`.SchDoc`** — components, pins (electrical-tip coordinates), designators,
  parameters, footprints, wires/junctions/net labels/power ports/ports/No-ERC markers
  → full inferred netlist. Hierarchical designs are followed: sheet symbols
  (RECORD 15/16) recurse into child `.SchDoc`s with per-instance namespaces and
  sheet-entry↔child-port pairing (Altium *Automatic* scope: ports merge globally
  only in designs without sheet symbols). `.PrjPcb` is accepted too — akcli reads
  the project's top sheet and honors `PowerPortNamesTakePriority`. Sheet-entry
  edge-position scale follows the documented convention; validating against a
  real AD hierarchical design is still pending — flag it when it matters.
- **`.SchLib`** — text-record symbol libraries (symbol names + pin counts).
- **`.PcbDoc`** — ASCII sections `Nets6`, `Components6`, `Classes6`, `Rules6`
  → nets, footprints, classes, rules; **plus binary copper**: `Tracks6`, `Vias6`,
  `Arcs6`, `Pads6` decode into `tracks`/`vias`/`arcs`/`pads` (coordinates in mils,
  Altium's native +Y-up frame, net indices resolved to names). Layouts were
  cross-validated item-by-item against KiCad's own Altium importer on real boards.
  `Fills6`/`Regions6`/`Texts6`/`Polygons6` are still skipped.
- **`.PcbLib`** — decodes each footprint's pads (number, position, size, drill, shape,
  rotation, NPTH vs plated) into the `FootprintDef` model. Undecoded primitives (silk,
  text, 3D bodies) surface as `UNSUPPORTED_PRIMITIVE` warnings rather than failing the
  read. Read-only, same as every other Altium input.

Refused loudly with `ERROR: ALTIUM_UNSUPPORTED` and **exit 5** (unsupported, not
corrupt): binary `.SchLib` symbol records, and an unknown record type inside a
binary `.PcbDoc` copper section (truncated records exit 3, `ALTIUM_MALFORMED`).
Corrupt containers exit **3** (`ALTIUM_BAD_MAGIC`, `ALTIUM_FAT_CYCLE`, ...). Also note:
`net`/`component`/`check`/`diff`/`pinmap`/`export` accept schematics only — feeding
them a `.SchLib` or `.PcbDoc` exits 5 with a note to use `read` instead. An unknown
OLE2 container is now classified by its storage layout and fails loudly (exit 5)
instead of silently being read as an empty schematic. Separately, `akcli read --strict`
turns a non-empty source that normalizes to zero objects (`EMPTY_IMPORT`) into an
exit-1 failure rather than a quiet empty result.

## Getting designs OUT of Altium

```bash
akcli read main.SchDoc --json > model.json        # full normalized model (schema_version "1.3")
akcli net main.SchDoc --json > netlist.json       # structured netlist (stable_id per net)
akcli export main.SchDoc --format protel -o board.net   # Altium/Protel .NET for other EDA tools
akcli export main.SchDoc --format kicad -o board.net.kicad   # KiCad legacy eeschema netlist
akcli export main.SchDoc --format csv -o nets.csv            # flat net,ref,pin rows
```

`export` defaults to `--format protel` and writes stdout unless `-o` is given.
`akcli export --json` wraps the rendered netlist text in a `{schema_version, source,
format, content}` envelope (the netlist body itself is unchanged) — for a fully
structured netlist model use `akcli net --json` instead. All exports are
deterministically sorted; unnamed nets are named by membership-derived `stable_id`,
so re-exports are diffable.

## Getting changes INTO Altium

`akcli` has **no offline Altium write path** on any OS. Two routes exist.

### Route A (default, offline): human draw instructions + importable netlist

1. Author the edit as a normal op-list (see the akcli-circuit-design skill for the format),
   then translate each op into unambiguous human instructions. Good instructions state,
   per op: the **exact library symbol** and designator (`Place Device:R as R10, value
   10k`), the **location in mils** on the 50-mil grid, **pin-level wiring** by
   `REF.PIN` (`wire R10.2 to U3.14`), and every power port / net label by **net name**.
   Never say "connect the resistor to the MCU" — name pins and nets.
2. Export the **current** (pre-edit) connectivity as a baseline Protel netlist the
   user can cross-probe against while editing — `export` has no `--ops` input, so it
   always emits the file as it is now, never the post-edit connectivity:
   ```bash
   akcli export main.SchDoc --format protel -o baseline.net
   ```
   If a netlist of the intended *new* connectivity is needed, first draw the op-list
   into a scratch KiCad schematic and export that file instead
   (`akcli draw scratch.kicad_sch --ops ops.json --apply`, then
   `akcli export scratch.kicad_sch --format protel -o intended.net`).
3. After the user edits in Altium, verify — never trust the human either:
   ```bash
   akcli diff main.SchDoc main_edited.SchDoc --exit-zero
   ```

### Route B (optional, live): the Windows Altium 22+ bridge

A file-based JSON bridge drives a **running** Altium Designer 22+ on Windows. The
Python half (`src/akcli/drivers/altium_live/bridge.py`) is tested; the
DelphiScript half (`src/akcli/drivers/altium_live/scripts/altium_api.pas`)
is a scaffold **not yet validated on a
real Windows box** — treat every live write as experimental. It is **not wired into
the CLI** (`akcli draw` targets `.kicad_sch` only); call it from Python:

```python
from akcli.drivers.altium_live.bridge import ping, send, default_bridge_dir

print(ping())  # altium_ping handshake; rejects protocol_version != 1 (PROTOCOL_MISMATCH)
oplist = {
    "protocol_version": 1,
    "target_format": "altium",
    "target_file": "C:\\work\\project\\main.SchDoc",
    "ops": [
        {"op": "place_component", "lib_id": "Resistor", "designator": "R99",
         "x_mil": 1000, "y_mil": 2000, "value": "10k"},
        {"op": "add_wire", "vertices": ["R99.1", [1100, 2000]]},
    ],
}
print(send(oplist, reqdir=default_bridge_dir(), timeout=60.0))
```

Setup on the Windows box: open
`src/akcli/drivers/altium_live/scripts/altium_api.PrjScr` in Altium once, then each
request is served by running `altium_api>Run` (interactively via DXP → Run Script, or
`X2.EXE -RScriptingSystem:RunScript(...)`). Both halves must agree on the bridge dir:
env `AKCLI_ALTIUM_BRIDGE_DIR`, or the pointer file `%TEMP%\akcli-altium-bridge.path`
for a pre-running Altium, else the default `%TEMP%\akcli-altium-bridge\`.

Live-driver rules:
- Supported ops: `place_component`, `set_component_transform`,
  `set_component_parameters`, `add_wire`, `add_junction`, `add_no_connect`,
  `add_net_label`, `place_power_port`/`place_gnd`/`place_vcc`, `add_text`.
  `add_bus`/`add_bus_entry` return `OP_UNSUPPORTED` (KiCad-writer only).
- v1 caveats: non-ASCII text decodes to `?`; `set_component_parameters` applies only
  designator/comment (not footprint/custom params); net labels are always local scope;
  no delete/move ops — additive placement plus transform/parameter edits only.
- The whole op-list is one undo transaction (a single Ctrl+Z reverts it), but snapshot
  or copy the project before any live write, and test on a throwaway copy first.
- A `status: "ok"` response means **ops placed, not design verified** — re-export the
  Altium netlist and diff it (see Verification below).
- Transport failures are `BridgeBusy` (a held `.lock`) and stdlib `TimeoutError`
  (default 30 s poll timeout); neither means Altium rejected the ops.

## AD → KiCad migration workflow

`akcli` is **not** an Altium-to-KiCad converter — it carries connectivity truth, not
artwork. Migrate by re-drawing in KiCad and proving net equivalence:

1. **Read the source of truth.** `akcli read main.SchDoc --json` and
   `akcli net main.SchDoc --json` give the component list, values, footprints, and
   nets to reproduce.
2. **Map the library.** For each Altium component pick a KiCad `lib_id`: use symbols
   from a project `.kicad_sym` or the official KiCad libraries (passed to `plan`/`draw`
   via repeatable `--symbols`, or config `[paths]`), or convert real LCSC parts with
   `akcli jlc add <C-number>`. Carry the Altium `value` and
   resolved `footprint` fields from the `read --json` output into `place_component` ops.

   Going the OTHER way (a library the user needs **inside Altium**): `akcli jlc add`
   emits KiCad-6-dialect libraries that Altium Designer imports natively —
   **File » Import Wizard » KiCad Design Files** converts the `.kicad_sym`/`.kicad_mod`
   to a `.SchLib`/`.PcbLib`. That wizard is the supported route for any KiCad→AD
   library need; akcli itself never writes Altium files.

   If the project has its **own** Altium `.PcbLib` footprint (not an LCSC-sourced part,
   so `jlc add` doesn't apply), convert it directly:
   ```bash
   akcli library import-altium part.PcbLib --out vendor.pretty --courtyard 0.25 --apply
   ```
   Pads convert verbatim and are cross-validated against KiCad's own Altium importer;
   the run writes a `provenance.json` alongside the output (source file SHA-256,
   converter version, and every declared transformation — e.g. a synthesized courtyard).
   Dry-run by default; `--apply` is required to write the files.
3. **Draw into KiCad.** Author the op-list, then (see the akcli-circuit-design skill):
   ```bash
   akcli plan board.kicad_sch --ops ops.json
   akcli draw board.kicad_sch --ops ops.json --apply
   ```
4. **Prove equivalence** — the step that makes this a migration, not a guess:
   ```bash
   akcli diff main.SchDoc board.kicad_sch --exit-zero
   ```
   Nets match by pin **membership** (Jaccard), not display name, so `N$1234`-style
   auto-name churn does not break it. A cross-format diff has no shared UniqueIDs, so
   it is always reported `low_confidence` — read the per-net membership changes, not
   just the summary line.

What will **not** carry over (plan around it, state it in the report):
- Symbol artwork, sheet graphics, text/annotation placement — connectivity only.
- Hierarchy: the KiCad writer is flat-only v1; the KiCad READER does follow `(sheet ...)` children (per-instance namespaces, sheet-pin<->hierarchical-label connectivity).
- Binary `.SchLib` symbol graphics; `.PcbDoc` fills/regions/texts/polygons (pads/tracks/
  vias/arcs DO read now, but nothing converts them to `.kicad_pcb` — read-side only).
- Altium pin electrical types map only ints 0–7 (no `POWER_OUT`/`NO_CONNECT`), so
  ERC fidelity differs slightly between the two sides.

## Verification: never trust a conversion

Any time a design crosses a tool boundary — Altium → KiCad, op-list → live Altium,
human edits from your instructions — diff netlists between source and result before
declaring success:

```bash
akcli diff source.SchDoc result.kicad_sch --exit-zero    # membership-level diff
akcli export source.SchDoc --format csv -o a.csv         # or compare flat CSVs
akcli export result.kicad_sch --format csv -o b.csv
```

`diff` exits 1 when membership changed (`--exit-zero` for report mode). Corroborate
with `akcli check result.kicad_sch --exit-zero` and read its metadata caveats
(passive-pin ratio, No-ERC suppressed count, unnamed-net count) before calling the
migration clean — a findings-free run on a mostly-untyped board proves little.
