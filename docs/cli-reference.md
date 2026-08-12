# `akcli` CLI reference

`akcli` is the command-line entry point of `akcli`. It reads
Altium binary `.SchDoc`/`.SchLib`/`.PcbDoc`/`.PcbLib` and KiCad
`.kicad_sch`/`.kicad_sym`/`.kicad_pcb`/`.kicad_mod`, runs checks (including design-intent and
design-contract assertions), diffs revisions, verifies schematic ↔ PCB equivalence, audits and
repairs project library workspaces, gates manufacturing against versioned fab profiles, draws KiCad
schematics with a before/after net-connectivity diff, and provides 60 standards-cited engineering
calculators (`akcli calc`) — a pure-stdlib toolchain purpose-built for KiCad, with read-only
Altium import as the on-ramp.

> This reference is the contract for the CLI surface. It tracks the subcommands and flags defined in
> `src/akcli/cli.py`.

```
akcli [GLOBAL FLAGS] <subcommand> [ARGS...]   # doc-noqa (usage synopsis, not a runnable line)
```

**Convention:** `stdout` carries data (parsed JSON/text results); `stderr` carries logs and
diagnostics. This keeps `akcli ... --json | jq` clean.

## Global flags

| Flag | Effect |
|---|---|
| `--version` | Print package version **and** `protocol_version`, then exit. |
| `-h`, `--help` | Show help for `akcli` or a subcommand, then exit. |
| `-C`, `--config PATH` | Use this `akcli.toml` instead of walk-up discovery from the input file's directory. |
| `-v`, `-vv` | Increase log verbosity (to stderr). `-v` info, `-vv` debug-level logs. |
| `-q`, `--quiet` | Suppress non-error logs on stderr. |
| `--json` | Emit machine-readable JSON on stdout (carries `schema_version`). |
| `--no-color` | Disable ANSI color in text output. |
| `--debug` | Show full Python tracebacks instead of structured `ERROR: CODE` messages. |

## Subcommands

### `akcli read <file> [--md] [--strict] [--summary] [--match GLOB] [--limit N]`
Parse an Altium or KiCad schematic/PCB/library into the normalized model and print it.
- Input: `.SchDoc`, `.SchLib`, `.PcbDoc`, `.PcbLib`, `.PrjPcb`, `.kicad_sch`, `.kicad_sym`,
  `.kicad_pcb`, `.kicad_mod`.
- **Fail-loud format detection.** The extension picks the reader; a bare OLE2 container with an
  unknown extension is classified by its **storage layout** (`Board6` → PcbDoc, `Library` → PcbLib,
  `FileHeader` → SchDoc/SchLib) — never assumed to be a schematic. An unrecognized container exits
  `5` instead of silently returning an empty model. The `--json` metadata carries `detected_format`,
  `detection_method` (`extension`|`content`) and `object_counts`.
- **`EMPTY_IMPORT`:** a non-empty source that normalizes to zero objects prints an `EMPTY_IMPORT`
  warning (the classic silent-failure signature); `--strict` turns it into exit `1`.
- A `.PcbLib` decodes each footprint storage into the `Library` model's `footprints`
  (`FootprintDef`/`FootprintPad`: pad number/position/size/drill/shape/rotation, NPTH vs plated);
  undecoded primitives (silkscreen graphics, text, 3D bodies) surface as `UNSUPPORTED_PRIMITIVE`
  warnings, never dropped silently. `.kicad_mod`/`.pretty` read into the same model.
- A KiCad root sheet **recurses into its `(sheet ...)` children** (paths relative to the parent
  file, cycle- and depth-guarded); every sheet instance contributes its components under the
  designator from the matching `(instances (path ...))` entry.
- An Altium root **recurses into sheet symbols** the same way (RECORD 15/16/32/33; ports pair
  with their own sheet entry — Altium *Automatic* scope). A `.PrjPcb` reads the project's top
  sheet and honors `PowerPortNamesTakePriority`; if it lists **more than one** top-level sheet,
  only the first is read and a warning names the ignored ones (a schematic that cannot be scanned
  for sheet symbols also warns instead of being skipped silently).
- **Nothing is dropped silently.** Every `.SchDoc` RECORD id the reader does not model is counted
  and reported once per read as
  `warning: altium: skipped 5 unsupported record(s): 26 (Bus) ×3, 37 (BusEntry) ×2`
  (stderr, and in `--json` under `warnings`); counts are ordered most-frequent-first and unnamed
  ids print bare. Buses (RECORD 26 / bus entry 37) are **not** read — when they appear in that
  histogram the netlist is missing their connectivity. A RECORD-41 parameter that lands nowhere
  (owned by an unsupported record, e.g. a parameter set) is counted too, even though 41 itself is
  a supported id.
- **Altium footprints are model-type-aware.** A component links several models through the same
  record; only `ModelType=PCBLIB` (or an absent `ModelType`) is a footprint, and among those the
  `IsCurrent=T` one wins. A `SIM`/`SI`/`PCB3DLib` model name never reaches `footprint`, so a
  part with no footprint model reads as *missing* (and `check` raises `BOM_MISSING_FOOTPRINT`)
  instead of importing a SPICE model name that looks like a footprint.
- **`Comment="=<ParamName>"` is an indirection**, not a literal: it resolves (case-insensitively)
  against that component's own parameters. An unresolvable reference keeps the literal string and
  warns: `warning: altium: 1 unresolved Comment parameter reference(s) (imported literally): R1 '=Value'`.
- **Document parameters** (`Title`, `Revision`, `DocumentNumber`, `Date`, …) — the ownerless
  RECORD-41s that fill the title block — read into `title_block` in `read --json` (and in
  `--summary` when non-empty). For a hierarchical design this is the **root** sheet's.
- A `.PcbDoc` decodes both the ASCII sections (nets/components/classes/rules) and the **binary
  copper sections** `Tracks6`/`Vias6`/`Arcs6`/`Pads6` into `tracks`/`vias`/`arcs`/`pads`
  (mils, Altium's native +Y-up frame); `Fills6`/`Regions6`/`Texts6`/`Polygons6` are skipped.
- A `.kicad_pcb` decodes footprints, **pad-level net bindings** (both `(net N "name")` and KiCad 10
  `(net "name")` dialects, absolute pad positions with the footprint rotation folded in), tracks,
  vias (through/blind/micro), zones, and the board setup (`board`: copper layers, thickness, setup
  values, Edge.Cuts outline bbox). KiCad geometry stays in its native **mm, +Y-down** frame
  (`board.units == "mm"`), unlike the Altium PCB reader's mils — check `source_format`.
- `--json` prints the full `Schematic`/`Pcb`/`Library` export with `schema_version`; `--md` prints
  a human Markdown summary.
- `--summary` prints **counts + metadata only, never the full object arrays** — the
  context-budget escape hatch for big boards. `--summary --json` emits
  `{schema_version, source, format, counts, metadata[, warnings]}`; drill down afterwards with
  `akcli nets --match`, `akcli component --match`, or a full `read --json`.
- `--match GLOB` / `--limit N` filter and cap the listed objects (components/symbols/footprints;
  case-sensitive fnmatch). The `--json` metadata carries `total`/`matched`/`returned`/`truncated`.

### `akcli net <file> [NAME] [--at REF.PIN] [--same A.PIN B.PIN] [--why REF.PIN]`
Extract the netlist (net → pin membership) using the shared `netbuild` engine.
- With `NAME`, print just that net; a miss prints a notice (with a did-you-mean hint) to
  **stderr** and exits `8` (`QUERY_MISS`); `--json` additionally emits
  `{"found": false, "query": NAME, "kind": "net", "source": FILE}` on stdout so an agent can
  distinguish "net absent" from "file absent" (exit `4`) without parsing stderr.
- `--json` shares ONE envelope + net shape with `akcli nets`:
  `{schema_version, source, ..., nets: [{name, stable_id, members: ["REF.PIN", ...][, aliases]}]}`
  (the list branch adds the `total/matched/returned/truncated` throttle meta; the
  `NAME` branch adds per-net diagnostics — `is_named`, `source_names`, `confidence`,
  `merge_reasons`). One parser covers both commands.
- **Pin-level queries** (`REF.PIN`, split on the first dot; unknown pins exit `8`
  with a did-you-mean plus the valid pin list):
  - `--at REF.PIN` — reverse lookup: the net this pin is on (`net: null` when unconnected).
  - `--same A.PIN B.PIN` — binary connectivity: `{"same": true|false}` + the common net,
    and each pin's net name; always exits `0` when both pins exist (the answer IS the payload).
  - `--why REF.PIN` — attachment diagnosis for "why is this pin (not) connected":
    pin tip world coords, attachment status (`at_junction`/`at_wire_end`/`midspan_touch`/
    `label_on_tip`), the nearest wire end / label / other pin with mil distances, an
    `off_grid_50mil` flag, and actionable `hints` (built from the same quantized helpers
    `netbuild` uses, so the verdict never disagrees with the net engine). Geometry needs a
    `.kicad_sch`; other formats answer net membership only.
- **Buses (imported KiCad designs):** the engine models real bus semantics
  arbitrated against `kicad-cli`. A `(bus_entry)` conducts between its two ends
  (two wires ending on its two ends are one net even with no bus); an entry end
  attaches to a wire only at a wire endpoint or junction (a bare mid-span touch
  floats, same as a pin) but to a bus anywhere along a segment; the bus member a
  rip joins is chosen by the **wire-side label** (an unlabeled rip stays
  unconnected); `NAME[a..b]` vector expansion is inclusive at both ends in either
  order (`K[3..0]` → K3…K0); local bus labels are sheet-scoped while **global**
  bus labels merge member nets across sheets. So `akcli net`/`diff` on a
  bus-carried KiCad design reports the bus-carried nets correctly. A
  `(bus_alias ...)` declaration is deliberately **ignored** for netlisting —
  ground-truth tested against `kicad-cli` 10.0.4, an alias-labeled bus is
  member-less (identical to no alias), and a vector-looking alias name still
  expands as the vector — so `akcli` matches `kicad-cli` here by design.
- **Performance:** `netbuild` uses an `O(log n + k)` orthogonal-segment index, so
  connectivity on large ladder/bus sheets is near-linear (a ~5100-segment sheet
  builds in a fraction of a second); the semantics are byte-identical to the
  prior brute-force scan.

### `akcli nets <file> [--intent-snapshot OUT.json] [--include-unnamed] [--match GLOB] [--limit N]`
Print **every net → sorted members**, one line per net (`MID: C1.1, R1.2, R2.1`); unnamed nets
render as `<unnamed net_...>`. `--json` emits
`{source, total, matched, returned, truncated, nets: [{name, stable_id, members}]}`.
- `--match GLOB` filters by net name (case-sensitive `fnmatch`, e.g. `--match 'VDD*'`);
  `--limit N` caps the listing. A cut listing is visible in-band: the JSON envelope carries
  `total`/`matched`/`returned`/`truncated`, and text mode prints a `note:` to stderr — a filtered
  result can never be mistaken for the whole netlist.
- `--intent-snapshot OUT.json` additionally writes the netlist as a **design-intent JSON**
  document (`'-'` = stdout) that `akcli check --intent` consumes — the snapshot → edit → assert
  workflow. Named nets only by default; `--include-unnamed` also captures unnamed nets keyed by
  their `stable_id`.
- A snapshot round-trips: `akcli check <sch> --intent <snapshot>` on the unchanged schematic
  reports zero findings.

### `akcli component <file> [REF] [--match GLOB] [--limit N]`
Without `REF`: list components — compact rows (designator, library reference, value, pin count),
filterable with `--match GLOB` (on the designator) and `--limit N`; `--json` wraps the rows in the
same `total`/`matched`/`returned`/`truncated` envelope as `nets`.
With `REF`: that component's pin → net table. A missing `REF` prints a notice (with a
did-you-mean hint) to **stderr** and exits `8` (`QUERY_MISS`); `--json` additionally emits
`{"found": false, "query": REF, "kind": "component", "source": FILE}` on stdout.

### `akcli pins <lib_id> [--at X Y] [--rotation {0,90,180,270}] [--mirror {none,x,y}] [--symbols PATH ...]`
Op-list authoring helper: resolve a symbol (`Device:R`, `Timer:NE555P`, ...) from the same
sources the writer uses (repeatable `--symbols`, config `[paths]` `.kicad_sym` entries) and print
every pin's number, name, electrical type, owning unit, and **world coordinate** for the given
placement:

```
$ akcli pins Device:R --at 2000 1000 --symbols .../Device.kicad_sym
Device:R  @(2000,1000) rot=0 mirror=none
   pin  name       type       unit      x_mil     y_mil
     1             passive       1       2000       850
     2             passive       1       2000      1150
```

