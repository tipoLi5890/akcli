---
description: Build and validate an op-list, then draw/edit a KiCad .kicad_sch with akcli (dry-run first, then --apply on request). Writes files — user-triggered only.
argument-hint: <board.kicad_sch> [--ops ops.json] [--apply] [--symbols extra.kicad_sym] "<what to draw>"
disable-model-invocation: true
---

Draw or edit a **KiCad** `.kicad_sch` from an op-list using `akcli`. This command **writes files**,
so it is user-triggered only.

Arguments: `$ARGUMENTS`
- The target must be a KiCad `.kicad_sch` (Altium files are NOT writable — for Altium, instead
  hand the user human draw instructions plus `akcli export <file> --format protel -o board.net`).
- An existing op-list may be given via `--ops <file>`; otherwise build one from the user's
  natural-language request.
- `--apply` means actually write (default is a verify-only dry-run). `--symbols <path>` adds an
  extra `.kicad_sym` / template `.kicad_sch` symbol source (repeatable). A nickname `--symbols`
  and config `[paths]` do not cover falls back to the project/global `sym-lib-table`, so
  `Device:R` resolves against an installed KiCad — still pass `--symbols` when the run must be
  reproducible elsewhere, or for a freshly converted symbol no table registers yet.
- A child `.kicad_sch` of an existing hierarchy is a valid target; placing *into* a sub-sheet
  path from the parent is not supported — run `draw` against the child file itself.

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. **Build/validate the op-list.** If none was provided, author one as JSON per
   `schemas/ops.schema.json` (and `docs/op-list-authoring.md` if present). Document shape:
   `{ "protocol_version": 1, "target_format": "kicad", "target_file": "<board>.kicad_sch",
   "ops": [ ... ] }`. Honor the contract: coordinates in **mils, origin top-left, +Y down, 50-mil
   grid**; rotation enum `{0,90,180,270}`; mirror `{none,x,y}`; `add_wire.vertices` is an even,
   orthogonal array of `[x,y]` points or `"REF.PIN"` endpoint strings. The 22 ops: `place_component`
   (optional `"unit": N` for multi-unit parts), `set_component_transform`,
   `set_component_parameters`, `add_wire`, `route_net` (L/Z auto-route), `add_junction`, `add_no_connect`, `add_net_label`,
   `place_power_port` (sugar: `place_gnd`/`place_vcc`), `add_bus`, `add_bus_entry`, `add_text`,
   `add_rectangle` (graphic annotation / group frames), `add_text_box` (bordered notes), `add_sheet`, `delete_component`, `delete_object`, `move_component`, `rename_net`, `set_title_block`
   (`akcli ops list` is the authoritative vocabulary). Write it to a temp `ops.json`.

   For modular designs, declare a `groups` envelope (`{NAME: {origin, title?}}`) and tag ops
   with `"group"` — coordinates become group-local, membership persists in the sheet, and
   `akcli groups <sch> --frame --apply` can draw module borders afterwards. Use
   `anchor`/`offset_mil` for relative placement ("decoupling cap on U1.VCC"), `place_array` for
   repeated parts, `route_net` for non-coaxial pin-to-pin connections, and `akcli bbox <lib_id>`
   to reserve space before placing. An `add_text`/`add_text_box`/`add_rectangle` `key` must be
   unique within the op-list — the validator refuses duplicates (both ops would seed one uuid).

   **Editing a list you already applied is safe for parts:** symbol/pin uuids key on the
   designator alone, so inserting or reordering ops does not re-identify untouched parts.
   Wires/graphics still key on coords, so a moved one leaves its old node behind — clean up with
   `delete_object`. A file written by a pre-0.17 akcli needs ONE unedited re-apply (or
   `akcli undo`) before the list is edited.

2. **Plan (never writes):** `akcli plan <board.kicad_sch> --ops ops.json [--symbols <path>]
   [--render preview.svg]`.
   Show the user what would change and resolve any `SYMBOL_NOT_FOUND` / off-grid / non-orthogonal
   errors before proceeding. **Read the "Net changes" block**: `! SPLIT` / `! MERGE` lines are the
   silent killers (name-based connectivity can short or fragment nets without any dangling
   endpoint); `(none)` proves the edit is connectivity-neutral. With `--render`, LOOK at the
   preview (world-mil grid overlay) before applying — a multimodal agent reads placement quality
   straight off the image.

3. **Dry-run draw (verify only, default):**
   `akcli draw <board.kicad_sch> --ops ops.json [--symbols <path>]`.
   Confirm per-op results are `ok` and the connectivity verifier reports zero dangling endpoints.

4. **Apply only when the user explicitly asked to write:**
   `akcli draw <board.kicad_sch> --ops ops.json --apply [--symbols <path>]`.
   When editing an EXISTING sheet, add `--strict-nets` (any `!` net-change line touching a named
   net refuses the write) and pass `--note "<why>"` so the workspace journal (`akcli log .`)
   records the design intent. The write is atomic (snapshot → temp → verify-on-temp → `os.replace`) and writes a rotated
   `<target>.bak` copy under the workspace's `.akcli/backups/` (walked by `akcli undo`), and is
   rejected if connectivity verification fails — so the original is never corrupted.

5. **Verify after writing:** re-read with `akcli read <board.kicad_sch>` / `akcli net ...` to
   confirm the result matches intent; `akcli check` covers the group layout lints
   (`LAYOUT_GROUP_OVERLAP` / `LAYOUT_FRAME_STALE`) and re-runs the write gate's document
   checks read-only (`--integrity`: dangling endpoints, duplicate uuids/designators,
   unresolved `lib_id`s, instance paths, no-connect conflicts). Never assume the write was correct without
   re-checking. For grouped sheets, `akcli groups <sch>` lists modules and
   `akcli groups <sch> --frame --apply` draws/refreshes their borders.

If any op returns `ERROR: OP_UNSUPPORTED`, `PROTOCOL_MISMATCH`, or a geometry error, stop and
report it; do not retry blindly.
