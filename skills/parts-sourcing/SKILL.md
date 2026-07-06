---
name: parts-sourcing
description: >-
  Source real, orderable parts for a schematic with the `akcli jlc` command family —
  search the JLCPCB/LCSC catalog, check stock/price/Basic-vs-Extended status, convert a
  chosen part into a KiCad symbol/footprint/3D library (in-process, vendored MIT
  JLC2KiCadLib core), verify it against the datasheet, and close the BOM-hygiene loop with
  `akcli check --bom`. Use this skill whenever the task involves: finding a part by MPN,
  value, or package; looking up an LCSC C-number; checking JLCPCB stock or price tiers;
  choosing between Basic and Extended parts; recording sourced parts on a schematic; or
  filling in missing BOM values and footprints. Triggers on keywords: JLCPCB, LCSC,
  C-number, jlcsearch, EasyEDA, BOM, bill of materials, part search, sourcing, stock,
  price, Basic part, Extended part.
---

# parts-sourcing — driving `akcli jlc` for JLCPCB/LCSC search and BOM hygiene

`akcli jlc` searches the JLCPCB / LCSC catalog (via the public jlcsearch service). It is
the **only networked `akcli` feature** — every other command runs fully offline. For
plain schematic read/analyze/draw mechanics (formats, op-list rules, exit-code legend,
config discovery), **see the circuit-design skill**; this skill covers only the
sourcing loop.

## Core sourcing principles (follow these)

- **Prefer Basic (`B`) parts, then Preferred (`P`).** Extended parts add per-reel assembly
  fees at JLCPCB. Surface the stock count with every recommendation — a perfect part with
  0 stock is not sourced.
- **Compare at the build quantity.** The `--json` output carries the full
  `price_tiers` ladder; the headline price is the lowest-`qFrom` tier only.
- **Check assemblability before falling in love with a package.** JLCPCB's PCBA line
  has hard minimums (0402/0201, BGA pitch, pin pitch) — see the jlcpcb-capabilities skill.
- **The schematic is authoritative.** Record every sourcing decision on the schematic
  (an `LCSC` parameter per designator), never only in a side document.

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

### (2) Convert — turn a C-number into a KiCad library

```bash
akcli jlc add C2040                                   # symbol + footprint
akcli jlc add C2040 --3d                              # + 3D STEP model
akcli jlc add C2040 --out ./mylib --lib-name akcli --force
```

Conversion runs **in-process** (vendored MIT JLC2KiCadLib core — no external
binary). Output layout: `symbol/<lib-name>.kicad_sym`,
`footprint/<name>.kicad_mod`, `footprint/packages3d/<name>.step` (with `--3d`).
Exit codes: `0` success, `2` bad usage, `4` part has no EasyEDA CAD data,
`6` conversion failed/empty, `7` network error.

**A converted library is a claim, not a fact.** Verify before wiring in:
1. **Pin count vs datasheet** — `akcli read <out>/symbol/<lib>.kicad_sym` must match
   the package drawing exactly; spot-check power/ground/pin-1 in `--json`.
2. **Footprint keying** — check pin-1 marking, pad numbering and courtyard against
   the datasheet land pattern (read the `.kicad_mod` or view it in KiCad).

### (3) Place and close the BOM loop

`--place` emits a one-op `place_component` op-list (`place.json`) with `lib_id`
read from the produced `.kicad_sym` — never guessed from filenames:

```bash
akcli jlc add C2040 --out ./mylib --place --designator U1 --at 1000 1500
akcli draw board.kicad_sch --ops ./mylib/place.json --symbols ./mylib/symbol/akcli.kicad_sym          # dry-run first
akcli draw board.kicad_sch --ops ./mylib/place.json --symbols ./mylib/symbol/akcli.kicad_sym --apply
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
  Digi-Key/Mouser/Octopart client in `akcli`. Source from the vendor and flag for human review.
- **Controlled, long-lead, or supply-critical parts** (automotive-qualified, ITAR, allocated
  MCUs): JLCPCB stock is a point-in-time snapshot, not a procurement commitment. Flag these for
  human sourcing instead of relying on `stock > 0`.
- **Offline environments:** `jlc` is the only networked subcommand; everything else in `akcli`
  still works. Without network, `jlc search`/`jlc show` exit `7`.

## Exit codes (jlc family)

`0` success, including clean no-results (stderr notice) · `2` usage error · `7` network
error. Full legend and error-line format: see the circuit-design skill.