It mirrors the writer's `geometry.pin_world`, so a printed coordinate is byte-for-byte where
`draw` will place that pin — the exact point wires, labels, and power ports must land on. An
unresolvable `lib_id` is `SYMBOL_NOT_FOUND` (exit `6`). `--json` emits the table as objects.

Without `--symbols`, a standard nickname is resolved through the project and per-user global
`sym-lib-table`. Those tables address KiCad's bundled libraries as `${KICAD10_SYMBOL_DIR}/...`
— **variables KiCad compiles in** rather than exporting (a clean install stores them as `null`
in `kicad_common.json`), so akcli derives their default from the located `kicad-cli` install
(macOS `<KiCad.app>/Contents/SharedSupport/symbols`, Linux `/usr/share/kicad/symbols`, Windows
`<install>\share\kicad\symbols`; footprints/3D models likewise). An environment value always
wins: `export KICAD10_SYMBOL_DIR=/path/to/symbols` overrides the derivation, and is the fix
when KiCad is installed somewhere akcli cannot infer. `akcli doctor` reports whether this
resolution actually works here (the `symbols` check).

### `akcli bbox <lib_id> [--at X Y] [--rotation {0,90,180,270}] [--mirror {none,x,y}] [--symbols PATH ...]`
The spacing-planning sibling of `pins`: per unit, the drawn-**body box** and the **full box**
(body UNION pin tips) in world mils for a hypothetical placement, plus width/height and the
documented 400-mil spacing convention. Pin-only symbols (power stubs) fall back to the pin box
and say so (`pin_only`). Same transform chain as the writer, so the boxes can never disagree
with where `draw` puts the part. `--json` emits the boxes as objects.

### `akcli groups <sch> [--frame [--apply]] [--margin MIL] [--allow-open] [--symbols PATH ...]`
Functional-group inspection + visual frames. List mode reports every group recovered from the
hidden `Group` symbol property (written by grouped ops — see the groups envelope in
[docs/op-list-authoring.md](op-list-authoring.md)): members, world bounding box, and whether its
frame is present. `--frame` (dry-run unless `--apply`) draws one border rectangle + title per
group through the standard draw pipeline (`.bak`, verify gate, journal, `undo`); frames carry a
stable `key`, so a re-run after parts move **replaces** the stale border in place.

### `akcli check <file>`
Run the design checks (ERC-lite + power + BOM hygiene + nets + pairs + layout + document
integrity) and print findings.
- `-C/--config` supplies rails, MCU designator, the schematic grid, `[[erc_waiver]]`
  entries, and the checker-agnostic `[[waiver]]` table (below).
- `--erc` / `--power` / `--bom` / `--nets` / `--pairs` / `--layout` / `--integrity` / `--intent` /
  `--contract` / `--libsync` select check families (default: `erc`+`power`+`bom`+`nets`+`pairs`+
  `layout`+`integrity`; `layout` and `integrity` only run on `.kicad_sch`; `[check] pairs = false`
  opts `pairs` out of the default set; `intent`, `contract` and `libsync` are **opt-in only** —
  they never run by default).
- `--integrity` runs the write gate's document checks read-only over any `.kicad_sch` —
  `DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`, `DUPLICATE_UUID`, `DUPLICATE_DESIGNATOR`,
  `UNRESOLVED_LIB_ID`, `INVALID_INSTANCES_PATH`, `POWER_PORT_STACKED` (two net-naming power
  symbols on one coordinate — their rails merge invisibly), `SHEET_FILE_MISSING` (a `(sheet …)`
  whose child file is not next to it), `NO_CONNECT_CONFLICT` (WARNING — reported by the same
  pass but, alone among these, does not refuse a write) — so imported or
  hand-edited files get the same gate `draw --apply` enforces post-write. A wire end on
  another wire's mid-span counts as connected only with a `(junction)` there — eeschema's
  rule, and the one `akcli nets` applies, so the two never disagree.
- `--pairs` is **name-level continuity**: `PAIR_INCOMPLETE` (WARNING — a `_P`/`_DP`/`_H`/`+`
  net whose `_N`/`_DN`/`_L`/`-` partner does not exist; deliberately asymmetric, a lone
  `_N`/`_L` is the active-low convention and never fires), `PAIR_PIN_MISMATCH` (NOTE — both
  sides exist with different pin counts), and `BUS_GAP` (a numbered family `D0..D7` with
  internal index holes; WARNING for families ≥ 4 members, NOTE below; families need not start
  at 0). `[check] pair_suffixes = [["_P","_N"], ...]` replaces the suffix table and
  `bus_min_family` raises the family-size threshold; geometric diff-pair *skew* stays in
  `review analyze` (EMC family).
- `[check] group_clearance = N` (mils, `0` = off) makes `--layout` require that much channel
  between every pair of functional groups' extents (`LAYOUT_GROUP_CLEARANCE`, advisory) —
  the lint side of `arrange --groups --group-gap N`.
- `--nets` is **connectivity hygiene**: `NET_SINGLE_PIN` (a floating label or
  a power port driving nothing), `NET_OFF_GRID` (pins off the configured grid
  — wires that touch on screen without ever connecting), and, on `.kicad_sch`
  inputs, the attachment near-miss lints `NET_PIN_MIDSPAN_TOUCH` (a pin tip on
  a wire's mid-span with no junction — connected in Altium's dialect, NOT in
  eeschema), `NET_LABEL_UNATTACHED` (a label anchored on neither a pin tip nor
  a wire), and `NET_WIRE_CORNER_ON_PIN` (an L-wire corner landing on a pin tip
  — the classic accidental-short trap). The grid comes from config
  `[project] grid` (bare number = mils, or `"50mil"` / `"1.27mm"` / `"0.5mm"`
  strings; default 50 mil), compared in exact integer nanometres.
- `--layout` is a **geometric-overlap lint**: estimated bounding boxes for
  symbol bodies and label text, reporting symbols drawn over each other,
  labels running over a body or pin field, label-label overlaps, texts
  stacked on one anchor, plus `LAYOUT_POWER_ON_PIN` (a `PWR_FLAG` anchored on
  another symbol's pin tip — it belongs mid-wire, see the `place_pwr_flag`
  macro — or any power symbol whose **body** lands on the part. A rail port
  anchored at a pin tip with its body extending away is the blessed
  label-on-pin pattern and is **not** flagged),
  `LAYOUT_WIRE_THROUGH_SYMBOL`
  (a wire routed through a symbol body), and `LAYOUT_LABEL_OVER_WIRE` (NOTE:
  label text crossing an unrelated wire). ERC can never see these — a
  schematic can be electrically perfect and visually unreadable.
- `--erc` additionally reports `ERC_UNPLACED_UNIT` (WARNING, `.kicad_sch`
  only): a multi-unit part with units never placed (e.g. gate B of a dual
  comparator) — terminate them with the `terminate_unused_unit` macro.
  Waiver rule token: `unplaced_unit`.
- `--erc` also runs the **pin-type conflict matrix** (`ERC_PIN_CONFLICT`,
  waiver token `pin_conflict`): the high-signal cells of KiCad's default
  matrix — open-collector/open-emitter/tri-state pins mixed with push-pull
  `output`/`power_out` drivers on one net (WARNING; softer cells NOTE) — and
  **undriven POWER_IN** (`ERC_POWER_IN_UNDRIVEN`, waiver token
  `power_in_undriven`): a supply pin on a net that is neither a
  name-recognized rail nor driven by any `power_out` pin. Both are gated
  behind the type-confidence demotion like every type-based rule, so a
  mostly-Passive import degrades to NOTEs instead of emitting garbage.
- `--erc` also reports `NC_PIN_CONNECTED` (WARNING, waiver token
  `nc_pin_connected`): a pin carrying a no-connect marker that is
  nevertheless a member of a ≥2-pin net — the marker and the wire/label
  contradict each other (the classic accident: an edit wires up a pin that
  still carries a stale marker from an earlier revision). Works on any input
  format and sees label-only connections, because it rides on the inferred
  net membership; on `.kicad_sch` the wire-based subset of the same conflict
  is *also* caught pre-netbuild by `--integrity`'s `NO_CONNECT_CONFLICT` —
  the overlap is intentional defense-in-depth, not a duplicate bug.
- The **type-confidence demotion is per NET**, not per board. A type-based
  finding keeps its full severity when *every* member pin of the net in
  question carries a real electrical type, even on a board whose overall
  confidence is below the floor — the demotion exists because an *untyped*
  pin might be the driver the rule cannot see, and on a fully-typed net that
  excuse is gone. A net that still has untyped pins on a low-confidence board
  is demoted to a NOTE as before, and says so in the message. (The header's
  passive-pin ratio still reports the board-wide picture.)
- `--power` reports `POWER_RAIL_NO_SOURCE` (WARNING): a recognized rail with
  **≥2 consumers that nothing appears to feed**. "Feed" is read generously so
  the rule only fires on a rail nobody wired — any `power_out`/`output` pin, a
  `PWR_FLAG`, a connector/battery/test-point designator (`J`/`P`/`X`/`XP`/`CN`/
  `BT`/`TP`/`G`), a configured `[[rail]]` (a declaration of intent), an
  entry-style rail name (`VBUS`/`VBAT`/`VIN`/`DCIN`… — an off-board source is
  idiomatically drawn as a bare label), or any of those on a net the rail
  reaches through series parts (a rail behind a fuse/ferrite/diode inherits
  its upstream source) silences it. Ground is never crossed while searching,
  and DNP parts neither source a rail nor carry a source through.
- Presence-style checks ignore **DNP** parts: a Do-Not-Populate bypass cap
  does not satisfy `POWER_NO_DECOUPLING` (nor a DNP pull-up/load-cap/fuse/TVS
  the matching `review` rule). The BOM checks are where DNP parts still count.
- `--intent FILE` asserts a **design-intent JSON** file (written by
  `akcli nets --intent-snapshot`, or by hand) against the built netlist.
  Bare `--intent` (no value) uses the project's standing contract from
  `akcli.toml` `[paths] intent` — the "which intent applies to this board"
  question answered by config instead of convention:

  ```json
  {"protocol_version": 1,
   "mode": "exact",
   "nets": {"SWCLK": ["U1.4", "J2.2"],
            "GND": {"members": ["R*.2", "U?.1"], "mode": "subset"}}}
  ```

  `mode` is `"exact"` (the matched net must contain exactly the listed pins)
  or `"subset"` (containment only). Members are `"REF.PIN"` split on the FIRST
  dot, so pin names with dots parse (`"U1.P0.25"` = pin `P0.25`). Intent nets
  are matched to actual nets by **pin membership**, never by display name, so
  renames can't fake a pass. Findings (all ERROR): `INTENT_PIN_UNKNOWN`,
  `INTENT_NET_NOT_FOUND`, `INTENT_MISSING_MEMBER`, `INTENT_EXTRA_MEMBER`
  (exact mode only), `INTENT_NETS_SHORTED` (two intent nets landed on ONE
  actual net). A malformed file is `BAD_CONFIG` (exit `2`); a wrong
  `protocol_version` is `PROTOCOL_MISMATCH` (exit `6`); a missing file exits `4`.
  - **Per-net mode override** (additive; `protocol_version` stays `1`): a net
    value may be an object `{"members": [...], "mode": "exact"|"subset"}` that
    overrides the document `mode` for that one net; the classic plain-list form
    keeps using the document mode. An unknown key or a bad `mode` in the object
    form is `BAD_CONFIG`.
  - **Wildcard members**: the REF part of a member may be an `fnmatch` pattern
    (`"R*.2"`, `"U?.1"`, `"[UJ]1.3"`) — the pin part stays literal. A wildcard
    is satisfied when **at least one** actual pin matches both the REF pattern
    and the pin; zero matches raise `INTENT_MISSING_MEMBER` naming the pattern
    (never `INTENT_PIN_UNKNOWN`, which is literal-member-only). In `exact` mode
    wildcards are **ignored when computing `INTENT_EXTRA_MEMBER`** — only
    literal members are subtracted, so a pin present solely via a wildcard match
    still surfaces as extra (a wildcard asserts existence, not a closed set).
- `--contract FILE` asserts a **design-contract TOML** file against the built netlist — topology
  and semantic rules ERC cannot express, each carrying datasheet `evidence`. Rule kinds per
  `[[contract]]`: `require`/`forbid` (a pin on / off a named net), `require_same_net`/
  `forbid_same_net` (pin-pair topology), `component` + `value` (exact, space/case-insensitive, or a
  list of accepted values), and `nc` (a pin must not join a multi-pin net). Pin specs `REF.PIN`
  resolve the pin **number** first, then the pin **name** (`U1.FB2`). A rule with `waived = true`
  (plus `owner`/`reason`/optional `expires`) is an **approved exception**. The three outcomes are
  never conflated: `CONTRACT_PASS` (info), `CONTRACT_FAIL` (at the rule's `severity`, default
  error), and `CONTRACT_WAIVED` (note) — an expired exception raises `CONTRACT_EXCEPTION_EXPIRED`
  (warning) instead of silently passing. A malformed file is `BAD_CONFIG` (exit `2`), a too-new
  `protocol_version` is `PROTOCOL_MISMATCH` (exit `6`). Intent snapshots do net-membership
  regression; contracts express policy — the two compose. See
  [docs/design-integrity.md](design-integrity.md).
