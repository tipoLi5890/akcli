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
  extra `.kicad_sym` / template `.kicad_sch` symbol source (repeatable).

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. **Build/validate the op-list.** If none was provided, author one as JSON per
   `schemas/ops.schema.json` (and `docs/op-list-authoring.md` if present). Document shape:
   `{ "protocol_version": 1, "target_format": "kicad", "target_file": "<board>.kicad_sch",
   "ops": [ ... ] }`. Honor the contract: coordinates in **mils, origin top-left, +Y down, 50-mil
   grid**; rotation enum `{0,90,180,270}`; mirror `{none,x,y}`; `add_wire.vertices` is an even,
   orthogonal array of `[x,y]` points or `"REF.PIN"` endpoint strings. The 18 ops: `place_component`
   (optional `"unit": N` for multi-unit parts), `set_component_transform`,
   `set_component_parameters`, `add_wire`, `add_junction`, `add_no_connect`, `add_net_label`,
   `place_power_port` (sugar: `place_gnd`/`place_vcc`), `add_bus`, `add_bus_entry`, `add_text`,
   `add_sheet`, `delete_component`, `delete_object`, `move_component`, `rename_net`
   (`akcli ops list` is the authoritative vocabulary). Write it to a temp `ops.json`.

2. **Plan (never writes):** `akcli plan <board.kicad_sch> --ops ops.json [--symbols <path>]`.
   Show the user what would change and resolve any `SYMBOL_NOT_FOUND` / off-grid / non-orthogonal
   errors before proceeding.

3. **Dry-run draw (verify only, default):**
   `akcli draw <board.kicad_sch> --ops ops.json [--symbols <path>]`.
   Confirm per-op results are `ok` and the connectivity verifier reports zero dangling endpoints.

4. **Apply only when the user explicitly asked to write:**
   `akcli draw <board.kicad_sch> --ops ops.json --apply [--symbols <path>]`.
   The write is atomic (snapshot → temp → verify-on-temp → `os.replace`) and writes a `<target>.bak`
   copy, and is rejected if connectivity verification fails — so the original is never corrupted.

5. **Verify after writing:** re-read with `akcli read <board.kicad_sch>` / `akcli net ...` to
   confirm the result matches intent. Never assume the write was correct without re-checking.

If any op returns `ERROR: OP_UNSUPPORTED`, `PROTOCOL_MISMATCH`, or a geometry error, stop and
report it; do not retry blindly.
