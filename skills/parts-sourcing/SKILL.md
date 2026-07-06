---
name: parts-sourcing
description: >-
  Source real, orderable parts for a schematic with the `akcli jlc` command family —
  search the JLCPCB/LCSC catalog, check stock/price/Basic-vs-Extended status, convert a
  chosen part into a KiCad or Altium library via delegated external converters, verify the
  converted symbol/footprint against the datasheet, and close the BOM-hygiene loop with
  `akcli check --bom`. Use this skill whenever the task involves: finding a part by MPN,
  value, or package; looking up an LCSC C-number; checking JLCPCB stock or price tiers;
  generating a KiCad symbol/footprint/3D model or an Altium .SchLib/.PcbLib from an LCSC
  part; placing a sourced part into a schematic; or filling in missing BOM values and
  footprints. Triggers on keywords: JLCPCB, LCSC, C-number, jlcsearch, EasyEDA, BOM,
  bill of materials, part search, sourcing, stock, price, Basic part, Extended part,
  footprint library, symbol library, nlbn, npnp.
---

# parts-sourcing — driving `akcli jlc` for JLCPCB/LCSC search, library conversion, and BOM hygiene

`akcli jlc` searches the JLCPCB / LCSC catalog (via the public jlcsearch service) and converts
real parts into KiCad or Altium libraries. It is the **only networked `akcli` feature** — every
other command runs fully offline. For plain schematic read/analyze/draw mechanics (formats,
op-list rules, exit-code legend, config discovery), **see the circuit-design skill**; this skill
covers only the sourcing loop.

## Core sourcing principles (follow these)

- **A converted library is a claim, not a fact.** `jlc add` output comes from a third-party
  converter fed with EasyEDA/LCSC data; pin mapping, courtyard, and 3D origin can all be wrong.
  Verify every converted part against the datasheet before wiring it in.
- **Prefer Basic (`B`) parts, then Preferred (`P`).** Extended parts add per-reel assembly fees
  at JLCPCB. Surface the stock count with every recommendation — a perfect part with 0 stock is
  not sourced.
- **Placement is a separate, reviewable step.** `jlc add --place` only writes an op-list file;
  the schematic changes only through `akcli draw ... --apply` (see the circuit-design skill).
- **Never enable converter auto-download silently.** Absent binaries exit `7` with an install
  hint; `--auto-download` is an explicit trust decision the user should make.

## Workflow

### (1) Search — find candidate parts

```bash
akcli jlc search NE555                     # keyword search (default 20 results)
akcli jlc search NE555 --limit 5
akcli jlc search "0603 100nF" --json       # machine-readable part objects
akcli jlc show C7593                       # one part by LCSC C-number (bare 7593 also works)
akcli jlc show C2040 --easyeda             # + 3D/STEP availability, EasyEDA manufacturer/MPN/package
```

The query matches MPN, category, and C-number. Text output is a table
(`LCSC  MPN  PACKAGE  STOCK  PRICE  B  DESCRIPTION`) where the `B` column is `B` for a JLCPCB
**Basic** part, `P` for **Preferred**, `-` otherwise. `--json` part fields: `lcsc`, `mpn`,
`package`, `stock`, `price` (lowest-`qFrom` tier as a float, or `null`), `basic`, `datasheet`,
`category`, and `attributes` (with `subcategory`, `is_preferred`, and the full
`price_tiers` `[{qFrom,qTo,price},...]` ladder). Compare candidates on stock, price tier at the
build quantity, and Basic status — not just the first hit.

No results is exit `0` with a stderr notice; network/HTTP failures exit `7` with one
`ERROR: NETWORK: ...` line. `--easyeda` is best-effort: on failure it prints
`(metadata unavailable)` and never breaks the command.

### (2) Convert — turn a C-number into a library

```bash
akcli jlc add C2040 --to kicad                                  # symbol + footprint
akcli jlc add C2040 --to kicad --3d                             # + 3D STEP model
akcli jlc add C2040 --to kicad --out ./mylib --lib-name akcli --english --force
akcli jlc add C2040 --to altium                                 # .SchLib + .PcbLib pair
```

Default output dir is `./akcli-parts/<C-number>/`. Conversion is **delegated to external
Apache-2.0 Rust subprocesses** (never imported or vendored):

- `--to kicad` uses **nlbn** (`cargo install nlbn`, or a pinned GitHub release).
- `--to altium` uses **npnp** — upstream ships Windows-x86_64 binaries only; on macOS/Linux
  build it with `cargo install --git https://github.com/linkyourbin/npnp`.

**When the binary is not installed:** `jlc add` prints a copy-pasteable install hint and exits
`7`. It never downloads anything by default. Re-running with `--auto-download` fetches the
version-pinned, SHA-256-verified release into the cache — but only offer this after telling the
user what it does, and note that npnp publishes no checksum upstream, so its auto-download is
refused; `cargo install` is the only non-Windows path for `--to altium`.