- `--libsync [--symbols DIR ...]` checks the freshness of the embedded
  `lib_symbols` cache (`.kicad_sch` only). With `--symbols` sources it
  compares **pin signatures** (number/name/type/position/unit) against the
  fresh libraries and warns `LIB_EMBED_STALE` on drift (graphics-only drift
  is deliberately silent); without sources it falls back to an old-format
  heuristic and notes `LIB_EMBED_OLD_FORMAT` (pre-20231120 document version
  or symbols missing `exclude_from_sim`), pointing at `akcli relink-symbols`.
- **Lint-style exit:** `--fail-on {info,note,warning,error,never}` sets the
  minimum (post-waiver) finding severity that exits non-zero. Default `warning`
  reproduces the historical behavior (exit `1` iff any finding ≥ WARNING);
  `never` always exits `0`. `--exit-zero` is a **deprecated alias for
  `--fail-on never`** (still works). The same pair is accepted by **every**
  findings-emitting command — `diff`, `pinmap`, `library audit`,
  `library lint-symbols`, `fab check`, and `gate` — one exit policy to
  learn, not one per command.
- **`[[waiver]]` config** (checker-agnostic, applied centrally before both
  rendering and the exit decision — independent of the ERC-only
  `[[erc_waiver]]`). Each entry: `code` (required; `fnmatch` glob like `"ERC_*"`
  or `"BOM_*"`), optional `refs` (string or list of `fnmatch` globs matched
  any-vs-any against a finding's refs; omit → all findings of that code),
  optional `severity` (`off`|`note`|`info`, default `off`: `off` drops the
  finding, `note`/`info` demote it), optional `reason` (free text — always
  supply one). An unknown key, missing `code`, or bad `severity` is
  `BAD_CONFIG` (exit `2`). The metadata header always prints
  `config-waived: N (M demoted)` (json/sarif metadata key `config_waived`), so a
  waiver-cleaned run is never mistaken for intrinsically clean.
- `--format text|json|sarif|junit` — `sarif` emits SARIF 2.1.0 for GitHub code
  scanning (stable `partialFingerprints`, schematic path as the artifact URI);
  `junit` emits JUnit XML for CI test reporters (WARNING+ findings become
  failed testcases). Exit semantics are unchanged by the format.
- **Structured finding positions** (present only when the checker located the
  finding — positionless findings keep their exact prior shape): text output
  appends a trailing ` @ (x,y)` clause (mils, model frame — top-left origin,
  +Y down; ints render without `.0`). JSON adds `"pos": [x_mil, y_mil]` and
  `"anchors": [{"kind","id"[,"pos"]}]` (`kind` ∈ component|pin|net|label; e.g.
  an overlap carries two component anchors). SARIF adds
  `logicalLocations` + `properties.akcli.{pos,anchors}` onto the result;
  `partialFingerprints` deliberately **exclude** pos/anchors so alert identity
  never churns when a part moves.

### `akcli review <analyze|facts|report|explain|propose|testbench|diff|tree|validate> ...`
Engineering design review: advisory findings with explicit confidence and evidence. Unlike
`check`, review findings never fail the build on their own — they exit `0` unless you opt in with
`--fail-on`. See [docs/review-rules.md](review-rules.md) for the full rule table.
- `akcli review analyze <sch> [--profile fast|standard|deep] [--detector NAME]
  [--pcb FILE] [--gerbers DIR] [--facts DIR] [-o FILE] [--fail-on info|note|warning|error|critical|never]` —
  runs the review detectors over a schematic (`--profile` picks the detector families;
  `--detector` repeatable to run only named detectors, overriding `--profile`; `--pcb`/
  `--gerbers` feed the pcb/cross and gerber-package checks; `--facts` points at a datasheet
  facts dir, auto-discovered as `<sch dir>/datasheets` when it holds `extracted/`; `--out`
  also writes the findings JSON envelope). Advisory by default (exit `0`); `--fail-on` turns
  on the same lint-style exit `1` as `check`.
- `akcli review facts <add|verify|lookup> ...` — the **datasheet facts store**: audited,
  PDF-pinned numbers that turn heuristic findings `datasheet_backed`. `facts add MPN --pdf FILE
  [--dir DIR] [--method manual|pdftotext|llm] [--set KEY=VAL@pN ...]` binds a fact to its source
  PDF by sha256; `facts verify [MPN] [--dir DIR] [--exit-zero]` audits the store (schema, PDF
  staleness, page bounds, quotes); `facts lookup MPN [KEY] [--dir DIR]` prints one MPN's audited
  facts. `--dir` defaults to the project-local `./datasheets` — the store is a committed
  team asset (deliverable-class, SPEC §10): review it in PRs and it survives handoff.
- `akcli review report <findings.json> [--format text|json|sarif|junit|markdown]` — re-renders a
  findings file (from `analyze --out`) in another format.
- `akcli review explain <CODE>` — prints one review rule: what it checks, the formula, and its
  provenance.
- `akcli review propose <findings.json> [-o FILE]` — turns findings into declarative candidate
  changes (op-list / contract / sim drafts); never touches design files.
- `akcli review testbench <sch> [--findings FILE] [--deck-only] [-o DIR] [--timeout S]
  [--exit-zero]` — auto-generates and runs subcircuit SPICE testbenches from quantitative
  findings (RC corners, divider ratios) with ngspice verdicts. Reuses a findings file with
  `--findings`, or runs `review analyze` in-process (standard profile) otherwise. `--deck-only`
  emits the decks without running ngspice (exit `0`); with `--deck-only`, `--out DIR` writes
  `<fingerprint>_<kind>.deck` files there.
- `akcli review diff <old.json> <new.json> [--fail-on-new]` (alias: `review drift`) — compares
  two findings files (fingerprint-aligned drift); `--fail-on-new` exits `1` when the later run
  adds findings. This is findings-drift — the net-level revision diff is `akcli diff`.
- `akcli review tree <sch>` — prints the schematic's power tree (rails, regulators, consumers).
- `akcli review validate <candidates.json> <sch> [--facts DIR] [-o FILE]` — gates LLM
  deep-review candidates: schema / anchors / datasheet evidence / masquerade checks; failures are
  quarantined with reasons.

### `akcli diff <file_a> <file_b> [--bom] [--fail-on SEV] [--exit-zero]`
Diff two schematic revisions. Nets are matched by **membership** (not display name); components by
UniqueID, then `(value, footprint, pin-count)` signature, then refdes. `--json` validates against
`schemas/diff.schema.json` (summary counts, rename map, per-component and per-net changes with
match method + confidence); net members are `"REF.PIN"` strings — the same encoding as
`net`/`nets`/`verify`.
`--bom` appends the per-component BOM delta: added/removed parts, value edits, order-id edits
and assembly-class flips (fitted↔dnp) — the "what does purchasing see" answer; cost deltas come
from diffing `jlc bom --lock` lockfiles.

### `akcli arrange <target.kicad_sch> [--apply] [--groups [FILE]] [--frames] [--grid MIL] [--margin MIL] [--group-gap MIL] [--page-width MIL] [--row-width MIL] [--allow-open] [--note TEXT] [--symbols PATH ...]`
Resolve symbol overlaps by nudging **free** components — parts with no wire
endpoint or label anchor on any pin (moving anchored parts would strand their
connectivity, which is exactly what `check --nets` flags). Greedy first-fit in
reading order; anchored overlaps are reported for manual fixing (exit `1`).
Dry-run by default; `--apply` writes through the draw pipeline (`.bak` +
connectivity re-verify), so `akcli undo` reverts an arrange. `--symbols`
supplies extra symbol sources for the write pipeline (same semantics as
`draw --symbols`). `--allow-open` writes even when a KiCad GUI lock file is
present (File>Revert in KiCad afterwards); `--note` records a free-form
intent note in the workspace journal.

With `--groups` each functional block relocates into its own shelf-packed
region as **rigid, net-preserving bundles** (moves carry labels/wires; power
satellites ride their host). `--groups FILE` takes a TOML/JSON
`{group: [refdes, ...]}` map; **bare `--groups`** derives the map from the
sheet's hidden `Group` properties — the file itself is the module map.
Net preservation is **enforced at apply time**: the moves are dry-applied to
a temp copy and the before/after netlists must be equivalent, otherwise the
write is REFUSED (exit `6`) with the split/merge lines on stderr — a sheet
wired *across* group boundaries cannot move rigidly (connect groups with
label-on-pin nets instead). `--allow-net-changes` is the explicit override.
The moves themselves run in **two phases through a staging band** (uniform
shift below the sheet, then per-bundle placement), so carried labels can
never be captured by a later mover whose original pins overlap an earlier
mover's slot; at genuinely coincident pin tips the writer leaves one label
behind per staying pin.

**`--propose-labels OUT.json` turns a refusal into a repair draft**: every
named net touching ≥ 2 grouped components gets an `add_net_label` on each
member pin (connectivity becomes name-backed and move-invariant), wire
clusters those labels make redundant are deleted (`delete_object` — stretched
cross-bundle wires are the failure mode, not a feature), and stranded `#`
satellites (PWR_FLAG, mid-wire ports) are re-seated onto a pin of their net.
The draft never touches the file: `akcli plan`/`draw` it, then re-run the
arrange. Unnamed multi-bundle nets are reported — they need a human-chosen
name first. Proven end-to-end on a real 88-part/10-group board
(`tests/test_corpus_pod.py`).
`--frames` redraws each group's border + title after packing (keyed uuids
replace stale frames; one `undo` reverts the arrange together with its
frames).

Group blocks stack straight down the page by default, `--group-gap` apart
(default 1200 mil); `--row-width MIL` wraps a group's internal shelf past
this width (default 4000). **`--page-width MIL` switches to 2D packing**: blocks go
side by side left→right, wrap past the page width, and `--group-gap` is
guaranteed on **both** axes — functional neighbours adjacent with a routing
channel between every pair. Project policy pins these in `akcli.toml` so
every session packs the same way (flags still override):

```toml
[arrange]
group_gap = 1000      # channel between group blocks, both axes (mil)
page_width = 20000    # pack side by side, wrap past this width
group_margin = 200    # clearance between bundles inside a group
row_width = 4000      # wrap a group's internal shelf past this width
```

The matching lint: `[check] group_clearance = 1000` makes `check --layout`
flag any pair of groups whose extents sit closer than that channel
(`LAYOUT_GROUP_CLEARANCE`, advisory) — so a later manual move that squeezes
the gap is caught even when nothing overlaps. The dry-run/`--json` report
now carries each block's `at`/`size` (mil) for programmatic verification.

### `akcli verify <file_a> <file_b> [--strict] [--exit-zero]`
Two modes, dispatched by the **second** file's type:
- **Schematic ↔ schematic** (`verify a.SchDoc b.kicad_sch`) — **net-equivalence proof** on top of
  the diff engine, the one-command answer to "did the conversion keep the circuit?". PASS (exit `0`)
  iff the component set matches and every net's **pin membership** is identical; nets that merely
  changed display name are listed but do not fail (conversions rename unnamed nets). Component
  value/footprint drift is reported as a note — `--strict` turns it into a failure.
- **Schematic ↔ PCB** (`verify board.kicad_sch board.kicad_pcb`, second file `.kicad_pcb`) —
  compares refdes presence, footprint/value assignment, and the pad-level **net partition** (net
  names are untrusted; what must hold is that pins joined on the schematic are joined on the board
  and vice versa). Findings, each located to the designator/pad: `SCHPCB_MISSING_ON_PCB` /
  `SCHPCB_EXTRA_ON_PCB`, `SCHPCB_FOOTPRINT_MISMATCH`, `SCHPCB_PAD_MISSING`, `SCHPCB_NET_SPLIT`
  (one schematic net across >1 board nets), `SCHPCB_NET_MERGE` (one board net shorting >1 schematic
  nets), `SCHPCB_UNNETTED_PAD`. `#PWR`/`#FLG` pseudo-components are excluded; `--strict` also fails
  on value mismatches. PASS (exit `0`) iff no ERROR-level finding.

`--json` carries the verdict (`equivalent`, `mode`) plus the findings/diff report.
`--exit-zero` always exits `0` regardless of the PASS/FAIL verdict (report mode).

