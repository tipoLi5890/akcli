# Design integrity: library workspace, contracts, fab policy, release gate

This page covers the design-integrity surface added after v0.7.0: reliable
format detection, footprint-library reading (`.PcbLib`/`.kicad_mod`), the
project library workspace (`akcli library`), schematic↔PCB equivalence
(`akcli verify`), design contracts (`akcli check --contract`), fab profiles
(`akcli fab`), and the release gate (`akcli release preflight`).

The common thread: **fail loudly, plan before writing, and keep policy in
versioned files with evidence** — never in hardcoded constants.

Not to be confused with `akcli check --integrity`, which is the *document*
integrity family (the write gate's nine document checks — dangling
endpoint/bus entry, duplicate uuid/designator, unresolved lib_id, instances
path, stacked power port, missing sheet file, no-connect conflict — run
read-only); see [docs/cli-reference.md](cli-reference.md).

## Reading footprint libraries

```bash
akcli read part.PcbLib --json
akcli read module.kicad_mod
akcli read board.kicad_sch --strict
```

- `.PcbLib` (Altium, OLE2) decodes each footprint storage: pads carry
  position/size/drill/shape/rotation, and tracks and arcs become `graphics`
  (lines, arcs, circles) on a KiCad layer. Anything not imported (text, fills,
  regions, 3D bodies, vias, keepout objects, graphics on a layer with no KiCad
  target) is surfaced as an `UNSUPPORTED_PRIMITIVE` warning with a count —
  never silently dropped. An unrecognized OLE2 container exits 5 instead of
  being read as an empty schematic.
- `read --json` metadata now carries `detected_format`, `detection_method`
  and `object_counts`; a non-empty source that normalizes to nothing raises
  an `EMPTY_IMPORT` warning, and `--strict` turns that into exit 1.

## Authoring a symbol from a pin table (and linting one you were given)

```bash
akcli library new-symbol pins.json -o MyLib.kicad_sym --apply
akcli library lint-symbols MyLib.kicad_sym
```

Hand-writing a wide IC symbol is where symbol libraries acquire their one
undetectable defect: two pins on the same connection point. KiCad treats that
point as a node, so every placement of the part joins those pins into one net —
and ERC reports nothing, because the *symbol* is what told it they are the same
node. A pin table, by contrast, is the part of a datasheet that transcribes
reliably. `new-symbol` splits the job along exactly that line: you declare
number, name, electrical type and body side per pin, and akcli computes every
coordinate (100-mil grid, group-then-order placement, body sized from the pin
counts and name widths, deterministic bytes). Both `side` and `type` are
required — a guessed side is a wrong pin position, and a defaulted
`unspecified` type is a pin KiCad's ERC exempts from every conflict rule.

The one sanctioned same-coordinate case is an explicit `stack_with` (same side,
same name, same type — KiCad's stacked-pin convention). Anything else is
refused, and the refusal is a **write gate**, not a warning: a
`SYMLIB_PIN_COINCIDENT` in the generated output exits 6 (`VERIFY_FAILED`) with
nothing written. That is a deliberate exception to the "geometry findings are
advisory" rule elsewhere in akcli, because this particular geometry *is*
connectivity.

`lint-symbols` runs the same checks over libraries akcli did not write —
imported, vendor-converted or hand-authored — plus the two hygiene rules that
travel with them: `SYMLIB_PIN_TYPE_UNSPECIFIED` (untyped pins, the ERC-noise
source in converted symbols) and `SYMLIB_PIN_OFF_GRID` (a pin no grid-drawn
wire can reach). Multi-unit symbols are evaluated per unit, so a quad gate
drawn on one body never produces a false positive. Full flag reference and the
finding-code table: [cli-reference.md](cli-reference.md).

## Importing an Altium PcbLib into a KiCad library

```bash
akcli library import-altium part.PcbLib --out vendor.pretty --courtyard 0.25
akcli library import-altium part.PcbLib --out vendor.pretty --courtyard 0.25 --apply
akcli library import-altium part.PcbLib --out vendor.pretty --layer-map Mechanical15=F.CrtYd --apply
```

Pads are carried over verbatim (positions, sizes, drills — never recomputed).
Tracks and arcs are imported as `fp_line` / `fp_arc` / `fp_circle`; text,
fills, regions, 3D bodies, vias and keepout objects are still skipped, each
with a warning and a count.

The arbiter is KiCad's own Altium importer (`kicad-cli fp upgrade` converts a
`.PcbLib`). Real libraries cannot be committed, so the comparison runs at
three distances: against output KiCad wrote for a synthetic library
(`tests/fixtures/kicad/altium_import/`, checked on every platform), against
the installed KiCad when there is one, and against real libraries a developer
supplies through `AKCLI_PCBLIB_SAMPLE` (`tests/test_pcblib_graphics.py`) —
pad for pad and, per KiCad layer, shape for shape.

