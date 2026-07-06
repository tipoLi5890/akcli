---
name: parts-sourcing
description: >-
  Source real, orderable parts for a schematic with the `akcli jlc` command family —
  search the JLCPCB/LCSC catalog, check stock/price/Basic-vs-Extended status, pick the
  right candidate for the build quantity, and close the BOM-hygiene loop with
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

### (2) Get a symbol/footprint

`akcli` does **not** convert LCSC parts into libraries. Source the KiCad symbol and
footprint from the official KiCad libraries, a project `.kicad_sym`, or the vendor —
then verify pin mapping and the land pattern against the datasheet before wiring the
part in. Symbols feed `akcli plan`/`akcli draw` via repeatable `--symbols` (see the
circuit-design skill).

### (3) Place and close the BOM loop

Author a `place_component` op for the chosen symbol, then run the BOM hygiene loop
until clean:

```bash
akcli draw board.kicad_sch --ops place.json --symbols mylib.kicad_sym          # dry-run first
akcli draw board.kicad_sch --ops place.json --symbols mylib.kicad_sym --apply
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