### `akcli new [<path>] [--paper SIZE] [--title T] [--force]`
Bootstrap a minimal blank `.kicad_sch` (root `(uuid ...)`, `(paper ...)`, and an
optional title block) that `akcli draw`/`plan` can immediately append to — the
first-class replacement for hand-seeding a skeleton file. `--paper` sets the
sheet size (`A4`|`A3`|…, default `A4`); `--title` fills the title-block title;
`--force` overwrites an existing file (otherwise a present target is refused).
`--json` = `{created, target, paper, title, status}`. Keep the created file for
the whole session — its root uuid is the namespace for every deterministic op
UUID, so regenerating it breaks idempotent re-runs.

### `akcli undo <target.kicad_sch> [--apply] [--steps N] [--list] [--run RUN_ID] [--allow-open] [--note TEXT]`
Restore the target from the **rotated draw backups** that `akcli draw --apply`
writes under the workspace's `.akcli/backups/` (`<name>.bak` newest, then
`.bak2`, `.bak3` — up to 3). Legacy pre-0.12 stacks beside the file are still
found when `.akcli/backups/` holds none for the target.
Dry-run by default (prints the part/net delta that restoring would cause);
`--apply` swaps, so **undo twice is a redo**.
- `--list` prints the backup stack (level, path, size, mtime — newest first,
  contiguous from `.bak`) and exits; `--json` = `{target, depth, backups:[…]}`.
- `--steps N` walks back N snapshots at once (default 1), leaving the stack so a
  single subsequent `undo` redoes the last step; N clamps to the available
  stack. `--steps 0` is a usage error (exit `2`); `--steps` with no backup exits
  `4`.
- `--run RUN_ID` (0.20) restores **every file of a multi-sheet draw
  transaction** together, all-or-nothing, from
  `.akcli/backups/runs/<RUN_ID>/` (the `run_id` is in the draw's `--json`
  payload, its status line, and the journal). Three-way swap per file, so the
  **same command again is a redo**. Refuses (`exit 6`) when any file changed
  since the transaction — a swap would silently discard that later edit — and
  exits `4` for an unknown RUN_ID.
- `--allow-open` swaps even when a KiCad GUI lock file is present (File>Revert
  in KiCad afterwards); `--note` records a free-form intent note in the
  workspace journal.
- Exit `4` when no backup exists.

### `akcli pinmap <file> [--mcu REF] [--expected PATH] [--fail-on SEV] [--exit-zero]`
Emit the MCU pin → net table (MCU chosen by `mcu_designator` in config, or `--mcu REF`).
- `--expected PATH` cross-checks against an external expected pin→signal table (CSV or JSON). The
  schematic is authoritative; the expected table is advisory.
- `--json` emits the standard findings envelope, validated against `schemas/pinmap.schema.json`
  (each `PINMAP` finding is one pin→net row; `PINMAP_*` codes report expected-table mismatches).

### `akcli expected <file.dts|.dtsi|.overlay|.md|.markdown> [-o FILE]`
Extract an **expected pin→signal table** from a Zephyr devicetree source/overlay
(`gpios = <&gpioN pin ...>` phandles and Nordic `NRF_PSEL(...)` pinctrl) or from a
markdown pinout table (`--key-header`/`--value-header` pick columns explicitly).
Emits the JSON object `pinmap --expected` consumes; `-o` writes it to a file. Output is
**always JSON** regardless of the global `--json` flag (the one deliberate exception to the
"`--json` switches machine mode" contract — the product IS the machine format).
Exits `1` when nothing was extracted (an empty table would make `pinmap
--expected` vacuously pass), `2` on an unsupported input type, `4` when the
file is missing. The schematic stays authoritative — this table is advisory.

### `akcli calc [list | info <name> | batch <file> | <name> key=value ...]`
Offline **engineering calculators** (60): E-series snapping and 2–4-resistor
combination search (IEC 60063:2015), voltage dividers and LED resistors,
LM317/FB regulator networks with worst-case corners (TI SLVS044Y), IPC-2221B
track width ↔ current ↔ temperature rise and Table 6-1 clearance, via
R/thermal/ampacity/L/C (Johnson & Graham 1993), differential pairs
(IPC-2141A), Onderdonk/Preece fusing, ASTM B258 AWG,
microstrip/stripline/coax impedance (Hammerstad–Jensen 1980, Cohn 1954),
PI/TEE/bridged-TEE attenuators, L/PI matching networks (Pozar §5.1),
buck/boost/flyback power stages (TI SLVA477B/372C, Erickson ch. 6), LDO
dissipation, MOSFET gate drive (TI SLUA618A), current-sense shunts, NE555
(TI SLFS022I), op-amp gain pairs, comparator hysteresis (TI SLVA954),
Sallen–Key filters (TI SLOA024B), ADC resolution/settling, I²C pull-up window
(NXP UM10204), RS-485 fail-safe bias (TIA-485-A), CAN split termination
(ISO 11898-2), crystal load caps (ST AN2867), junction thermal (JESD51), TVS
selection (IEC 61000-4-5 surge), fuse derating (IEC 60127 R10), NTC inrush,
battery life (`battery` and the datasheet-mAh `battery-life`), LDO headroom
go/no-go (`ldo-headroom`), open-drain-aware comparator thresholds
(`comparator-hysteresis`), diode envelope-detector RC validity
(`envelope-detector`), unit conversions (dBm/W/Vrms, mil/mm, oz/µm), resistor
color/SMD/EIA-96 markings (IEC 60062:2016), galvanic compatibility
(MIL-STD-889C).
- Inputs take engineering notation (`4k7`, `100n`, `2M2`); **every result
  prints its formal reference**. `--json` returns
  `{calc, inputs, results{value,unit,note}, reference}`; `--md` renders a
  markdown table.
- **Input-suffix rule (already-milli units):** a parameter whose declared unit
  is itself milli-denominated (`battery-life`'s `capacity` in **mAh**, `i_avg`
  in **mA**) takes a **bare number** in that unit — `capacity=2500` means
  2500 mAh. A trailing engineering `m` there is **rejected** (`ERROR: capacity
  is already in mAh — write capacity=2500`) rather than silently applying a
  compounding 1000× milli. The generic length unit `m` (meters) is unaffected —
  `width=5m` still means 5 mm via the milli prefix.
- `battery-life`'s default `derating` is **0.8** (aligned with `battery`, so the
  two give identical hours for the same capacity/current); override with
  `derating=` for a chemistry/load-specific figure. `capacity=2500 i_avg=10`
  → 200 h / 8.33 d.
- `--ops FILE` (design-type calculators: `vdivider-design`,
  `regulator-design`, `led`, `i2c-pullup`, `crystal-caps`,
  `hysteresis-design`, `sallen-key`, `attenuator`) additionally emits a
  schema-valid op-list with the computed E-series values filled in
  (`-` = stdout) — edit coordinates, then `akcli plan`. Four of the eight emit
  a macro op (`vdivider-design` → `place_divider`, `led` →
  `place_led_indicator`, `i2c-pullup` → `place_pullup`, `crystal-caps` →
  `place_crystal`); the rest emit `place_component` ops.
- `batch <file|->` runs `{"jobs": [{"calc": ..., "params": {...}}, ...]}` and
  emits an array of result envelopes; exits `1` if any job failed (each
  failure carries an `error` field), `0` when all succeed.
- `list` groups all calculators; `info <name>` shows parameters, defaults and
  the citation. Both honour `--json` (`info` emits one entry in the same shape
  `list` uses, plus its own `name`). Bad name/params exit `2`, as does a
  non-finite (`nan`/`inf`) or degenerate input the formula cannot evaluate.
- Numerics are cross-checked in the test suite against KiCad's
  pcb_calculator (independent reimplementation — no GPL code) and published
  datasheet/handbook values. IPC-2152 is deliberately NOT included: it is
  chart-based licensed measurement data with no public closed form — this
  tool refuses to fake it (`tracktemp`/`trackwidth` use the conservative
  IPC-2221 fit instead).
- `akcli view calc` launches a local web UI for all calculators (localhost
  only): auto-compute forms with live notation parsing, physical-style SVG
  illustrations, ⌘K palette, session log, shareable URLs, op-list export.

### `akcli export <file> [--format protel|kicad|csv] [-o FILE]`
Export the schematic's **netlist** for other EDA tools. Default `--format protel` (an
Altium-importable `.NET`); `kicad` emits a legacy eeschema netlist; `csv` flat `net,ref,pin` rows.
Writes stdout unless `-o` is given. Deterministically sorted; unnamed nets are named by their
membership-derived `stable_id`, so re-exports diff cleanly. `--json` wraps the rendered netlist
in a `{schema_version, source, format, content}` envelope (the `content` string is byte-identical
to the plain output); for a structured net-by-net document use `akcli net --json`.
- `--format kicad` emits the sections KiCad's own `sch export netlist` writes, including the
  **UUID identity** a re-import needs: `(design)` (source/tool/sheet table), per-component
  `(sheetpath (names ..) (tstamps ..))` + `(tstamps <symbol-uuid>)`, `(libparts)` (fields +
  typed pins per distinct `lib_id`) and `(fields)` for extra properties (LCSC/MPN/Datasheet).
  KiCad 6+ keys the footprint↔symbol association by that UUID path, so a refdes-only netlist
  loses every association on a renumber. `(date "")` is deliberately **empty**: a wall-clock
  date would break the same-input-bytes → same-output-bytes rule.
  **Net names are eeschema's, not akcli's bare ones**: a sheet-scoped name (local or
  hierarchical label) is qualified with its sheet path — `/GND` on the root sheet,
  `/Amp/SIG` one level down, `/Amp/Deep/SIG` two — while a global label or power port is
  written bare (`VOUT`, `GND`). Every net also carries `(class "Default")`. pcbnew keys
  board nets by NAME, so a netlist that disagrees with eeschema renames the whole board
  the next time it is updated from the schematic. Only `--format kicad` qualifies names;
  `protel` and `csv` keep the bare akcli names, and `akcli net`/`nets` are unaffected.
  `tests/test_e2e_netlist.py` arbitrates the result against a real `kicad-cli` export
  (committed ground truth in `tests/fixtures/kicad/`, re-derived live wherever KiCad is
  installed) — component set, net→member partition, per-ref UUID path and net names must
  match. Note KiCad omits `#`-prefixed virtual symbols (power ports, `PWR_FLAG`) from its
  netlist while akcli keeps them: they are how the net engine names nets. Nets with no
  designed name are the one deliberate divergence: KiCad invents `Net-(R1-Pad2)` /
  `unconnected-(R2-Pad2)` from its own traversal order, which a normalized model cannot
  reproduce, so akcli keeps the membership-derived `stable_id`.

