# `akcli jlc` — JLCPCB / LCSC part search

Search the JLCPCB / LCSC component catalog from the command line. This is the
**only networked feature** in altium-kicad-cli; every other command is fully
offline and zero-dependency.

> **Needs network.** `jlc` calls the public **jlcsearch** service
> (`https://jlcsearch.tscircuit.com`). All other `akcli` commands work with no
> network and no Altium/KiCad install. Network code is import-isolated under
> `altium_kicad_cli.parts` and loaded lazily, so it never touches the offline paths.

## Commands

### `akcli jlc search <query> [--limit N] [--json]`

Keyword search. The query matches manufacturer part number (MPN), category, and
the LCSC C-number (with or without the `C` prefix).

```bash
akcli jlc search NE555
akcli jlc search NE555 --limit 5
akcli jlc search "0603 100nF" --json
```

Text output is a table (`LCSC  MPN  PACKAGE  STOCK  PRICE  B  DESCRIPTION`),
where the `B` column is `B` for a JLCPCB **Basic** part, `P` for a **Preferred**
part, else `-`. `--json` emits the full list of part objects.

Exit codes: `0` on results **and** on a clean no-results path (a `no parts found`
notice goes to stderr); `7` on a network/HTTP error (a single
`ERROR: NETWORK: …` line goes to stderr — never a traceback).

### `akcli jlc show <C-number> [--easyeda] [--json]`

Fetch one part by its LCSC C-number (e.g. `C7593` or bare `7593`).

```bash
akcli jlc show C7593
akcli jlc show C7593 --json
akcli jlc show C2040 --easyeda          # also: 3D-model availability + EasyEDA metadata
```

`--easyeda` adds a light, read-only metadata lookup against EasyEDA's **unofficial**
Std-editor backend to report whether a **3D/STEP model is available**, plus the
manufacturer/MPN/package EasyEDA has on file. It converts nothing. The lookup is
best-effort: if EasyEDA is unreachable or the endpoint changes, the EasyEDA section is
simply omitted (or shows `(metadata unavailable)`) — it never breaks `jlc show`.

Exit codes: `0` when found, `0` with a `no part … found` stderr notice when the
C-number does not exist, `7` on a network/HTTP error.

### `akcli jlc add <C-number> [--3d] [--out DIR] [--lib-name NAME] [--force] [--place ...]`

Fetch a real LCSC/EasyEDA part and convert it into a KiCad library — **in-process**,
via the vendored MIT [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib)
core (no external tool to install; networked).

```bash
akcli jlc add C2040                       # symbol + footprint
akcli jlc add C2040 --3d                  # + 3D STEP model
akcli jlc add C2040 --out ./mylib --lib-name akcli
akcli jlc add C25804 --place --designator R1 --at 2000 1000   # + one-op place.json
```

Output layout under `--out` (default `./akcli-parts/<C-number>/`):
`symbol/<lib-name>.kicad_sym`, `footprint/<name>.kicad_mod`, and with `--3d`
`footprint/packages3d/<name>.step`. `--place` writes `place.json` (a one-op
`place_component` op-list with `lib_id` read from the produced `.kicad_sym` and
the footprint id from the `.kicad_mod` stem) to apply with
`akcli draw <target> --ops place.json --symbols <symbol lib> --apply`.

Exit codes: `0` success · `2` bad usage (bad C-number, `--place` without
`--designator`/`--at`) · `4` part has no EasyEDA CAD data · `6` conversion
failed / produced nothing · `7` network error.

**A converted library is a claim, not a fact** — the CAD data comes from
EasyEDA/LCSC and can be wrong (pin mapping, land pattern, 3D origin). Verify
against the datasheet before wiring the part in.

## Part fields (JSON)

| field | meaning |
|---|---|
| `lcsc` | LCSC C-number, e.g. `C7593` |
| `mpn` | manufacturer part number |
| `description` | catalog description (often empty in jlcsearch) |
| `package` | package / footprint name, e.g. `SOIC-8`, `0603` |
| `stock` | JLCPCB stock quantity |
| `price` | representative unit price (lowest-quantity tier) as a float, or `null` |
| `basic` | `true` for a JLCPCB Basic part |
| `datasheet` | datasheet URL when provided, else `null` |
| `category` | top-level category |
| `attributes` | extra fields: `subcategory`, `is_preferred`, `price_tiers`, `specs` |

The upstream `price` is a tiered structure; `attributes.price_tiers` keeps the
full `[{qFrom, qTo, price}, …]` ladder while `price` surfaces the cheapest
single-unit (lowest-`qFrom`) tier for quick comparison.

## Library use

```python
from altium_kicad_cli.parts import search as jlc

parts = jlc.search("NE555", limit=10)        # list[Part]
part  = jlc.get("C7593")                      # Part | None

# inject a transport (any urllib OpenerDirector-like object) to run offline/test:
parts = jlc.search("NE555", opener=my_fake_opener)

# optional short on-disk cache keyed by request URL, to avoid hammering the API:
parts = jlc.search("NE555", cache_dir="/tmp/akcli-cache", cache_ttl=3600)
```

Network failures raise `altium_kicad_cli.parts.search.JlcNetworkError` (clean
message, no traceback).

## Attribution

Part search is backed by the public **jlcsearch** service (tscircuit, MIT) with data
from **jlcparts** (MIT); EasyEDA/LCSC/JLCPCB are the underlying data sources. Full
notices in `THIRD_PARTY_NOTICES.md`.