Exit codes for `jlc add`: `0` success, `2` bad usage (missing `--to`, bad C-number, `--place`
without `--designator`/`--at`, `--to altium --place`), `4` part not found, `6` converter ran but
failed or produced nothing, `7` binary missing or network error.

### (3) Verify — never trust the converter

Every successful `jlc add` prints a verify caveat. Act on it:

```bash
akcli read ./akcli-parts/C2040/akcli.kicad_sym          # symbol names + pin counts
akcli read ./akcli-parts/C2040/akcli.kicad_sym --json | jq '.'
```

Check, in order:
1. **Pin count vs datasheet** — the symbol's pin count from `akcli read` must match the
   datasheet's package drawing exactly. A mismatch means a wrong or truncated conversion: stop.
2. **Pin mapping** — spot-check critical pins (power, ground, pin 1) in the `--json` output
   against the datasheet pinout table.
3. **Footprint keying** — open the produced `.kicad_mod` and confirm pin-1 marking, pad
   numbering direction, and courtyard match the datasheet land pattern; for `--3d`, check the
   model origin. `akcli` cannot render footprints — read the file text or view it in KiCad.
4. After placing (step 4), run `akcli check` on the schematic; if `kicad-cli` is installed its
   advisory ERC runs automatically after `akcli draw --apply`.

For Altium output: `akcli read` decodes the produced `.SchLib` (symbol names + pin counts;
`--json` for pin detail), so run checks 1–2 on it directly. `.PcbLib` has no reader — reading
it fails with a parse error (exit `3`) — so footprint verification (check 3) must happen in
Altium Designer; say so explicitly and hand the user the datasheet checkpoints above.

### (4) Place and close the BOM loop

`--place` (KiCad only) emits a one-op `place_component` op-list to `<out>/place.json`, with
`lib_id` read from the produced `.kicad_sym` and `footprint` from the `.kicad_mod` — never
guessed from filenames:

```bash
akcli jlc add C2040 --to kicad --out ./mylib --place --designator U1 --at 1000 1500
akcli draw board.kicad_sch --ops ./mylib/place.json --symbols ./mylib/akcli.kicad_sym          # dry-run first
akcli draw board.kicad_sch --ops ./mylib/place.json --symbols ./mylib/akcli.kicad_sym --apply
```

Then run the BOM hygiene loop until clean:

```bash
akcli check board.kicad_sch --bom              # dup designators, refdes gaps, missing value/footprint
akcli component board.kicad_sch U1             # confirm the placed part's pin -> net map
```

- **Missing value/footprint findings:** fix via a `set_component_parameters` op (fields:
  `designator` required; optional `value`, `footprint`, `parameters`) applied with
  `akcli plan` / `akcli draw --apply` — see the circuit-design skill for op-list mechanics.
- **Match designators to sourced parts:** record each designator's LCSC C-number (e.g. as an
  `LCSC` entry in the op's `parameters` object) so the fabrication BOM maps `R10 -> C25804`
  unambiguously. Re-run `akcli check board.kicad_sch --bom` after every edit; it exits `1`
  while findings remain (`--exit-zero` for report mode).
- After any `--apply`, re-read (`akcli read` / `akcli net`) to confirm the write — never assume.

## When NOT to use this skill

- **Parts with no LCSC listing** (`jlc show` returns a `no part ... found` notice): there is no
  Digi-Key/Mouser/Octopart client in `akcli`. Source the symbol/footprint from the vendor or
  draw it manually; do not force a "close enough" LCSC substitute silently.
- **Controlled, long-lead, or supply-critical parts** (automotive-qualified, ITAR, allocated
  MCUs): JLCPCB stock is a point-in-time snapshot, not a procurement commitment. Flag these for
  human sourcing instead of relying on `stock > 0`.
- **Offline environments:** `jlc` is the only networked subcommand; everything else in `akcli`
  still works. Without network, `jlc search`/`jlc show` and `--auto-download` fetches exit `7`;
  `jlc add` with the converter installed fails inside the converter subprocess instead,
  surfacing as exit `6` (converter ran but failed) or `4` (converter reports part not found) —
  never `7`.
- **Altium placement:** `--place` is KiCad-only (`--to altium --place` exits `2`), and `akcli`
  never writes Altium schematics — deliver human instructions per the circuit-design skill.

## Exit codes (jlc family)

`0` success, including clean no-results (stderr notice) · `2` usage error · `4` part not found ·
`6` converter ran but failed / produced no artifacts · `7` network error or missing external
binary (install hint printed). Full legend and error-line format: see the circuit-design skill.