### `akcli plan <target.kicad_sch> --ops FILE [--symbols PATH ...] [--no-net-diff] [--render OUT.svg]`
Validate an op-list against `protocol_version` and `schemas/ops.schema.json`, resolve it against the
target `.kicad_sch` (symbols from repeatable `--symbols` sources and the target's inline cache), and
print what *would* change. Never writes. Includes the **Net changes** block (below);
`--no-net-diff` skips it. `--render OUT.svg` renders the WOULD-BE sheet from the same temp
dry-apply the net diff uses (coordinate-grid overlay included) — **look before you `--apply`**;
a refused op-list honestly skips the render, and a renderer failure is a warning that never
changes the verdict. The `--json` payload carries `preview: {path, bytes}` (or `null`).

### `akcli draw <target.kicad_sch> --ops FILE [--symbols PATH ...] [--apply] [--no-net-diff] [--root FILE] [--flat] [--strict-nets] [--allow-open] [--no-erc] [--render OUT.svg]`
Execute an op-list against a KiCad `.kicad_sch`. The vocabulary is 22 ops + 10 macros (see
`schemas/ops.schema.json`), including `delete_component` (with `cascade`) / `delete_object` /
`move_component` / `rename_net`, hierarchical `add_sheet` (below), and multi-unit placement via
`place_component`'s optional `"unit"` field.
- **Default is a dry run** (no file written): prints per-op results and the connectivity
  verification. `--dry-run` states it explicitly, and **wins over `--apply`** if both are given
  (a warning goes to stderr) — a defensively added flag can never cause a write.
- `--apply` performs the write via the atomic snapshot → temp → verify-on-temp → `os.replace`
  pipeline, writing a rotated `<target>.bak` copy under `.akcli/backups/`. The write is rejected (exit `6`)
  if any op errors or the connectivity verifier finds an ERROR (e.g. `DANGLING_ENDPOINT`,
  `DANGLING_BUS_ENTRY` — a bus entry end landing on neither a bus nor a wire).
- **Net changes** (both `plan` and `draw`, dry-run and apply): the op-list is dry-applied to a
  temp copy placed *next to the target* (so a hierarchical root still resolves its child
  sheets) and the before/after netlists are diffed by **pin membership** (never by name).
  The block prints one deterministic line per change, most severe first:

  ```
  Net changes:
    ! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.2>(2)
    ! MERGE MID + +3V3 -> MID
    ~ VTH: +U1.7 (5->6 pins)
    = RENAME MID -> VOUT (3 pins)
    + NEW BALL1_N (5)
    - GONE SENSE1 (3)
  ```

  `(none)` when connectivity is unchanged; the block is suppressed (not shown as "(none)") when
  the dry-apply itself fails. `--no-net-diff` skips the computation. If the diff cannot be
  computed at all (unreadable target, missing child sheet, ...) a
  `WARNING: net diff unavailable: ...` line goes to stderr — never a silent skip.
- **Sheet-tree scope (0.20).** When the target is a **child sheet of a hierarchy**, the net
  diff is evaluated over the **whole sheet tree**, not the child alone (a child read standalone
  is a truncated graph — its global labels lose every member the other sheets contribute). The
  root is auto-discovered by scanning the project directory for the file that instantiates the
  target; `--root FILE` names it explicitly (a root whose tree does not contain the target is
  `PROJECT_ROOT_MISMATCH`, exit `2`), and when several roots instantiate the target the run
  refuses to guess (`PROJECT_ROOT_AMBIGUOUS` — under `--strict-nets` the write fails closed;
  advisory runs degrade to `net diff unavailable`). `--flat` restores the single-file view
  explicitly. Tree-scoped runs stage the tree under `.akcli/stage/` (cleaned up afterwards) and
  stamp `project_root` + `evaluation_scope: "tree"` into the `--json` payload — both fields are
  **absent** on the flat path, whose output is byte-identical to 0.19.
- **Multi-sheet op-lists (0.20).** The envelope's `sheets` map (`{"amp": {"file":
  "amp.kicad_sch"}}`) + per-op `"sheet"` tags route ops onto declared child files, so ONE
  op-list edits the root and its children in **one atomic transaction**: every dirty file is
  staged and verified before the first write, the pre-write bytes of every file are copied to
  `.akcli/backups/runs/<run_id>/`, and a partial commit rolls back automatically
  (`TXN_ROLLBACK_FAILED` only when even the rollback fails — the backup set is intact and
  `akcli undo <target> --run <run_id> --apply` restores it). The `--json` payload carries
  `run_id` + per-file `files[]` on these runs (absent on single-file runs). A sheet
  instantiated more than once refuses a scalar designator (`INSTANCE_REFS_REQUIRED`) — pass
  `instance_designators` maps; see
  [docs/op-list-authoring.md](op-list-authoring.md).
- `--strict-nets` (with `--apply`): **refuse to write** (exit `6`, evidence lines on stderr)
  when the diff shows a split or merge touching a *named* net on either side — splits/merges of
  unnamed nets are ordinary wiring edits and pass. It forces the diff even under `--no-net-diff`,
  and it fails **closed**: when the diff cannot be computed, the write is refused
  (`REFUSED: --strict-nets: net diff unavailable (...)`) rather than waved through.
- **GUI-open guard.** `--apply` refuses with `TARGET_LOCKED` (exit `6`) when KiCad's `~<name>.lck`
  lock file is present — a write under an open GUI is a losing race (the GUI's later save overwrites
  it from memory). `--allow-open` is explicit risk acceptance, and a successful apply under an open
  GUI prints a `File>Revert` reminder on stderr. (Same guard on `arrange`/`undo --apply`, and on
  the `relink-symbols`/`library repair` apply paths.)
- A final status line states the outcome unambiguously:
  `status: dry-run — nothing written (re-run with --apply)`,
  `status: APPLIED — wrote board.kicad_sch (backup .akcli/backups/board.kicad_sch.bak; akcli undo reverts)`, or
  `REFUSED: --strict-nets: net split/merge touches a named net; nothing written`.
- After a successful `--apply`, an **advisory** `kicad-cli` ERC runs when that binary is
  installed (never fatal); `--no-erc` skips it honestly (logged at `-v`) — akcli's own
  connectivity gate always runs regardless.
- `--json` payloads validate against `schemas/draw-result.schema.json`
  (`schema_version`/`applied`/`status`/`ops`/`connectivity`, plus
  `"net_diff": {"equivalent", "risk", "lines"}` or `null` when unavailable).
- **`add_sheet` (hierarchical authoring, KiCad only — `altium: false`):** emits a
  `(sheet …)` node with `Sheetname`/`Sheetfile`, deterministic uuids, and
  edge-computed sheet pins. Op shape:
  `{op:"add_sheet", name, file, at:[x,y], size:[w,h], pins?:[{name, type, side, offset_mil}]}`
  (mils; `at` = top-left corner). `type` ∈ input|output|bidirectional|tri_state|passive;
  `side` ∈ left|right|top|bottom. **Wires attach to a sheet pin by coordinate**
  (`at` + `offset_mil` along the side, grid-snapped) — there is no
  `Sheet.Pin` wire endpoint. A cross-sheet net is a parent sheet-pin paired with
  the child's same-name hierarchical label. The referenced child `.kicad_sch` is
  **not** created by `add_sheet` and must ALREADY exist next to the parent —
  run `akcli new <child>.kicad_sch` first, or the op is refused with
  `SHEET_FILE_MISSING` (exit `6`). A stub is enough; it does not have to be
  finished. The same code reports a root whose child later went missing, since a
  dangling `(sheet …)` makes every `nets`/`check`/`render` on the design fail.

### `akcli relink-symbols <target.kicad_sch> [--libs DIR ...] [--only NICKS] [--apply]`
(also available as `akcli library relink` — the library-maintenance family it belongs to)
Re-embed **stale `lib_symbols` cache entries** from fresh `.kicad_sym` libraries — the fix for
KiCad's `lib_symbol_mismatch` ERC noise on files carrying old embedded symbols. For each embedded
`Nick:Name` entry it resolves `<libdir>/<Nick>.kicad_sym` (`--libs` takes dirs or `.kicad_sym`
files, repeatable; default: the KiCad.app SharedSupport symbols dir when it exists) and
classifies it `up-to-date`, `replace`, or `missing-lib` (comparison is whitespace-insensitive,
so formatting alone never triggers a replace):

```
$ akcli relink-symbols board.kicad_sch
  replace     Device:R  [/Applications/KiCad/.../symbols/Device.kicad_sym]
  replace     power:GND  [/Applications/KiCad/.../symbols/power.kicad_sym]