Layers follow that importer (overlay → silkscreen, copper → copper, paste,
mask, `MechanicalN` → `User.N`). Two things are left to you rather than
guessed:

- **Mechanical layers have no fixed meaning.** Each one used raises a
  `MECHANICAL_LAYER` warning naming it until `--layer-map
  Mechanical15=F.CrtYd` says what it is. No courtyard is ever inferred from a
  mechanical layer number.
- **`KeepOut`, `MultiLayer` and `InternalPlaneN` graphics are skipped.**
  KiCad's importer converts them to rule areas or to one copy per copper
  layer; akcli writes plain graphics only, so it reports and skips them.
  `--layer-map` can still draw such a layer where you choose.

Transformations are **declared**, not silent: filesystem-safe renames, the
optional synthesized courtyard, the layer map that was applied and the
imported/skipped primitive counts are reported and recorded, together with the
source file's SHA-256 and the converter version, in the output directory's
`provenance.json`. Dry-run by default.

## The project library workspace

```bash
akcli library audit path/to/project
akcli library repair path/to/project --rename-footprint-lib footprint=proj_jlc
akcli library repair path/to/project --3d-path absolute --apply
```

`audit` cross-checks the project's schematics ↔ `sym-lib-table` /
`fp-lib-table` ↔ registered library contents ↔ 3D models. It catches, among
others:

| Code | Meaning |
| --- | --- |
| `FOOTPRINT_LIB_UNREGISTERED` | a symbol's Footprint field uses a nickname the fp-lib-table does not register — KiCad will say "footprint not found" |
| `FOOTPRINT_MISSING` | nickname resolves, but the footprint is not in that library |
| `LIB_URI_UNRESOLVED` / `LIB_PATH_MISSING` | a table URI uses an unresolvable `${VAR}` / points at nothing |
| `MODEL_MISSING` / `MODEL_NOT_PORTABLE` | a 3D model path does not resolve / is absolute (usable here, breaks elsewhere) |
| `FOOTPRINT_LEGACY_FORMAT` | a pre-v6 `(module …)` file: parseable via API but invisible to the KiCad GUI browser |
| `SYMBOL_LIB_UNREGISTERED` | a symbol's `lib_id` nickname is in neither the project nor the global sym-lib-table — the embedded copy still renders, but re-linking will fail |
| `SYMBOL_MISSING` | nickname resolves to a project library, but that symbol is not in it |
| `FOOTPRINT_FIELD_MALFORMED` | a Footprint field with no library nickname (`SOT-23` instead of `lib:SOT-23`) |
| `MODEL_UNRESOLVED` | a 3D model path uses a `${VAR}` that does not resolve here |
| `LIB_TABLE_MISSING` / `LIB_UNREADABLE` / `SCH_UNREADABLE` | no `sym-lib-table`/`fp-lib-table` at all (note), or a registered library / schematic whose bytes cannot be read (error) |

The global table is consulted when it can be read, but only the **project**
tables are treated as the project's own commitment. A nickname that resolves
in the global table produces no finding at all. If the global table cannot be
read, the finding is downgraded and says so — NOTE for a symbol nickname,
WARNING for a footprint nickname — instead of a false error. A nickname in
neither table is a WARNING (symbol) or an ERROR (footprint: KiCad will not
find the package).

Config `[[waiver]]` entries apply to `audit` (and to `verify <sch> <pcb>`)
exactly as they do to `check`: waived findings are named on stderr, counted in
a `config-waived: N (M demoted)` line (`--json`: `config_waived`, `waived`),
and the exit is computed from what is left. A finding's `refs` — what a waiver
scopes on — are the component designator for a per-component finding and the
table nickname (`nick`, or `nick:footprint`) for a library finding:

```toml
[[waiver]]
code = "FOOTPRINT_LIB_UNREGISTERED"
refs = ["J7"]
reason = "mechanical part, footprint assigned at layout"
```

