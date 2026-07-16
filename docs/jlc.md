# `akcli jlc` — JLCPCB / LCSC part search

Search the JLCPCB / LCSC component catalog from the command line. This is the
**only networked feature** in akcli; every other command is fully
offline and zero-dependency.

> **Needs network.** `jlc` calls the public **jlcsearch** service
> (`https://jlcsearch.tscircuit.com`; override with `AKCLI_JLC_BASE_URL` for a
> self-hosted instance or tests). All other `akcli` commands work with no
> network and no Altium/KiCad install. Network code is import-isolated under
> `akcli.parts` and loaded lazily, so it never touches the offline paths.

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

Note: unlike most `akcli` commands, a `jlc` network error does not emit the
`--json` error envelope on stdout — only the plain `ERROR: NETWORK: …` line on
stderr.

### `akcli jlc bom <sch> [--qty N] [--min-stock N] [--suggest] [--fix|--fix-all] [--csv OUT.csv] [--json]`

Check a schematic's **BOM against the live catalog** — every BOM line is
resolved to a part and reported with stock, price and Basic/Preferred status:

```bash
akcli jlc bom board.kicad_sch
akcli jlc bom board.SchDoc --min-stock 100 --json
```

Resolution order per line: an **LCSC C-number parameter** (`LCSC`, `LCSC Part`,
`JLCPCB#`, … — any parameter mentioning lcsc/jlc whose value looks like
`C12345`) wins and is fetched directly; else an **MPN parameter** (`MPN`,
`Manufacturer Part`, `Part Number`, …) is searched and matched exactly
(preferring in-stock Basic parts, then the deepest stock); else the line is
listed as `no-part-id` (advisory — guessing a part from a bare "10k 0402"
would not be a check). Components sharing one identity group into one line
(one lookup, `QTY` = ref count); `#`-virtual parts are excluded like the
offline BOM check.

`--qty N` evaluates at build quantity: every line needs `N x refs` pieces,
stock is compared against that, the applicable **price tier** is chosen at
that quantity, and the summary carries the estimated parts cost per run.
Responses are cached on disk for an hour (`~/.cache/akcli/jlc`;
`AKCLI_JLC_CACHE` relocates or disables). Transient network failures
(timeouts, HTTP 429/5xx) are retried with exponential backoff honoring
`Retry-After`; when retries are exhausted and a cached copy exists, the
**stale copy is served with a stderr warning** (`AKCLI_JLC_CACHE_STALE=off`
restores hard failure).