status: dry-run — 2 replacement(s) pending; re-run with --apply
```

- Dry-run by default. `--apply` splices the fresh blocks in, then a **safety gate** re-reads
  both versions and requires identical net membership — a moved pin in the new library refuses
  the write with `VERIFY_FAILED` (exit `6`) and leaves the file untouched. The spliced document
  must also pass the same connectivity gate `draw --apply` runs, compared as a **delta**: a
  finding the splice INTRODUCES refuses the write, while findings the file already carried do
  not (relink is not the tool that repairs a broken import). On pass it writes
  `.akcli/backups/<name>.bak` and replaces atomically.
- `--only power,Device` restricts to the listed library nicknames (or full lib_ids).
- Exit `6` when any entry is `missing-lib` (scope with `--only` to silence intentionally
  unavailable nicks); `--json` lists the actions (minus the raw symbol text).

### `akcli ops <list | template OP [--required-only] | validate FILE | from-intent DESIGN [-o FILE] [--symbols PATH ...]>`
Op-list authoring kit: `list` prints the 22-op vocabulary with required fields
and per-executor support, plus the **10 macro ops** (`connect_and_label`,
`place_pwr_flag`, `terminate_unused_unit`, `place_divider`, `place_decoupling`,
`place_pullup`, `place_led_indicator`, `place_rc_filter`, `place_crystal`,
`place_array`)
that expand to core ops before validation; `template` emits a fill-in JSON
op-list skeleton for any op or macro (guide:
[docs/op-list-authoring.md](op-list-authoring.md)). A mistyped op or
calculator name gets a did-you-mean suggestion.
- `validate <oplist.json>` runs the **target-free structural validation** —
  envelope, per-op fields, macro expansion; exactly the checks `plan`/`draw`
  run before touching a target. Exit `0` valid / `6` on any problem; `--json`
  emits `{protocol_version, valid, ops_sha256, op_count, errors}`. The
  plugin's PreToolUse hook runs this before any `draw --apply` and blocks on
  failure; the hook also warns (never blocks) when the workspace journal shows
  no prior `plan`/dry-run for that exact op-list.
- `from-intent <design.json>` **generates** a complete op-list from a design
  document: the `check --intent` pin→net table plus a `components` map and
  optional `layout` hints (`schemas/design-intent.schema.json`) — the pin→net
  table is the authoring interface, the tool owns the geometry. Emits only the
  ordinary vocabulary (`place_component` + label-on-pin `add_net_label` +
  `add_no_connect` for every unlisted pin — opt out with the design doc's
  `layout.no_connect_unlisted: false`; the other `layout` knobs are `origin`,
  `columns`, `margin_mil` and per-component `group`) into a uniform
  50-mil-grid cell
  layout, so the output flows through the unchanged `plan`/`draw` pipeline;
  `-o FILE` writes it (default stdout), `--symbols` adds symbol sources using
  the same resolution chain as `plan`/`draw`. Multi-unit symbols place ALL
  units in adjacent cells (unreferenced units all-no-connect). Deterministic:
  identical design content → identical output bytes. Refusals exit `6` with
  `DESIGN_UNSUPPORTED` (pin addressed by NAME — the message names the correct
  number — wildcard members, a REF missing from `components`, one pin in two
  nets, coincident stacked pins on different nets, bus-syntax net names,
  multi-unit parts with common unit-0 pins); an unknown lib_id is the usual
  `SYMBOL_NOT_FOUND`. The SAME design file then asserts the drawn sheet:

```bash
akcli ops from-intent design.json -o ops.json --symbols MyLib.kicad_sym
akcli draw board.kicad_sch --ops ops.json --symbols MyLib.kicad_sym --apply
akcli check board.kicad_sch --intent design.json
```

### `akcli view [calc|live|SCH] [PATH] [--port N] [--no-browser] [--state-dir DIR] [--max-steps N]`
ONE local dashboard server for both pages (127.0.0.1 only, zero dependencies,
HTML bundled in the package). Default port `8765`, auto-incrementing when
busy. `/` is the **hub** — the entry page the browser opens on launch, with
one card per dashboard (the live card shows the watched file, step count and
latest ERC state in real time; `C`/`L` jump straight in).
Bare `akcli view` needs no file: it serves the hub with `/live` idle.
`view <sch.kicad_sch>` is shorthand for `view live <sch>`; `view live` with
no path watches the single `.kicad_sch` in the current directory (none or
several → explicit usage error, exit 2); `view calc` serves `/calc` alone.
- `/calc` — the calculator bench: home launcher + grouped sidebar with fuzzy
  filter, ⌘K command palette, forms that auto-compute as you type with live
  engineering-notation parse hints (defaults shown in typed-back notation,
  e.g. `35u`), results with formal references, change chips vs the previous
  run, click-to-copy values, copy as markdown/JSON/CLI, diagram captions
  annotated with the computed values, a session log, shareable URL hashes,
  theme-aware physical-style SVG illustrations, and one-click op-list export
  for the mappable design calculators (`GET /api/ops`, the web twin of
  `calc --ops`).
- `/live` — watches the schematic; every on-disk change (e.g. an
  `akcli draw --apply`) exports every sheet's SVG via `kicad-cli` and appends
  a step to the timeline **immediately**; KiCad's JSON ERC back-fills the
  step seconds later (badge `ERC…` while pending). Updates are pushed over
  Server-Sent Events (`/live/events`), with a slow poll as fallback. The
  dashboard: inline SVG with zoom/pan/crop, sheet tabs for hierarchical
  designs, per-step ERC badges plus a violation panel that marks **NEW**
  findings vs the previous step (with click-to-locate markers on the sheet),
  diff mode (previous step ghosted in red), timeline replay, PNG export,
  parts-trend sparkline, a note box that annotates the next step
  (`POST /live/note`, the UI twin of `note.txt`), and a clear-timeline
  action. A **live offline-lint overlay** (toolbar `lint` button / key `G`)
  draws dashed-square markers at each finding's position from
  `GET /api/findings` — a fast, offline nets+geom+layout lint of the watched
  sheet (never `kicad-cli`/network, mtime-cached), converting the finding's
  mil position into the SVG's mm frame (`MIL_TO_MM = 0.0254`); markers are
  click-to-zoom and suppressed on multi-sheet views (positions are root-frame
  only). When the BOM panel runs its networked `?check=1` pass, each priced
  line with an LCSC id gains a **datasheet link** (resolved via
  `parts.datasheet.resolve`; direct PDFs vs page-links get distinct glyphs) —
  per-line failure-tolerant and absent on the offline BOM.
  Steps live in a per-run temp dir; `--state-dir DIR` persists the
  timeline across runs and `--max-steps N` bounds it (default 500 — oldest
  SVGs are deleted; `0` = unlimited). Needs `kicad-cli` (KiCad 8+) on
  `PATH`, in the macOS app bundle, or via `KICAD_CLI=`. `AUTO_REVERT=1` asks
  an open KiCad editor to File→Revert after each step (macOS only).

### `akcli jlc <search|show|bom|datasheet|add> ...`
JLCPCB/LCSC part search (`jlc search <query> [--limit N] [--category NAME]` —
`--category` keeps only results whose category equals NAME, case-insensitive
exact match: a value/package query like `0402 47K` otherwise mixes NTC
thermistors from `Sensors` into a resistor search; the filter is client-side
with over-fetch, so `--limit` applies to the filtered list), single-part
inspection (`jlc show <C-number> [--easyeda]` — one part's catalog record;
`--easyeda` also queries EasyEDA for metadata + 3D-model availability), BOM
purchasability check (`jlc bom <sch>...` — stock/tier-price/est-cost
per BOM line via LCSC/MPN parameters; several schematics merge into ONE cart with per-board
breakdowns and tier pricing at the merged quantity; `--qty N` evaluates at build quantity).
Components carry their **assembly class** everywhere (fitted / dnp / external / no-part — from
KiCad `dnp`/`in_bom` attributes, `Sourcing` parameters and structural refdes classes, tunable in
`akcli.toml [bom]`): dnp/external/no-part lines are never looked up or priced, coverage counts
fitted parts only, and every explicit C-number is **reverse-verified** against the schematic's
value + package (on disagreement the line is marked `verified: "mismatch"` and its note carries
`BOM_LCSC_MISMATCH` — a BOM-line state, not a `check` finding; a mistyped id is a wrong reel,
not a formality). `LCSC_ALT` parameters supply second sources that take over automatically when the
primary is at risk. Flags: `--suggest`/`--fix`
find and write catalog replacements for missing/dead part ids — matches are graded by
NORMALIZED value equality (never substring), `--fix` writes only
high-confidence matches, `--fix-all` also writes low-confidence ones; `--alternates` proposes
second sources for at-risk lines and notes Basic swaps for Extended passives; `--csv OUT.csv`
exports a JLC-EDA-template BOM CSV (every class kept, annotated in a Note column; `'-'` =
stdout); `--md [OUT.md]` renders a Markdown report; `--lock OUT.json` freezes the check as a
lockfile and `--against-lock LOCK.json` reports drift (price/stock/EOL/id changes, exit `1`);
`--offline` answers from the HTTP cache only (misses degrade to `unverified`, bannered);
`--min-stock N` flags lines with stock below N (default `1`, or `akcli.toml [bom].min_stock`);
`--exit-zero` always exits `0` (report mode), **datasheet resolution/download**
(`jlc datasheet <C-number|MPN|sch> [--fetch] [--force] [--resolve-mpn] [--exit-zero] [-o DIR]`
— resolves szlcsc PDF links via the EasyEDA record, `%PDF`-magic-verified downloads, whole-BOM
batch mode; `--force` re-downloads even when the file already exists; `--resolve-mpn`
exact-matches MPN-only BOM lines against the catalog (same policy as `jlc bom`) to pin a
C-number before resolving; files cache under `AKCLI_DATASHEET_DIR` or
`~/.cache/akcli/datasheets/`), and library conversion (`jlc add C<num> [--3d]
[--footprint-lib NICKNAME] [--3d-path relative|absolute|'${VAR}']` → KiCad symbol + footprint +
optional STEP). `--footprint-lib` sets **both** the output directory and the fp-lib-table nickname
written into the symbol's Footprint field — pass the nickname your project registers, or KiCad
reports "footprint not found" (the field previously hardcoded `footprint:`). `--3d-path` picks the
3D-model reference policy — `relative` (portable, resolves only next to the library), `absolute`
(always resolves on this machine, not portable), or a `${VAR}` prefix — and prints the trade-off.
`-o DIR` overrides the output directory (default `<[paths].parts_dir or ./akcli-parts>/<C-number>/`);
`--lib-name` names the KiCad symbol library (default `akcli`); `--force` overwrites existing
artifacts; `--place --designator U1 --at X Y` also emits a ready-to-draw `place_component`
op-list.
This is the only **networked** subcommand family. Network failures exit `7`
(`ERROR: NETWORK: ...`); transient errors are retried with backoff, and a stale cached response is
served with a warning when retries are exhausted. See [docs/jlc.md](jlc.md) for the full reference.

### `akcli sim <sch> [--sim FILE] [--deck-only] [-o PATH] [--gnd NET] [--wave OUT.csv] [--sweep NAME=v1,v2,...] [--timeout S] [--exit-zero]`
Simulate a schematic with **libngspice** and assert on the results. `akcli sim` renders the
schematic to a SPICE deck (net → node mapping, component → device via the model-resolution ladder,
`sim.json` stimuli → `V`/`I`/`B` sources), runs it in an **isolated child subprocess** (crash- and
timeout-safe), reads back the `.meas` measurements, and compares each against a bound declared in
`sim.json` (`gt`/`lt`/`ge`/`le`/`approx`+`tol`, engineering notation accepted; a lower + an upper
bound in one entry forms a two-sided range). `--deck-only` emits the deck and exits `0` **without**
ngspice (the engine-free plan/review mode — write it with `--out`, or `--json` for
`{deck_sha, deck, warnings, unmodeled}`); otherwise `--sim FILE` is required. `--gnd NET` names the
net that becomes SPICE node `0` (default `GND`). `--wave OUT.csv` dumps the simulated vectors as a
**tidy CSV** (one `time` column + one column per `options.wave_vectors` entry). `--sweep
NAME=v1,v2,...` re-runs the assert pass across a Cartesian corner matrix (component-value override
`R21=2.2k,3.3k` or `temp=0,25,60`; repeatable, ≤64 corners; engine-only) and prints a per-corner
verdict table, exiting `1` if any corner fails. `--timeout S` kills the engine after S seconds
(default `60`). `--exit-zero` reports without failing. Text output is an engine/deck-sha header, an
always-printed measured-value table, then findings; `--json` returns
`{deck_sha, engine, measured, findings, ok}`. The engine is discovered via `AKCLI_NGSPICE` (a path,
or `off` to disable) → macOS KiCad bundle → `find_library` → Linux sonames → Windows KiCad; when
none loads, run mode prints `ERROR: NGSPICE_MISSING` and exits `7`. New in this cycle, the deck
builder auto-appends `.option rshunt=1e12` when it detects a floating node (`SIM_FLOATING_NODE`) and
warns `SIM_UNDRIVEN_RAIL` for a power-named net with no source. See [docs/sim.md](sim.md) for the
full reference (`sim.json` format, the model-resolution ladder, `fit_diode`, and the `M`/`MEG` deck
gotchas).

### `akcli sim fit-diode --point V@I [--rs-point V@I] [--cjo F] [--n-prior N] [--name MODEL] [--apply SCH --designator REF [--write]]`
Fit a SPICE diode `.model` from datasheet forward-voltage points (`0.37@20m` = 0.37 V @ 20 mA;
`--point` repeatable). A single point plus the ideality prior (`--n-prior`, default `1.05` Schottky)
solves `IS` directly — the honest default over eyeballed curve fits; `--rs-point` adds series
resistance and `--cjo` a junction capacitance. Prints the `.model` card + `Sim.Params`, or `--json`
for `{name, model_card, sim_params, params, note}`. `--apply SCH --designator REF` plans a native
`set_component_parameters` write stamping `Sim.Device`/`Sim.Params` onto that component (a **dry
run** printing the op-list unless `--write`, which commits through the KiCad writer with a rotated
`.bak`). This closes the datasheet → model loop with [`jlc datasheet`](jlc.md).

### `akcli render <file> [-o FILE] [--grid]`
**The one query-family command that writes a file by default** (`<input>.svg` next to the
input) — agents that only want the SVG content should pass `-o -` for stdout.
Render a schematic to **SVG with the pure-stdlib renderer** — the same normalized model every check runs
on, so an Altium `.SchDoc` renders as readily as a `.kicad_sch`. **Connectivity-true, with
faithful symbol artwork on KiCad sources**: wires, buses, bus entries, junctions, labels
(scope-colored), No-ERC marks, pin tips, and component bodies drawn from the sheet's embedded
`lib_symbols` graphics (rectangles/polylines/circles/arcs, pin stubs, pin names/numbers unless the
symbol hides them) through the same transform chain the net engine uses — so artwork can never
disagree with connectivity. Components the artwork path cannot draw (Altium sources, multi-unit
parts, symbols missing from `lib_symbols`) fall back to the synthesized pin-box body with
refdes/value. Hierarchical designs render one titled block per sheet.
Deterministic output (same input bytes → same SVG bytes). `-o -` writes to stdout; default
output is `<input>.svg` next to the input. `--json` emits
`{render_version, source, output, components, wires, labels, junctions, bytes}`. The
visual-feedback channel after a `draw --apply`: render, then *look* at what you placed.
`--grid` overlays world-mil gridlines (major every 500 mil), coordinate captions and an origin
cross — the numbers on the image are exactly the numbers op-lists use (`plan/draw --render`
previews always include it).

### `akcli doc <file> [-o FILE] [--refs GLOBS]`
Generate the **pinout book** — a human-readable Markdown design document composed from the
normalized model (works on `.kicad_sch` and imported Altium `.SchDoc` alike):
- **Pin tables** per IC/connector (default refs `U*,J*,CN*,P*`; override with a comma-separated
  `--refs` glob list): pin number, name, electrical type, and the net each pin *actually landed
  on* — the as-drawn pinout a reviewer checks against the datasheet.
- **Power rails** — the `review tree` analysis as a table (rail, voltage, regulator, consumers,
  decoupling count).
- **BOM** — real in-BOM components grouped by value/footprint/symbol, refs natural-sorted.

Deterministic output (same input bytes → same Markdown bytes; no timestamps), so books diff
cleanly in review. `-o FILE` writes the file (stdout stays clean); `--json` emits the same
content structured (`{schema_version, source, refs, component_count, net_count,
components: [{ref, lib_id, value, footprint, dnp, pins: [...]}, …],
rails, bom}` — `schemas/doc.schema.json`). The design-review entry point: `/circuit-draw` output plus `akcli doc` is what a
human reads without opening an EDA tool.

### `akcli log [PATH] [--limit N] [--cmd NAME]`
Query the **workspace write journal**. Every write-path command (`plan`, `draw`, `arrange`,
`undo`, `relink-symbols`) appends one JSONL entry to `<dir>/.akcli/journal.jsonl` — timestamp,
command, target, status (`dry-run`/`applied`/`refused`), the op-list sha256, op count, the
net-diff verdict, and the backup name (a multi-sheet transaction also records `run_id` and the
dirty `files` it touched — feed `run_id` to `akcli undo <target> --run <run_id>`) — so a later
invocation (or a harness hook) can answer
"was there a plan for this op-list before the `--apply`" and "what did the last session do here"
without re-deriving state. Write commands accept `--note TEXT` to record *why* the edit was
made next to *what* was done — design intent for the next session (see
[agent-state.md](agent-state.md)). `PATH` is the workspace directory
or an edited file (which filters to that file); `--cmd` filters by command; `--json` emits
`{journal_version, journal, returned, entries}`. Journaling never fails the parent command
(failures degrade to a stderr `note:`), is size-capped with one rotation
(`journal.jsonl.1`), and `AKCLI_JOURNAL=off` disables it. Add `.akcli/` to your project's
`.gitignore`.

### `akcli capabilities [--json] [--section NAME] [--command VERB]`
The **self-describing surface manifest** — the single root document an agent reads to drive the
tool blind. `--json` emits every subcommand + flag (introspected from the live parser, so it can
never drift from reality), the frozen exit-code and error-code tables, the op-list vocabulary
(core/sugar/macros + per-executor support, plus a `constraints` block — the rotation enum, the
orthogonal-wire-only rule, `grid_mil`, and the hierarchy mode `hierarchy: "hierarchical_write_v2"`
(`sheet_reuse: true`) with a `hierarchy_note` spelling out the `sheets` map / per-op `sheet`
multi-file transaction and the per-instance reference rule for a reused sheet — and an honest
`altium_write_wired: false` flag: Altium is import-only, so the informational "altium" op column
is NOT a write target), the calculator registry, the packaged JSON Schemas with their version fields, and
the tool conventions (`stdout=data/stderr=logs`, dry-run default, version-stamp policy, and the
`--json` error-envelope contract). Without `--json`, a compact human summary. Static surface only —
environment probing (which optional tools are actually installed here) is `akcli doctor`.

**Slicing (0.20).** The full manifest is ~96 KB (mostly `commands`), while an agent usually
needs one section or one command's flags. `--section NAME` (one of `exit_codes`, `error_codes`,
`global_flags`, `commands`, `ops`, `calculators`, `schemas`, `conventions`) prints that section
alone in a stamped envelope; `--command VERB` prints one command's entry (implies
`--section commands`; an unknown VERB reports `found: false` and exits `8`, the query-miss
convention). A slice is machine-facing by definition, so it is JSON with or without `--json`;
the no-flag document keeps its exact historical shape.

```bash
akcli capabilities --section exit_codes
akcli capabilities --command draw
```

### `akcli schema <name>`
Print a packaged JSON Schema **byte-for-byte** — the fourteen schemas ship inside the wheel
(mirrored from the repo's canonical `schemas/`, byte-identity CI-gated), and this command makes
them reachable through the CLI: an orchestrator that `pip install`-ed akcli gets
`ops.schema.json` for client-side validation without digging into site-packages. Names accept
the short form (`ops`, `findings`, `netlist`, `draw-result`, `ops.capabilities`, ...) or the
full filename; an unknown name fails at parse time with the full list (USAGE envelope under
`--json`). The output IS the schema file's exact text — already JSON, already carrying its own
version field — so `--json` changes nothing.

```bash
akcli schema ops
akcli schema findings.schema.json
```

### `akcli doctor [--network] [--require CAPS] [--json]`
One-shot environment report. Probes — the same way the features themselves discover them —
**python** (>= 3.11), the **akcli** install (version + mode), packaged **schemas**,
**kicad-cli** (`KICAD_CLI` env → PATH → known install locations), **ngspice**
(`AKCLI_NGSPICE` honored; KiCad's bundled libngspice found automatically), **symbols**,
**config** discovery, and **workspace** hygiene (legacy beside-the-file backup stacks, leftover
KiCad `~*.lck` lock files, an un-gitignored `.akcli/` — advisory, never `--require`-gatable).
`--network` additionally probes the `jlc` endpoint (doctor is offline by default).

The **symbols** check walks the whole path `akcli pins Device:R` / `place_component` take —
project + global `sym-lib-table` → `${KICAD*_SYMBOL_DIR}` expansion → `.kicad_sym` load →
symbol lookup — and prints the resolved library file on success. It is the probe for the
"everything is `SYMBOL_NOT_FOUND`" failure mode: KiCad's default tables are written against
path variables KiCad never exports, so if akcli cannot infer your install the hint is
`export KICAD10_SYMBOL_DIR=/path/to/symbols` (match whichever `KICAD*_SYMBOL_DIR` your
`sym-lib-table` uses).
Every missing item prints a remediation hint; only python is a hard requirement.
`--require kicad-cli,ngspice,...` turns the report into a CI gate: exit `1` when a named
capability is missing (unknown capability names exit `2`). `--json` emits
`{checks: {name: {ok, detail, hint?}}, required, ok}`. The `akcli-setup` skill drives this
command for setup/repair flows.

### `akcli library <audit|repair|import-altium|new-symbol|lint-symbols|check-lock|relink> ...`
Project **library workspace**: the sym/fp-lib-table, the schematics, the registered libraries and
their 3D models as one auditable object — plus authoring and linting the symbols themselves.
See [docs/design-integrity.md](design-integrity.md).
- `akcli library audit [<project>] [--sch FILE ...] [--fail-on SEV] [--exit-zero]` — cross-checks schematics ↔
  `sym-lib-table`/`fp-lib-table` ↔ library contents ↔ 3D models (project dir or `.kicad_pro`;
  default `.`). Findings: `FOOTPRINT_LIB_UNREGISTERED` (a Footprint nickname the fp-lib-table does
  not register — the "cannot find footprint" trap), `FOOTPRINT_MISSING`, `SYMBOL_LIB_UNREGISTERED`/
  `SYMBOL_MISSING`, `LIB_URI_UNRESOLVED`/`LIB_PATH_MISSING`, `MODEL_MISSING`/`MODEL_NOT_PORTABLE`,
  `FOOTPRINT_LEGACY_FORMAT` (a pre-v6 footprint that parses via API but is invisible to the KiCad
  GUI). Only the **project** tables are consulted; lint-style exit (`1` on ≥ WARNING).
- `akcli library repair [<project>] [--rename-footprint-lib OLD=NEW] [--3d-path absolute|'${VAR}']
  [--apply]` — productizes the two historically hand-`sed`-ed fixes as a plan: rewrite Footprint
  nicknames `OLD:* → NEW:*` (lossless S-expression edit of registered `.kicad_sym` + schematics) and
  rewrite bare-relative 3D-model paths. Dry-run by default; `--apply` writes atomically with a `.bak`
  and re-audits.
- `akcli library import-altium <part.PcbLib> [-o DIR] [--courtyard MM] [--apply]` — converts an
  Altium `.PcbLib` into a `.pretty` library, pads **verbatim** (never recomputed). Transformations
  are declared, not silent (filesystem-safe renames; an optional pad-bbox `--courtyard`); on
  `--apply` it writes the `.kicad_mod` files plus a `provenance.json` (source SHA-256, converter
  version, options, every warning). Dry-run by default.
- `akcli library check-lock [<project>]` — is any KiCad file open in the GUI? Scans the project
  tree for documents whose `~<name>.lck` GUI lock file is present. akcli's own write ops already
  refuse a locked target (`TARGET_LOCKED`), but hand scripts and `sed` do not — this exposes the
  same probe so an external flow can gate on it (`akcli library check-lock . && ./relayout.sh`).
  Exit `6` (`TARGET_LOCKED`) when ANY file is locked, `0` when the tree is writable; `--json`
  lists each locked file with its lock path.
- `akcli library relink <target.kicad_sch> [--libs DIR ...] [--only NICKS] [--apply]` — the
  library-maintenance home of top-level `relink-symbols` (same handler and flags; the old name
  stays for compatibility).

#### `akcli library new-symbol <SPEC.json> [-o LIB.kicad_sym] [--name NAME] [--apply]`

Generate a single-unit `.kicad_sym` from a **pin table** — you supply what a datasheet gives up
reliably (number, name, electrical type, which body edge), akcli computes what hand-authoring gets
wrong (coordinates). Input shape: [`schemas/symbol-pins.schema.json`](../schemas/symbol-pins.schema.json).

```bash
akcli library new-symbol pins.json                      # dry-run: print the placement
akcli library new-symbol pins.json -o MyLib.kicad_sym --apply
akcli library new-symbol pins.json --name ALT_NAME --json
```

- **`side` and `type` are required per pin and never guessed.** A guessed side is a wrong pin
  position; a defaulted `unspecified` type is a pin KiCad's ERC pin-conflict matrix exempts from
  every rule.
- **Layout**: pins on the 100-mil grid, standard 100-mil pin length. Within one side, pins are
  ordered by their `group`'s first appearance in the array, then by array order — so the output is
  stable and related pins stay together. The body rectangle is sized from the per-side pin counts
  and the pin-name text widths; `body.width_mil` may only **widen** it (shrinking below the computed
  minimum would push top/bottom pins onto the left/right pin columns).
- **Deterministic**: identical input yields identical bytes.
- **Stacked pins**: `stack_with` is the ONLY way to put two pins on one coordinate, and the target
  must be on the same side with the same name **and** the same electrical type (KiCad's stacked-pin
  convention). Chains are refused so a coordinate has exactly one owner.
- **Write gate (deliberate exception to "geometry findings are advisory")**: if the generated symbol
  trips `SYMLIB_PIN_COINCIDENT`, the command refuses and writes nothing — exit `6`
  (`VERIFY_FAILED`), with or without `--apply`. Two pins on one point is a *connectivity* defect:
  KiCad merges them into one node in every placement of the part, and ERC cannot report it because
  the symbol is what told ERC they are the same node.
- **Spec surface**: required top-level keys `protocol_version`, `name`, `pins`; optional
  `reference_prefix` (default `U`) and `body.width_mil`. Per pin: `number`, `name`, `type`,
  `side` (required), plus optional `group`, `hidden` (an invisible pin) and `stack_with`.
- **Not supported yet**: multi-unit symbols. A spec carrying `units`/`unit_count`/`part_count`, or a
  pin carrying `unit`/`unit_number`/`owner_part_id`, is refused with an explicit "not supported
  yet" rather than a generic
  unknown-key error. A wrong `protocol_version` is `PROTOCOL_MISMATCH` (exit `6`). Other
  refusals (all `BAD_CONFIG`, exit `2`): duplicate pin numbers, an unknown
  `type` (the message lists the whole vocabulary), a `stack_with` name/type/side mismatch, and an
  empty pin table.
- Dry-run by default; `--apply` writes atomically, leaves a `.bak` when replacing, and refuses
  `TARGET_LOCKED` if KiCad holds the target open. Output path defaults to `<symbol name>.kicad_sym`
  (under `[paths].parts_dir` when configured).

#### `akcli library lint-symbols <LIB.kicad_sym> [--json] [--fail-on SEV] [--exit-zero]`

Run the same geometry checks over a library akcli did **not** write — an imported, converted or
hand-authored `.kicad_sym`. This is where the incident behind `new-symbol` would have been caught.

```bash
akcli library lint-symbols MyLib.kicad_sym
akcli library lint-symbols MyLib.kicad_sym --json --fail-on error
```

Findings: `SYMLIB_PIN_COINCIDENT` (error), `SYMLIB_PIN_TYPE_UNSPECIFIED` (note),
`SYMLIB_PIN_OFF_GRID` (warning) — see the finding-code table below. Multi-unit symbols are evaluated
per unit, so four gates drawn on one body never produce a false positive. Lint-style exit (`1` on
≥ `--fail-on`, default `warning`); here the findings are advisory — only `new-symbol` gates a write.

### `akcli fab <check|explain> ...`
Manufacturing policy against a **versioned fab profile** (TOML carrying mandatory `[source]` urls —
policy is source-controlled, never a builtin constant).
- `akcli fab check <board.kicad_pcb> --profile FILE [--order FILE] [--fail-on SEV] [--exit-zero]` — checks the
  deep-read board: via geometry vs the free-process envelope (`FAB_VIA_PAID_PROCESS`,
  `FAB_VIA_TENTED_TOO_BIG`, `FAB_VIA_MIN_MARGIN`), via-in-pad (`FAB_VIA_IN_PAD`, with registered
  `thermal_via` exceptions → `FAB_VIA_IN_PAD_EXCEPTION`; an expired one → `FAB_EXCEPTION_EXPIRED`),
  blind/buried bans (`FAB_VIA_TYPE_FORBIDDEN`), stackup drift (`FAB_STACKUP_MISMATCH`), and cost
  thresholds (`FAB_COST_*`: board size/area, drill density, fine multilayer traces). `--order`
  validates the declared **order manifest** (delivery format, finish, via covering, … — pricing
  inputs never guessed from the PCB): `ORDER_INCOMPLETE`, `ORDER_REVIEW_REQUIRED` (ENIG/panel/
  multi-design), `ORDER_PROFILE_CONFLICT`. Lint-style exit.
- `akcli fab explain <CODE> [--profile FILE]` — prints the rule behind a finding code, the fix
  direction, and (with `--profile`) the profile's evidence sources.

### `akcli release preflight --sch FILE [--pcb FILE] [--intent FILE] [--contract FILE] [--fab-profile FILE] [--order FILE] [--sim FILE] [--gerbers DIR] [--review-policy FILE] [-o FILE] [--allow-dirty]`
Run **every applicable gate** and emit a traceable release manifest. Gates:
`check` (ERC/power/BOM/nets), `intent`, `contract`, `library-audit`, `sch-pcb` (needs `--pcb`),
`fab` (needs `--pcb` + `--fab-profile`), `order`, `sim` (needs `--sim` sim.json — quantitative
SPICE assertions; a **requested** sim gate that cannot run FAILS rather than skips, so a PASS
manifest never ships because the machine lacked libngspice), `review` (needs `--review-policy`),
`gerber` (needs `--gerbers`), and `git` (clean worktree). A gate with no input
is **skipped with a reason**, never silently green. `--out` writes the manifest JSON: input
SHA-256s, the akcli version, the git revision and dirtiness, and each gate's findings. A dirty
worktree fails the `git` gate (`RELEASE_DIRTY_WORKTREE`, ERROR) unless `--allow-dirty` (which
records the fact). Exit `0` when every gate is `pass` or `skipped` (a skipped gate is a recorded
absence of evidence, not a blocker); any single `fail` exits `1`.

### `akcli gate [PATH] [--pcb FILE] [--intent FILE] [--lock FILE] [--live] [--fail-on SEV] [--exit-zero]`
The **iteration-weight** counterpart to `release preflight`: four gates re-run by hand after every
GUI edit round during a design campaign, with none of the release manifest/input-hashing/git-state
machinery. Not the same thing as the repo's own CI-green *gate* (`.github/scripts/
ci_green_gate.sh`, see `docs/releasing.md`) — that one checks GitHub Actions status on a tagged
release commit; this one is a local, offline, per-edit check bundle.

Gates:
- `check` — the **full default** `akcli check PATH` set (erc/power/bom/nets, plus
  layout/integrity for a `.kicad_sch`, plus pairs unless `[check].pairs = false`) — a *wider* set
  than `release preflight`'s fixed erc/power/bom/nets four-tuple.
- `intent` — `--intent FILE`, falling back to `[paths].intent` (the same config key
  `check --intent` falls back to). Skipped with a reason when absent.
- `sch-pcb` — `--pcb FILE`, falling back to `[paths].pcb`. Skipped with a reason when absent.
- `bom-lock` — BOM lockfile drift (`--lock FILE`, falling back to `[paths].bom_lock`), the same
  `diff_lock()` call `jlc bom --against-lock` makes; each drift is a `BOM_LOCK_DRIFT`
  finding (WARNING). **Offline (HTTP-cache-only) by default** — an
  every-edit loop must never touch the network on its own; `--live` opts one run into a fresh
  catalog fetch. A cache miss degrades a line to `unverified` rather than failing the gate outright;
  an unverified count is always shown, so a cold cache never reads as a fully-verified PASS.
  Skipped with a reason when no lockfile is given.

`PATH` is optional: it falls back to `[paths].schematic` — the first command that actually reads
that documented config key. No path anywhere is a `USAGE` error naming the missing key. Exit is
worst-of across the four gates (`0` when every gate passed or was skipped, `1` otherwise); the
shared `--fail-on`/`--exit-zero` flags re-tune that threshold without changing what each gate
reports. `--json` emits `{schema_version, result, gates: [...]}` — the same per-gate shape as
`release preflight`, minus the release manifest's `inputs`/`git`/tool-hashing fields.

```
akcli gate board.kicad_sch --pcb board.kicad_pcb --intent intent.json --lock bom.lock.json
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success / no findings. |
| `1` | Check findings present (lint-style; tune with `check --fail-on`, `never` always exits `0`). |
| `2` | Usage / argument / config error (incl. `BAD_CONFIG`, `PATH_OUTSIDE_ROOT`). |
| `3` | Parse error (corrupt OLE2 or S-expression, incl. a bad numeric field: `KICAD_SEXPR_BAD_NUMBER`, or bytes that are not UTF-8 at all: `KICAD_BAD_ENCODING`) — also the last-resort code for an unexpected internal exception (`INTERNAL`). |
| `4` | The path is not a usable file: missing (`FILE_NOT_FOUND`), or present but unreadable — a directory passed where a file was expected, no read permission, an IO error (`FILE_UNREADABLE`). |
| `5` | Unsupported format (incl. `ALTIUM_UNSUPPORTED` features). |
| `6` | Op-list or verify failure (incl. `--strict-nets` refusal, `relink-symbols` / `library repair` gate refusal, `PROTOCOL_MISMATCH`, GUI-lock `TARGET_LOCKED`, `sim` deck-build refusals). |
| `7` | Required external tool missing **or network failure** (`KICAD_CLI_MISSING`/`KICAD_CLI_TIMEOUT`, `jlc` `ERROR: NETWORK`, `BINFETCH_*`, `sim`'s `NGSPICE_MISSING`/`NGSPICE_FAILED`, reserved `BRIDGE_TIMEOUT`). |
| `8` | Query miss: the file parsed fine but the named entity does not exist in it (`net <file> NAME`, `component <file> REF`; `--json` emits `{"found": false, ...}`). |

## Structured errors

Without `--debug`, failures print a single structured line, e.g.:

```
ERROR: ALTIUM_FAT_CYCLE: FAT chain revisits sector 42 (cycle); aborting
```

- With `--json`, a failing command that would otherwise leave stdout empty instead emits a JSON
  error envelope on stdout: `{"schema_version": "1", "akcli_version": "<version>", "error":
  {"code", "message", "exit", "remediation"}}` — `akcli_version` names the build that failed,
  and `remediation` is an actionable next step from the frozen `errors.REMEDIATION`
  table (covering every error code); the plain `ERROR:` line still goes to stderr for humans. The
  envelope is skipped when the handler already wrote data on stdout, so a failure after partial
  output never yields two JSON documents.
- **argparse failures use the same envelope.** A wrong flag, a bad `choices` value, or an unknown
  command emits `{"error": {"code": "USAGE", "message", "exit": 2, "remediation"}}` under `--json`
  (the argparse text, including the full `choose from` list, rides in `message`); plain-text
  behavior is unchanged. `USAGE` and `FILE_NOT_FOUND` are envelope-level pseudo-codes named after
  the exit category, not members of the `ERROR_CODES` registry below.
- **Invoking `akcli --json` with no subcommand** emits the same `USAGE` envelope (exit `2`); the
  help text still goes to stderr. There is no non-zero exit that leaves stdout empty under `--json`.
- `INTERNAL` means **unknown**, and only that. Failures with an identifiable cause are classified
  before the last-resort handler sees them: the `OSError` family (directory-as-file, unreadable
  file, IO error) → `FILE_UNREADABLE` exit `4`, undecodable bytes → exit `3`. Only a genuinely
  unexpected exception still yields `INTERNAL` (exit `3`) plus a traceback on stderr; `--debug`
  re-raises instead.
- **JSON output is RFC 8259.** Non-finite floats (`NaN`, `Infinity`) are never emitted — producers
  refuse them at the input (e.g. `calc rc r=nan` is a `USAGE` error, as is a degenerate `r=0 c=0`
  that would divide by zero), so an agent's parser never fails on the *document*.
- **`-C/--config` is strict.** An explicit `-C` naming a file that does not exist, is a directory,
  or fails to parse is `BAD_CONFIG` (exit `2`) — it is never silently ignored. Walk-up discovery
  finding no `akcli.toml` stays silent, as before.

Error codes (frozen registry in `src/akcli/errors.py`), grouped by the exit code they
surface as:

| Exit | Error codes |
|---|---|
| `3` (parse) | `ALTIUM_BAD_MAGIC`, `ALTIUM_FAT_CYCLE`, `ALTIUM_OOB_SECTOR`, `ALTIUM_BAD_SECTOR_SHIFT`, `ALTIUM_ALLOC_GUARD`, `ALTIUM_MALFORMED`, `KICAD_SEXPR_DEPTH`, `KICAD_SEXPR_UNTERMINATED`, `KICAD_SEXPR_TOOBIG`, `KICAD_SEXPR_BAD_NUMBER`, `KICAD_BAD_ENCODING`, `INTERNAL` |
| `4` (unusable path) | `FILE_UNREADABLE` |
| `5` (unsupported) | `ALTIUM_UNSUPPORTED` |
| `6` (op-list/verify) | `SYMBOL_NOT_FOUND`, `BAD_ANGLE`, `NON_ORTHOGONAL_WIRE`, `OFF_GRID`, `OVERLAP`, `VERIFY_FAILED`, `OP_UNSUPPORTED`, `HIERARCHICAL_UNSUPPORTED`, `PROJECT_ROOT_AMBIGUOUS`, `SHEET_UNKNOWN`, `INSTANCE_REFS_REQUIRED`, `TXN_ROLLBACK_FAILED`, `PROTOCOL_MISMATCH`, `TARGET_LOCKED`, `GROUP_UNKNOWN`, `GROUP_NO_ORIGIN`, `POWER_PORT_STACKED`, `SHEET_FILE_MISSING`, `DESIGN_UNSUPPORTED`, `SIM_NO_GROUND`, `SIM_NODE_COLLISION`, `SIM_NODE_COUNT_MISMATCH`, `SIM_VALUE_INVALID`, `BRIDGE_BUSY` (reserved, unraised) |
| `2` (usage/config) | `PATH_OUTSIDE_ROOT`, `BAD_CONFIG`, `SIM_OP_ASSERT_UNSUPPORTED`, `PROJECT_ROOT_MISMATCH` |
| `7` (tool/network) | `KICAD_CLI_TIMEOUT`, `KICAD_CLI_MISSING`, `NGSPICE_MISSING`, `NGSPICE_FAILED`, `NETWORK` (`jlc`), `BINFETCH_DOWNLOAD`, `BINFETCH_CHECKSUM`, `BRIDGE_TIMEOUT` (reserved, unraised) |

Two related but distinct vocabularies:

- **Per-op results** (`plan`/`draw` output) carry an `error_code` from the same registry; a
  crashing op handler is contained and reported as error code `INTERNAL` instead of a traceback.
- **Finding codes** (`check` output; these drive exit `1`, not the table above) now also include
  `NET_PIN_MIDSPAN_TOUCH`, `NET_LABEL_UNATTACHED`, `NET_WIRE_CORNER_ON_PIN`,
  `LAYOUT_POWER_ON_PIN`, `LAYOUT_WIRE_THROUGH_SYMBOL`, `LAYOUT_LABEL_OVER_WIRE`,
  `ERC_UNPLACED_UNIT`, `INTENT_PIN_UNKNOWN`, `INTENT_NET_NOT_FOUND`, `INTENT_MISSING_MEMBER`,
  `INTENT_EXTRA_MEMBER`, `INTENT_NETS_SHORTED`, `LIB_EMBED_STALE`, `LIB_EMBED_OLD_FORMAT`,
  `POWER_RAIL_NO_SOURCE`, `REVIEW_FACTS_UNMATCHED`, the write-gate document findings that
  `check --integrity` also runs read-only (`DANGLING_ENDPOINT`, `DANGLING_BUS_ENTRY`,
  `DUPLICATE_UUID`, `DUPLICATE_DESIGNATOR`, `UNRESOLVED_LIB_ID`, `INVALID_INSTANCES_PATH`,
  `POWER_PORT_STACKED`, `SHEET_FILE_MISSING`) plus `NO_CONNECT_CONFLICT` — reported by the
  same pass at WARNING, so it surfaces in `--integrity` but does not by itself refuse a
  write — plus the design-integrity families
  `CONTRACT_*` (`check --contract`), `SCHPCB_*` (`verify sch board.kicad_pcb`), the
  `library audit` codes (`FOOTPRINT_LIB_UNREGISTERED`, `FOOTPRINT_MISSING`, `MODEL_MISSING`,
  `FOOTPRINT_LEGACY_FORMAT`, …), the `library lint-symbols` codes (`SYMLIB_PIN_COINCIDENT`,
  `SYMLIB_PIN_TYPE_UNSPECIFIED`, `SYMLIB_PIN_OFF_GRID`), and `FAB_*`/`ORDER_*` (`fab check`).

### Symbol-library finding codes (`library lint-symbols`)

| code | severity | what it means |
|---|---|---|
| `SYMLIB_PIN_COINCIDENT` | error | Two pins of one symbol **unit** share a connection point, with differing name or electrical type. KiCad treats a symbol's pin connection point as a node, so every placement of the part joins those pins into one net — and ERC reports nothing, because the *symbol* is what told it they are the same node. The one exemption is KiCad's stacked-pin convention: same name **and** same type. Multi-unit parts are evaluated per unit (`_0_*` pins count as belonging to every unit), so four gates drawn on one body never trip it. |
| `SYMLIB_PIN_TYPE_UNSPECIFIED` | note | Pins carrying the `unspecified` (or `free`) electrical type. KiCad's native ERC decides pin conflicts from a type-vs-type matrix in which `unspecified` conflicts with nothing: those pins are exempt from every driver/input rule, and the volume of exempt pins an untyped symbol contributes is what buries the genuine conflicts elsewhere in the report. |
| `SYMLIB_PIN_OFF_GRID` | warning | A pin connects off the 100-mil grid, so a wire drawn on the editor grid can never land on it: the pin looks wired and behaves as floating. |

`SYMLIB_PIN_COINCIDENT` is the one geometry-shaped finding akcli treats as
**connectivity**, not advice — see the `library new-symbol` write gate below.

## Examples

```bash
akcli read main.SchDoc --json | jq '.components | length'
akcli net  board.kicad_sch --json > netlist.json
akcli check main.SchDoc -C akcli.toml          # exit 1 if findings
akcli diff  v1.SchDoc v2.SchDoc
akcli pinmap main.SchDoc -C akcli.toml --expected pins.csv
akcli export main.SchDoc --format protel -o board.net
akcli nets board.kicad_sch --intent-snapshot intent.json  # snapshot the netlist you meant
akcli plan board.kicad_sch --ops ops.json --symbols Device.kicad_sym
akcli draw board.kicad_sch --ops ops.json --symbols Device.kicad_sym --apply --strict-nets
akcli check board.kicad_sch --intent intent.json          # assert the intent held
akcli relink-symbols board.kicad_sch --apply              # refresh embedded lib_symbols
akcli sim board.kicad_sch --deck-only                     # emit the SPICE deck (no ngspice)
akcli sim board.kicad_sch --sim board.sim.json            # run + assert, exit 1 on failure
akcli sim board.kicad_sch --sim board.sim.json --sweep temp=0,25,60   # corner matrix
akcli sim fit-diode --point 0.37@20m --n-prior 1.05 --name DBAT       # datasheet -> .model
```