`repair` productizes the two historically hand-`sed`-ed fixes as reviewable
plans (lossless S-expression rewrite, dry-run by default, `--apply` leaves a
`.bak` and re-audits). The re-audit decides the exit: `1` when an ERROR
finding remains in a file the repair rewrote; an error in a file it did not
touch is reported on stderr and does not fail the repair. `--json` adds
`post_audit: {errors, remaining, unrelated_errors}`.

`akcli project check [DIR]` complements `library audit` rather than
duplicating it: `audit` is scoped to the sym/fp-lib-table ↔ schematic ↔
library-contents ↔ 3D-model chain, while `check` looks one layer out, at
whether the `.kicad_pro` itself is coherent — its root-sheet binding matches
the schematic on disk (`PROJECT_ROOT_SHEET_MISSING`/`_MISMATCH`), every
`Sheetfile` in the tree exists (`SHEET_FILE_MISSING`), a board container is
present (`PROJECT_PCB_MISSING`), and derived state is gitignored
(`PROJECT_STATE_UNIGNORED`). `check` runs `library audit`'s own findings as
part of its report too, so a plain `akcli project check` alone is the single
project-integrity gate; `release preflight`'s `project` gate calls the same
`check_findings` function with library auditing switched off there, since
its own `library-audit` gate already covers that ground — one finding is
never reported twice across the two gates.

### Library baseline

`audit` answers "do the libraries resolve?". It cannot answer "are these the
libraries the last release was built from?" — a footprint edited in place, a
3D model swapped, a symbol library replaced all resolve perfectly. The
**library baseline** is the committed record that makes that question
checkable:

```bash
akcli library audit . --write-baseline
akcli library audit . --baseline
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --library-baseline akcli-library.baseline.json
```

`--write-baseline` writes `akcli-library.baseline.json` **beside the
`.kicad_pro`** (or `[paths].library_baseline`, or the FILE you name). Commit
it: it is a deliverable, like the lib-tables it describes — never state under
`.akcli/`. (It is deliberately not called a "lock": `library check-lock` is
the KiCad-GUI lock probe and `gate --lock` is the BOM lockfile.) For every
entry of the **project** `sym-lib-table` / `fp-lib-table` it records the
nickname, the uri as written, where that resolved relative to the project,
and a SHA-256 for each file: the `.kicad_sym`, every `.kicad_mod` in the
`.pretty`, a `provenance.json` left there by `library import-altium`, and
every 3D model those footprints reference. A model that does not resolve is
listed as such, never dropped.

Three things are recorded by reference only, because their bytes are not the
project's: libraries the schematics reach through the **global** table (by
nickname, under `global_libraries`), and any library or model referenced
through one of KiCad's built-in `${KICAD<N>_*_DIR}` variables. What those
resolve to depends on the KiCad installed on the machine — hashing them
would make the gate pass on one workstation and fail on the next.

`--baseline` (and the `library-baseline` gate of `release preflight`)
rebuilds the record from disk and reports every difference:

| Code | Severity | Meaning |
| --- | --- | --- |
| `LIB_BASELINE_DRIFT` | error | a file's hash, a table uri or a nickname differs from the record |
| `LIB_BASELINE_MISSING` | error | recorded, but gone on disk (a file, a table entry, a library that no longer resolves) |
| `LIB_BASELINE_UNTRACKED` | warning | on disk, but not in the record (a new footprint, a new table entry, a newly used global nickname) |

All three fail the release gate. After an *intended* library change, re-run
`--write-baseline` and commit the new file — the diff of that commit is the
review of the change. `refs` are the library nickname, plus `nickname:file`
for one file, so a `[[waiver]]` can cover one footprint file or a whole
library.

**Mark library files `-text` in `.gitattributes`.** The hashes are over raw
bytes. If git converts line endings on checkout (`core.autocrlf` on Windows,
or a blanket `* text=auto`), the same commit hashes differently on two
machines and every file reads as drift. Pin the bytes:

```gitattributes
*.kicad_sym  -text
*.kicad_mod  -text
*.step       -text
*.stp        -text
*.wrl        -text
akcli-library.baseline.json  text eol=lf
```

(`-text` stores and checks out the bytes exactly as committed; `text eol=lf`
on the baseline keeps the one file akcli writes itself at LF everywhere.)