`--suggest` searches the catalog for every `not-found` / `no-part-id` line
(query = value + package size from the footprint, e.g. `100n` +
`C_0402_...` → `100nF 0402`; candidates must match the package, ranking
prefers in-stock Basic parts) and prints the best candidate. Each suggestion
is graded **high** confidence (package matched AND the value is visible in
the candidate's description/MPN) or **low** (package matched only). `--fix`
writes only high-confidence C-numbers back into the schematic's LCSC
parameters — in the SAME parameter key when one existed (correcting a wrong
id in place) — through the draw pipeline (`.bak`, `akcli undo` reverts),
then re-checks; withheld low-confidence suggestions are counted on stderr.
Like `plan`/`draw`/`arrange`, the fix is recorded in the workspace write
journal — `akcli log` shows a `jlc-bom-fix` entry.
`--fix-all` also writes the low-confidence ones. Suggestions are heuristics:
**verify the datasheet before building.**

`--csv OUT.csv` also writes a **JLCPCB upload BOM CSV** (header exactly
`Comment,Designator,Footprint,LCSC Part #`; all refs of a line comma-joined
in one quoted Designator cell; footprint shortened after the `:`;
unresolved/dead-id lines get a **blank** LCSC cell so a dead C-number never
lands in an order file). `'-'` writes the CSV to stdout.

Statuses: `ok` · `low-stock` (below `--min-stock` or the needed quantity) · `out-of-stock` ·
`not-found` · `no-part-id`. Exit `1` when any of the first three problems is
present (`no-part-id` never fails the run; `--exit-zero` forces `0`); exit
`7` on a network error.

### `akcli jlc datasheet <C-number|MPN|sch> [--fetch] [--out DIR] [--force] [--resolve-mpn] [--json]`

Resolve — and with `--fetch`, download — **datasheet PDFs**. The target is one
LCSC C-number, one exact MPN (catalog-matched like `jlc bom`), or a schematic:
then every BOM line carrying an `LCSC` parameter is resolved in one run.

```bash
akcli jlc datasheet C2984661                     # print the PDF URL + MPN/manufacturer
akcli jlc datasheet TCRT5000 --fetch             # exact-MPN match, download the PDF
akcli jlc datasheet board.kicad_sch --fetch      # whole BOM -> ~/.cache/akcli/datasheets/
akcli jlc datasheet board.kicad_sch --resolve-mpn --fetch   # MPN-only lines get a catalog lookup first
```

**`--resolve-mpn`** (schematic target only) is opt-in: for BOM lines that carry
an MPN but no `LCSC` C-number, it first does an exact-match catalog lookup (same
in-stock/basic/stock preference policy as `jlc bom`, one search per distinct MPN)
to pin a C-number before the normal EasyEDA resolve. A hit is resolved like any
other; a miss stays `not-found` with a nearest-MPN note. A network error during
the lookup exits `7`.

Resolution goes through the part's **EasyEDA component record** — the
jlcsearch mirror never carries datasheet links, and `lcsc.com` bot-gates
plain-HTTP downloads. The EasyEDA record embeds the szlcsc-hosted PDF link in
a `head.c_para.link` field (symbol *or* footprint side; both are checked),
and those `atta.szlcsc.com` files download cleanly.

`--fetch` verifies the **`%PDF` magic** before keeping anything: a
challenge/viewer HTML page answered with status 200 is rejected as
`fetch-failed` instead of being saved as a broken `.pdf`. Files land in
`--out DIR` (default `AKCLI_DATASHEET_DIR`, else `~/.cache/akcli/datasheets/`)
named `C<digits>_<MPN>.pdf`, and an existing file is never re-downloaded
unless `--force` — the directory doubles as the cache.

Not every EasyEDA link is a document, so `resolve` **classifies** instead of
pretending: a direct `.pdf` is `resolved` (fetchable); a product/viewer page
(`item.szlcsc.com` JS shell, bot-gated mouser paths, ...) is `page-link` — the
URL is printed for a browser-grade fetcher (WebFetch) to take over; a bare
search-engine query (real-world EasyEDA data contains these, occasionally for
the *wrong* part) is `no-link` with the LCSC product-page hint.

Statuses: `resolved` · `fetched` · `cached` · `page-link` · `no-link` ·
`not-found` · `no-lcsc` (BOM line without an `LCSC` parameter; pin one via
`jlc bom --suggest/--fix` — or `--resolve-mpn` when the line carries an MPN)
· `fetch-failed`. Exit `1` when anything short of a PDF remains (`page-link`,
`no-link`, `not-found`, `fetch-failed`; `no-lcsc` never fails the run;
`--exit-zero` forces `0`); exit `7` on a network error.

> **Datasheet → SPICE model loop.** Once you have the PDF, its forward-voltage
> table row feeds [`akcli sim fit-diode`](sim.md#akcli-sim-fit-diode--datasheet--spice-model),
> which fits a `.model` and can write `Sim.Device`/`Sim.Params` straight back onto
> the schematic (`--apply --write`) — so a sourced part becomes a simulatable one
> without leaving the CLI.

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

### `akcli jlc add <C-number> [--3d] [--out DIR] [--lib-name NAME] [--footprint-lib NICKNAME] [--3d-path MODE] [--force] [--place ...]`

Fetch a real LCSC/EasyEDA part and convert it into a KiCad library — **in-process**,
via the vendored MIT [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib)
core (no external tool to install; networked).

```bash
akcli jlc add C2040                       # symbol + footprint
akcli jlc add C2040 --3d                  # + 3D STEP model
akcli jlc add C2040 --out ./mylib --lib-name akcli
akcli jlc add C2040 --footprint-lib proj_jlc --3d-path relative   # match your fp-lib-table
akcli jlc add C25804 --place --designator R1 --at 2000 1000   # + one-op place.json
```

Output layout under `--out` (default `./akcli-parts/<C-number>/`):
`symbol/<lib-name>.kicad_sym`, `<footprint-lib>/<name>.kicad_mod`, and with `--3d`
`<footprint-lib>/packages3d/<name>.step`. `--place` writes `place.json` (a one-op
`place_component` op-list with `lib_id` read from the produced `.kicad_sym` and
the footprint id from the `.kicad_mod` stem) to apply with
`akcli draw <target> --ops place.json --symbols <symbol lib> --apply`.

**`--footprint-lib NICKNAME`** (default `footprint`) sets **both** the footprint output directory
and the fp-lib-table nickname written into the symbol's Footprint field (`<nickname>:<name>`). The
default `footprint` is the historic cause of KiCad's "footprint not found" — pass the nickname your
project's `fp-lib-table` actually registers, or fix an existing library with
`akcli library repair <project> --rename-footprint-lib footprint=<nickname> --apply`.
**`--3d-path MODE`** picks how the footprint references the 3D model: `relative` (default; portable,
resolves only next to the library), `absolute` (always resolves on this machine, not portable), or a
`${VAR}` prefix (portable via a KiCad path variable). The trade-off is printed on stderr. A converted
library is a CLAIM — verify pin mapping/dimensions against the datasheet, and cross-check the whole
workspace with `akcli library audit <project>`.

Exit codes: `0` success · `2` bad usage (bad C-number, `--place` without
`--designator`/`--at`) · `4` part has no EasyEDA CAD data · `6` conversion
failed / produced nothing · `7` network error.

#### Altium Designer users — the same library imports natively

The produced libraries are written in the **KiCad 6 dialect**, which Altium
Designer's built-in importer understands: in AD choose
**File » Import Wizard » KiCad Design Files**, point it at the produced
`symbol/<lib>.kicad_sym` and `footprint/*.kicad_mod`, and it converts them to a
native `.SchLib` / `.PcbLib`. No third-party tool needed. Verify the imported
part against the datasheet in AD just as you would in KiCad.

**A converted library is a claim, not a fact** — the CAD data comes from
EasyEDA/LCSC and can be wrong (pin mapping, land pattern, 3D origin). Verify
against the datasheet before wiring the part in.

## Part fields (JSON)

`jlc bom`/`datasheet`/`show`/`add` wrap their `--json` output with a top-level
`schema_version` field; `jlc search --json` is a bare array of part objects
(no wrapper).

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

## Manufacturing handoff — Gerber / drill / BOM / CPL from KiCad

A JLCPCB PCBA order needs four artifacts from a finished layout: **Gerbers**,
**drill files**, a **BOM**, and a **CPL** (centroid / component-placement)
file. akcli produces the BOM leg directly (`jlc bom --csv`); the other three
come out of KiCad — either headless via `kicad-cli` (agent/CI-friendly) or
through pcbnew's GUI per JLCPCB's own guides:

- [Generate Gerber and drill files in KiCad 8](https://jlcpcb.com/hk/help/article/generate-gerber-and-drill-files-in-kicad-8)
- [Generate BOM and centroid files from KiCad 8](https://jlcpcb.com/hk/help/article/generate-bom-and-centroid-files-from-kicad-8)

### Headless (kicad-cli — what an agent should run)

Run DRC first, then export; flags verified against KiCad 10's `kicad-cli`:

```bash
kicad-cli pcb drc board.kicad_pcb --exit-code-violations        # gate on DRC
kicad-cli pcb export gerbers board.kicad_pcb -o fab/ \
  --layers F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts \
  --subtract-soldermask                                          # 4+ layers: add In1.Cu,In2.Cu
kicad-cli pcb export drill board.kicad_pcb -o fab/ \
  --format excellon --drill-origin absolute --excellon-units mm \
  --excellon-zeros-format decimal --generate-map
kicad-cli pcb export pos board.kicad_pcb -o fab/board-cpl.csv \
  --format csv --units mm --side both --exclude-dnp   # CPL/centroid
zip -r fab.zip fab/
```

The BOM comes from the schematic side, already in JLCPCB's upload shape:

```bash
akcli jlc bom board.kicad_sch --qty 10 --csv fab/bom.csv
```

### GUI equivalents (per the JLCPCB articles)

*Gerbers*: **File → Fabrication Outputs → Gerbers (.gbr)** — select the copper,
paste, silkscreen, mask and `Edge.Cuts` layers (plus `In*.Cu` on multilayer
boards); check *Use Protel filename extensions*, *Subtract soldermask from
silkscreen*, *Check zone fills before plotting*, *Tent vias*. *Drill*: from the
same dialog, **Generate Drill Files** with *absolute* origin, *millimeters*,
*decimal* zeros format (a drill map is recommended). *CPL*: **File →
Fabrication Outputs → Component Placement (.pos/.csv)**. JLCPCB also publishes
a **Fabrication Toolkit** plugin (KiCad Plugin Manager) that emits all four
artifacts in one click.

### JLCPCB file expectations

| Artifact | Accepted | Column headers JLCPCB expects |
|---|---|---|
| BOM | `.csv`/`.xls`/`.xlsx` | `Comment, Designator, Footprint, LCSC Part #` — exactly what `jlc bom --csv` writes |
| CPL | `.csv`/`.xls`/`.xlsx` | `Designator, Mid X, Mid Y, Layer, Rotation` |

`kicad-cli pcb export pos --format csv` emits `Ref,Val,Package,PosX,PosY,Rot,Side`
— rename the header row to JLCPCB's names (`Ref→Designator`, `PosX→Mid X`,
`PosY→Mid Y`, `Rot→Rotation`, `Side→Layer`) or let the Fabrication Toolkit /
JLCPCB's order-page column mapper handle it. Rotation mismatches on polarized
parts (diodes, tantalums, connectors) are the classic PCBA defect: always review
JLCPCB's component-placement preview before paying.

Keep an `LCSC` parameter on every schematic component (see `jlc bom
--suggest/--fix`) — it is what makes the BOM column exact instead of a
value-guess, and JLCPCB's parts matching instant.

## Library use

```python
from akcli.parts import search as jlc

parts = jlc.search("NE555", limit=10)        # list[Part]
part  = jlc.get("C7593")                      # Part | None

# inject a transport (any urllib OpenerDirector-like object) to run offline/test:
parts = jlc.search("NE555", opener=my_fake_opener)

# optional short on-disk cache keyed by request URL, to avoid hammering the API:
parts = jlc.search("NE555", cache_dir="/tmp/akcli-cache", cache_ttl=3600)
```

Network failures raise `akcli.parts.search.JlcNetworkError` (clean
message, no traceback).

## Attribution

Part search is backed by the public **jlcsearch** service (tscircuit, MIT) with data
from **jlcparts** (MIT); EasyEDA/LCSC/JLCPCB are the underlying data sources. Full
notices in `THIRD_PARTY_NOTICES.md`.