## jlc add, integrated with the workspace

```bash
akcli jlc add C12345 --3d --footprint-lib proj_jlc --3d-path absolute
```

`--footprint-lib` sets both the output directory and the nickname written into
the symbol's Footprint field. Without it the footprints go to `footprint/` and
the nickname is read from the project's `fp-lib-table`: the one entry whose URI
resolves to that directory. With no such entry (or several) it stays
`footprint`, and stderr prints the exact table line to add; `jlc add` never
edits a lib table. `--3d-path` picks the 3D reference policy: `relative`
(portable, resolves only next to the library), `absolute` (always resolves on
this machine, not portable), or a `${VAR}` prefix. The trade-off is printed;
nothing is silently chosen.

## Schematic ↔ PCB equivalence

```bash
akcli verify board.kicad_sch board.kicad_pcb
akcli verify board.kicad_sch board.kicad_pcb --strict --json
akcli verify board.kicad_sch board.kicad_pcb --library
```

Compares refdes presence, footprint assignment, and the **pad-level net
partition** — net names are untrusted (auto-named nets differ legitimately);
what must hold is that pins joined on the schematic are joined on the board
and vice versa. Findings are located to the designator/pad.
Errors: `SCHPCB_MISSING_ON_PCB`, `SCHPCB_FOOTPRINT_MISMATCH`,
`SCHPCB_PAD_MISSING`, `SCHPCB_NET_SPLIT`, `SCHPCB_NET_MERGE`.
Warnings: `SCHPCB_EXTRA_ON_PCB`, `SCHPCB_VALUE_MISMATCH` (reported only under
`--strict` — a value edited on one side alone is routine; the `release
preflight` sch-pcb gate does act on it),
`SCHPCB_UNNETTED_PAD`, `SCHPCB_NO_PAD_DATA` (the board carried no pad-net
data, so the partition could not be compared — never a silent pass).
`#PWR`/`#FLG` pseudo-components are excluded.

**Assembly flags.** For a KiCad schematic against a KiCad board, the
schematic's DNP and "exclude from BOM" flags are compared with the
footprint's own `(attr … dnp exclude_from_bom)` attributes:
`SCHPCB_DNP_MISMATCH` and `SCHPCB_BOM_FLAG_MISMATCH` (warnings). A flag
changed on one side only is exactly how a BOM and a pick-and-place file come
to disagree; "Update PCB from Schematic" is the usual fix. An Altium
schematic is not compared — its reader cannot know these flags, and a
default is not a fact.

**Board-only footprints.** Mounting holes, fiducials and NPTH mechanical
parts live on the board without a schematic symbol. KiCad has its own marker
for that — the footprint's *Not in schematic* attribute (`board_only`) — and
`verify` honours it: such a footprint is still listed as
`SCHPCB_EXTRA_ON_PCB`, but as a NOTE that says why, instead of a WARNING.
There is no separate exception list to maintain: mark the footprint in KiCad,
or use a `[[waiver]]` for anything else.

### The board against its libraries (`--library`)

A board carries its own copy of every footprint, and nothing keeps that copy
in step with the library: a pad deleted or re-drilled on the board survives
every netlist check, and a footprint fixed in the library never reaches a
board that was not updated. `--library` compares each board footprint with
the definition its library id names in the **project** `fp-lib-table`:

| Code | Severity | Meaning |
| --- | --- | --- |
| `FPLIB_PAD_SET_DRIFT` | warning | pad numbers / pad count differ from the library definition |
| `FPLIB_PAD_DRILL_DRIFT` | warning | a pad's type or drill differs |
| `FPLIB_SOURCE_MISSING` | warning | the nickname is registered, but the library no longer holds the footprint |
| `FPLIB_MODEL_MISSING` | note | the board footprint has no 3D model, or its model path does not resolve |

Drift is a warning because a deliberate local edit is legitimate — the
message says so, and `refs` is the designator, so one `[[waiver]]` records
the decision. None of these changes the PASS/FAIL verdict of `verify`; in
`release preflight` they are the separate `pcb-library` gate.

**What this does not cover is printed every time.** Only footprints whose
library the project table registers are compared. Footprints from KiCad's
own libraries (the global table), from an unregistered nickname, or with no
library id are counted and reported — `5 of 6 board footprint(s) compared …;
1 NOT compared (1 library only in the global fp-lib-table)` — and `--json`
carries the same counts under `library`. A clean run is a statement about
the compared footprints, not about the board.

## Design contracts

```bash
akcli check board.kicad_sch --contract board.contract.toml
```

A contract file (TOML) asserts topology rules ERC cannot express, each with
datasheet evidence:

```toml
protocol_version = 1

[[contract]]
id = "reg-feedback-node"
evidence = [
  "ACME-REG datasheet, Table 7",
  { source = "ACME-REG datasheet", page = 12, url = "https://example.com/acme-reg.pdf" },
]
require = [ { pin = "U1.FB2", net = "VFB2" } ]
forbid  = [ { pin = "U1.FB1", net = "RAIL_A" } ]

[[contract]]
id = "usb-c-flip-pair"
require_same_net = [ ["J1.A6", "J1.B6"], ["J1.A7", "J1.B7"] ]
forbid_same_net  = [ ["J1.A2", "J1.B11"] ]

[[contract]]
id = "feedback-resistor"
component = "R7"
value = { nominal = "150k", tolerance = "1%" }

[[contract]]
id = "spare-gpio"
nc = ["U1.GPIO3"]

[[contract]]
id = "thermal-via"
waived = true
reason = "exposed-pad thermal vias approved"
owner = "hw-lead"
expires = "2027-01-01"
```

`component` + `value` checks a part's Value field; `nc` asserts a pin joins
no multi-pin net. `value` takes one of four forms:

| `value` | Passes when |
|---|---|
| `"150k"` | the Value is the same text (case- and space-insensitive) |
| `["150k", "154k"]` | the Value is the same text as any entry |
| `{ nominal = "150k", tolerance = "1%" }` | the Value is within ±`tolerance` of `nominal` (inclusive) |
| `{ min = "100n", max = "1u" }` | `min` ≤ Value ≤ `max` (inclusive; either bound optional) |

The two table forms compare numbers. Bounds and the Value field are read as
engineering notation — `150k`, `4k7`, `100n`, `2.2u`, `4R7`, `1e-7`, `150000`;
prefixes `p n u µ m k M G T`, case-insensitive except `m` (milli) / `M`
(mega) — optionally followed by a unit: `10uF`, `10uH`, `4k7Ω`, `100 ohm`
(a lower-case `f`/`h` counts as a unit only after a prefix, so `10uf` parses
and a bare `100f` does not). The Value field may also carry trailing BOM
qualifiers — tolerance, voltage or power rating, dielectric, chip size
(`150k 1%`, `100n X7R`, `10u/16V`) — read by its leading token; bounds may
not. No other spelling is parsed: free text or no text is a `CONTRACT_FAIL`
that says the value cannot be parsed — use the text forms for such parts.
`tolerance` is a percentage string (`"1%"`, `"0.5%"`, `"±1%"`); a bound may
also be a TOML number.

`evidence` is a list whose entries are strings or
`{ source = "...", page = 12, url = "..." }` tables (`source` required, `page`
a positive integer). Entries are appended to the finding message in file
order; a table renders as `source p.12 <url>`.

`waived = true` marks an approved exception and requires a non-empty `owner`
and `reason`; `expires` (ISO date) is optional.

A malformed file is rejected on load with `BAD_CONFIG` (exit 2): an unknown
key or unparseable bound in a range table, `min` greater than `max`, a
`tolerance` that is not a percentage, an evidence table without `source`, a
waived rule without `owner` and `reason`.

Pin specs `REF.PIN` match the pin number first, then the pin name, so rules
read like the datasheet. The report keeps three outcomes distinct: PASS
(`CONTRACT_PASS`, info), FAIL (`CONTRACT_FAIL`, at the rule's `severity`,
default error), and SKIPPED-BY-EXCEPTION (`CONTRACT_WAIVED`, note). An
expired exception raises `CONTRACT_EXCEPTION_EXPIRED` (warning) instead of
silently passing. Intent snapshots
(`check --intent`) keep doing net-membership regression; contracts express
policy — the two compose.

## Fab profiles and the order manifest

```bash
akcli fab profiles
akcli fab check board.kicad_pcb --profile jlc-4l-1oz-free-vias-2026-07
akcli fab check board.kicad_pcb --profile jlc-4l-1oz.toml --order order.toml
akcli fab check board.kicad_pcb --profile jlc-4l-1oz-free-vias-2026-07-r2 --project board.kicad_pro
akcli fab explain FAB_VIA_IN_PAD --profile jlc-4l-1oz.toml
```

A profile is one vendor capability **revision** with its sources
(`[source] urls` is mandatory — a profile with no evidence link is rejected
as `BAD_CONFIG`; `retrieved_at` is not enforced but is what dates the
revision, and a profile that omits it renders its retrieval date as `?` in
every report — see
`examples/fab/jlc-4l-1oz.toml`).

`--profile` takes a profile **file** or the **id of a bundled profile**
(`akcli fab profiles` lists them with vendor, source urls and retrieval
date). An existing file at the given path always wins, so a profile committed
to the project is never shadowed by a bundled one; otherwise the argument is
matched against the bundled ids (or packaged file stems); otherwise it is
`BAD_CONFIG`, listing the bundled ids. The bundled set is a byte-for-byte
mirror of the profiles under `examples/fab/`, so a bundled profile's SHA-256
is that of its repository original. A bundled profile is still one dated
revision: once its `retrieved_at` is more than 12 months old (or absent),
`fab check` adds a single `FAB_PROFILE_STALE` **note** — re-verify the numbers
against the source urls, upgrade akcli, or check against your own profile
file. A profile file never raises it; its revision discipline belongs to the
repository that owns it.

A published revision is never edited: a changed rule set is a new id in a new
file. `jlc-4l-1oz-free-vias-2026-07-r2` (`examples/fab/jlc-4l-1oz-r2.toml`) is
the 2026-07 snapshot number for number, plus a `[slot]` table.

What the check reads from the board, and how sure it is:

- **Outline** — every `Edge.Cuts` shape (lines, arcs, circles, rectangles,
  polygons; on the board and inside footprints) is chained into closed loops
  and measured exactly, cutouts subtracted. Board area and drill density use
  that area. If the shapes do not close, the area is the **bounding box** and
  the finding says so, with the reason; it is never passed off as the outline.
- **Via type** — `through`, `blind`, `buried`, `micro`. KiCad 10 marks a
  buried via itself; on an older board a `blind` via between two inner layers
  is reported as buried.
- **Tenting** — the tenting cap (`max_tented_drill_mm`) applies to a via the
  board file marks tented (KiCad 9+ board setup and per-via overrides; the
  plot option on older boards), or to every via when the order manifest
  declares `via_covering = "tented"`. When neither says, the cap is not
  applied and one `FAB_TENTING_UNKNOWN` **note** reports how many vias that
  leaves unchecked.
- **Project rules** — the board's own DRC minimums in the `.kicad_pro` beside
  it (`--project FILE` to name another) are compared with the profile's
  minimums; rules the project allows below them are listed in one
  `FAB_RULES_LOOSER_THAN_PROFILE` **warning**. No project file, or a rule the
  project does not state: not compared.

Severity policy:

- direct violations (paid small-via geometry, tented drill over the cap,
  via-in-pad, blind/buried/micro vias, a slot narrower than `[slot]` allows,
  stackup drift) are **errors**;
- cost-threshold crossings (board size/area, drill density, fine multilayer
  traces) are **warnings** with the actual value vs the threshold — or
  **errors** where the profile repeats the key under `[cost.error_if]`;
- boundary-exact geometry passes with a **note** ("minimum margin"); a via
  annular ring below the profile's *preferred* width but above its minimum is
  likewise a **note** (`FAB_VIA_ANNULAR_BELOW_PREFERRED`), and a profile
  source akcli cannot parse is a **warning** (`FAB_UNSUPPORTED_SOURCE`),
  never a silent skip;
- a registered `thermal_via` exception passes as an explicit note — and an
  expired one is an error;
- missing evidence (no Edge.Cuts outline) is itself a warning: the check
  never pretends it measured something it could not.

The order manifest is *declared purchase intent* — delivery format, surface
finish, via covering, material, copper weight. akcli validates completeness
and profile consistency but never derives these from the PCB; declaring ENIG
or panel delivery raises explicit review findings for the surcharge rules the
tool cannot compute offline. Those stay review items by design: V-cut and
panel features are not in the board file, and the ENIG exposed-copper area is
not measured — `ORDER_REVIEW_REQUIRED` says "NOT computed", never a number.

## Release preflight

```bash
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --contract board.contract.toml --fab-profile jlc-4l-1oz.toml --order order.toml --out release-manifest.json
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile jlc-4l-1oz-free-vias-2026-07 --drc --gerbers fab/ --out release-manifest.json
```

Runs every applicable gate, in this order:

| gate | needs | what it checks |
|---|---|---|
| `check` | `--sch` | ERC-lite / power / BOM / nets |
| `intent` | `--intent` | design-intent assertions |
| `contract` | `--contract` | design contracts |
| `library-audit` | `--sch` | library workspace resolution |
| `project` | a `.kicad_pro` beside `--sch` | the `project check` findings, minus a redundant second library audit |
| `library-baseline` | `--library-baseline`, `[paths].library_baseline`, or `akcli-library.baseline.json` beside `--sch` | library files vs. the committed [library baseline](#library-baseline) |
| `sch-pcb` | `--pcb` | schematic ↔ board equivalence |
| `pcb-library` | `--pcb` | board footprints vs. the project footprint libraries ([`verify --library`](#the-board-against-its-libraries---library)), with the compared / NOT-compared counts in its `summary` |
| `fab` | `--pcb` + `--fab-profile` | fab policy (a profile file or a bundled profile id); reads `--order`'s `via_covering` and the project file's design rules when present |
| `drc` | `--pcb` + `--drc` | KiCad's own DRC (`kicad-cli pcb drc`) |
| `order` | `--order` | order-manifest completeness |
| `sim` | `--sim` | quantitative SPICE assertions |
| `review` | `--review-policy` | the calibrated review allowlist |
| `gerber` | `--gerbers` | fab-package completeness |
| `git` | a git worktree | cleanliness (`--allow-dirty` records instead of failing) |

Gates without inputs are **skipped with a reason**, never silently green —
except that a *requested* `--sim` or `--drc` gate that cannot run (no
libngspice / deck build failure; no kicad-cli / no usable DRC report) FAILS
rather than skips. Exit 0 when every gate is `pass` **or** `skipped`; any
single `fail` exits 1. A skipped gate is therefore not a blocker — it is a
recorded absence of evidence, which is why the reason is written into the
manifest.

**The `drc` gate** runs KiCad's DRC on a staged copy of the board (plus its
`.kicad_pro`, `.kicad_dru` and `fp-lib-table`), because `kicad-cli pcb drc`
writes a `.kicad_prl` beside whatever board it is given: the project
directory is never modified. Zone fills are evaluated **as saved, not
refilled** — the gate judges the bytes being released, so refill and save in
KiCad first. Violations (`KICAD_DRC_VIOLATION`) and unrouted connections
(`KICAD_DRC_UNCONNECTED`) keep KiCad's severity, so the project's own rule
severities, exclusions and custom rules decide what blocks; config
`[[waiver]]` entries apply on top, matching on the KiCad rule type, the
designators, the `REF.PAD` pins or the nets involved. `--drc` without `--pcb`
is a usage error.

**The manifest** binds what was gated:

- `inputs` — a SHA-256 per input file; the fab profile as
  `{path, sha256, id, bundled}`; each recognised gerber/drill file by name in
  `gerbers.file_hashes`.
- `tool` — the akcli version and the installed kicad-cli's version (`null`
  when there is none).
- `git` — the revision and dirtiness, plus, when dirty, `dirty_sha256`: a
  SHA-256 over `git diff HEAD --binary` followed by the untracked,
  non-ignored paths (their contents are not hashed). Two manifests with the
  same revision and the same `dirty_sha256` were produced from the same dirty
  state **on that machine**; the value is not a cross-machine identifier,
  because the diff bytes depend on local git configuration (`core.autocrlf`
  on Windows).
- `gates` — each gate's status and findings.

## Writer safety: the GUI-open guard

```bash
akcli draw board.kicad_sch --ops edit.json --apply --allow-open
```

`draw`/`arrange`/`undo`/`relink-symbols`/`library repair --apply` refuse with `TARGET_LOCKED` (exit 6) while
KiCad's `~<name>.lck` lock file is present: a write under an open GUI is a
losing race — the GUI's later save overwrites the file from memory.
`--allow-open` is explicit risk acceptance; after applying, the CLI reminds
you to File>Revert in KiCad. (akcli can refuse to write; it cannot stop an
open GUI from saving later — the reminder is the honest remedy.)
