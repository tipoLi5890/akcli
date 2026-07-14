# `akcli` CLI reference

`akcli` is the command-line entry point of `akcli`. It reads
Altium binary `.SchDoc`/`.SchLib`/`.PcbDoc` and KiCad `.kicad_sch`/`.kicad_sym`/`.kicad_pcb`, runs
checks (including design-intent assertions), diffs revisions, draws KiCad schematics with a
before/after net-connectivity diff, and provides 60 standards-cited engineering calculators
(`akcli calc`) — with no Altium or KiCad install required.

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
| `--quiet` | Suppress non-error logs on stderr. |
| `--json` | Emit machine-readable JSON on stdout (carries `schema_version`). |
| `--no-color` | Disable ANSI color in text output. |
| `--debug` | Show full Python tracebacks instead of structured `ERROR: CODE` messages. |

## Subcommands

### `akcli read <file> [--md]`
Parse an Altium or KiCad schematic/PCB/library into the normalized model and print it.
- Input: `.SchDoc`, `.SchLib`, `.PcbDoc`, `.PrjPcb`, `.kicad_sch`, `.kicad_sym`, `.kicad_pcb`.
- A KiCad root sheet **recurses into its `(sheet ...)` children** (paths relative to the parent
  file, cycle- and depth-guarded); every sheet instance contributes its components under the
  designator from the matching `(instances (path ...))` entry.
- An Altium root **recurses into sheet symbols** the same way (RECORD 15/16/32/33; ports pair
  with their own sheet entry — Altium *Automatic* scope). A `.PrjPcb` reads the project's top
  sheet and honors `PowerPortNamesTakePriority`.
- A `.PcbDoc` decodes both the ASCII sections (nets/components/classes/rules) and the **binary
  copper sections** `Tracks6`/`Vias6`/`Arcs6`/`Pads6` into `tracks`/`vias`/`arcs`/`pads`
  (mils, Altium's native +Y-up frame); `Fills6`/`Regions6`/`Texts6`/`Polygons6` are skipped.
- `--json` prints the full `Schematic`/`Pcb`/`Library` export with `schema_version`; `--md` prints
  a human Markdown summary.

### `akcli net <file> [NAME]`
Extract the netlist (net → pin membership) using the shared `netbuild` engine.
- With `NAME`, print just that net; a miss prints a notice to **stderr** and still exits `0`.
- Output: nets with members, aliases, and source names; `--json` validates against
  `schemas/netlist.schema.json`.
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

### `akcli nets <file> [--intent-snapshot OUT.json] [--include-unnamed]`
Print **every net → sorted members**, one line per net (`MID: C1.1, R1.2, R2.1`); unnamed nets
render as `<unnamed net_...>`. `--json` emits `{source, nets: [{name, stable_id, members}]}`.
- `--intent-snapshot OUT.json` additionally writes the netlist as a **design-intent JSON**
  document (`'-'` = stdout) that `akcli check --intent` consumes — the snapshot → edit → assert
  workflow. Named nets only by default; `--include-unnamed` also captures unnamed nets keyed by
  their `stable_id`.
- A snapshot round-trips: `akcli check <sch> --intent <snapshot>` on the unchanged schematic
  reports zero findings.

### `akcli component <file> [REF]`
Without `REF`: list components (designator, library reference, value, footprint, pin count, sheet).
With `REF`: that component's pin → net table. A missing `REF` prints a notice to **stderr** and
exits `0`.

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

### `akcli check <file>`
Run the design checks (ERC-lite + power + BOM hygiene + nets + layout) and print findings.
- `-C/--config` supplies rails, MCU designator, the schematic grid, `[[erc_waiver]]`
  entries, and the checker-agnostic `[[waiver]]` table (below).
- `--erc` / `--power` / `--bom` / `--nets` / `--layout` / `--intent` / `--libsync` select check
  families (default: `erc`+`power`+`bom`+`nets`+`layout`; `layout` only runs on `.kicad_sch`;
  `intent` and `libsync` are **opt-in only** — they never run by default).
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
  stacked on one anchor, plus `LAYOUT_POWER_ON_PIN` (a power symbol — notably
  `PWR_FLAG` — anchored directly on another symbol's pin tip; move it
  mid-wire, see the `place_pwr_flag` macro), `LAYOUT_WIRE_THROUGH_SYMBOL`
  (a wire routed through a symbol body), and `LAYOUT_LABEL_OVER_WIRE` (NOTE:
  label text crossing an unrelated wire). ERC can never see these — a
  schematic can be electrically perfect and visually unreadable.
- `--erc` additionally reports `ERC_UNPLACED_UNIT` (WARNING, `.kicad_sch`
  only): a multi-unit part with units never placed (e.g. gate B of a dual
  comparator) — terminate them with the `terminate_unused_unit` macro.
  Waiver rule token: `unplaced_unit`.
- `--intent FILE` asserts a **design-intent JSON** file (written by
  `akcli nets --intent-snapshot`, or by hand) against the built netlist:

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
  `--fail-on never`** (still works).
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

### `akcli diff <file_a> <file_b>`
Diff two schematic revisions. Nets are matched by **membership** (not display name); components by
UniqueID, then `(value, footprint, pin-count)` signature, then refdes.

### `akcli arrange <target.kicad_sch> [--apply] [--grid MIL] [--margin MIL] [--symbols PATH ...]`
Resolve symbol overlaps by nudging **free** components — parts with no wire
endpoint or label anchor on any pin (moving anchored parts would strand their
connectivity, which is exactly what `check --nets` flags). Greedy first-fit in
reading order; anchored overlaps are reported for manual fixing (exit `1`).
Dry-run by default; `--apply` writes through the draw pipeline (`.bak` +
connectivity re-verify), so `akcli undo` reverts an arrange. `--symbols`
supplies extra symbol sources for the write pipeline (same semantics as
`draw --symbols`).

### `akcli verify <file_a> <file_b> [--strict]`
**Net-equivalence proof** on top of the diff engine — the one-command answer to
"did the conversion keep the circuit?". PASS (exit `0`) iff the component set
matches and every net's **pin membership** is identical; nets that merely
changed display name are listed but do not fail (conversions rename unnamed
nets). Component value/footprint drift is reported as a note — `--strict`
turns it into a failure. `--json` carries the verdict plus the full diff
report. Works across formats: `akcli verify board.SchDoc board.kicad_sch`.

### `akcli new [<path>] [--paper SIZE] [--title T] [--force]`
Bootstrap a minimal blank `.kicad_sch` (root `(uuid ...)`, `(paper ...)`, and an
optional title block) that `akcli draw`/`plan` can immediately append to — the
first-class replacement for hand-seeding a skeleton file. `--paper` sets the
sheet size (`A4`|`A3`|…, default `A4`); `--title` fills the title-block title;
`--force` overwrites an existing file (otherwise a present target is refused).
`--json` = `{created, target, paper, title, status}`. Keep the created file for
the whole session — its root uuid is the namespace for every deterministic op
UUID, so regenerating it breaks idempotent re-runs.

### `akcli undo <target.kicad_sch> [--apply] [--steps N] [--list]`
Restore the target from the **rotated draw backups** that `akcli draw --apply`
writes beside it (`<name>.bak` newest, then `.bak2`, `.bak3` — up to 3).
Dry-run by default (prints the part/net delta that restoring would cause);
`--apply` swaps, so **undo twice is a redo**.
- `--list` prints the backup stack (level, path, size, mtime — newest first,
  contiguous from `.bak`) and exits; `--json` = `{target, depth, backups:[…]}`.
- `--steps N` walks back N snapshots at once (default 1), leaving the stack so a
  single subsequent `undo` redoes the last step; N clamps to the available
  stack. `--steps 0` is a usage error (exit `2`); `--steps` with no backup exits
  `4`.
- Exit `4` when no backup exists.

### `akcli pinmap <file>`
Emit the MCU pin → net table (MCU chosen by `mcu_designator` in config, or `--mcu REF`).
- `--expected PATH` cross-checks against an external expected pin→signal table (CSV or JSON). The
  schematic is authoritative; the expected table is advisory.

### `akcli expected <file.dts|.overlay|.md> [-o FILE]`
Extract an **expected pin→signal table** from a Zephyr devicetree source/overlay
(`gpios = <&gpioN pin ...>` phandles and Nordic `NRF_PSEL(...)` pinctrl) or from a
markdown pinout table (`--key-header`/`--value-header` pick columns explicitly).
Emits the JSON object `pinmap --expected` consumes; `-o` writes it to a file.
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
  schema-valid `place_component` op-list with the computed E-series values
  filled in (`-` = stdout) — edit coordinates, then `akcli plan`.
- `batch <file|->` runs `{"jobs": [{"calc": ..., "params": {...}}, ...]}` and
  emits an array of result envelopes; exits `1` if any job failed (each
  failure carries an `error` field), `0` when all succeed.
- `list` groups all calculators; `info <name>` shows parameters, defaults and
  the citation. Bad name/params exit `2`.
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
membership-derived `stable_id`, so re-exports diff cleanly. `--json` is **refused** (exit `2`) —
use `akcli net --json` for structured output.

### `akcli plan <target.kicad_sch> --ops FILE [--symbols PATH ...] [--no-net-diff]`
Validate an op-list against `protocol_version` and `schemas/ops.schema.json`, resolve it against the
target `.kicad_sch` (symbols from repeatable `--symbols` sources and the target's inline cache), and
print what *would* change. Never writes. Includes the **Net changes** block (below);
`--no-net-diff` skips it.

### `akcli draw <target.kicad_sch> --ops FILE [--symbols PATH ...] [--apply] [--no-net-diff] [--strict-nets]`
Execute an op-list against a KiCad `.kicad_sch`. The vocabulary is 18 ops + 9 macros (see
`schemas/ops.schema.json`), including `delete_component` (with `cascade`) / `delete_object` /
`move_component` / `rename_net`, hierarchical `add_sheet` (below), and multi-unit placement via
`place_component`'s optional `"unit"` field.
- **Default is a dry run** (no file written): prints per-op results and the connectivity
  verification. (`--dry-run` is accepted but inert — omitting `--apply` already is the dry run.)
- `--apply` performs the write via the atomic snapshot → temp → verify-on-temp → `os.replace`
  pipeline, writing a `<target>.bak` copy alongside the file. The write is rejected (exit `6`)
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
- `--strict-nets` (with `--apply`): **refuse to write** (exit `6`, evidence lines on stderr)
  when the diff shows a split or merge touching a *named* net on either side — splits/merges of
  unnamed nets are ordinary wiring edits and pass. It forces the diff even under `--no-net-diff`,
  and it fails **closed**: when the diff cannot be computed, the write is refused
  (`REFUSED: --strict-nets: net diff unavailable (...)`) rather than waved through.
- A final status line states the outcome unambiguously:
  `status: dry-run — nothing written (re-run with --apply)`,
  `status: APPLIED — wrote board.kicad_sch (backup board.kicad_sch.bak; akcli undo reverts)`, or
  `REFUSED: --strict-nets: net split/merge touches a named net; nothing written`.
- `--json` payloads carry `"status"` and
  `"net_diff": {"equivalent", "risk", "lines"}` (or `null` when unavailable).
- **`add_sheet` (hierarchical authoring, KiCad only — `altium: false`):** emits a
  `(sheet …)` node with `Sheetname`/`Sheetfile`, deterministic uuids, and
  edge-computed sheet pins. Op shape:
  `{op:"add_sheet", name, file, at:[x,y], size:[w,h], pins?:[{name, type, side, offset_mil}]}`
  (mils; `at` = top-left corner). `type` ∈ input|output|bidirectional|tri_state|passive;
  `side` ∈ left|right|top|bottom. **Wires attach to a sheet pin by coordinate**
  (`at` + `offset_mil` along the side, grid-snapped) — there is no
  `Sheet.Pin` wire endpoint. A cross-sheet net is a parent sheet-pin paired with
  the child's same-name hierarchical label. The referenced child `.kicad_sch` is
  **not** created by `add_sheet` — author it separately (e.g. `akcli new`).

### `akcli relink-symbols <target.kicad_sch> [--libs DIR ...] [--only NICKS] [--apply]`
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
  the write with `VERIFY_FAILED` (exit `6`) and leaves the file untouched. On pass it writes
  `<name>.bak` and replaces atomically.
- `--only power,Device` restricts to the listed library nicknames (or full lib_ids).
- Exit `6` when any entry is `missing-lib` (scope with `--only` to silence intentionally
  unavailable nicks); `--json` lists the actions (minus the raw symbol text).

### `akcli ops <list|template OP> [--required-only]`
Op-list authoring kit: `list` prints the 18-op vocabulary with required fields
and per-executor support, plus the **9 macro ops** (`connect_and_label`,
`place_pwr_flag`, `terminate_unused_unit`, `place_divider`, `place_decoupling`,
`place_pullup`, `place_led_indicator`, `place_rc_filter`, `place_crystal`)
that expand to core ops before validation; `template` emits a fill-in JSON
op-list skeleton for any op or macro (guide:
[docs/op-list-authoring.md](op-list-authoring.md)). A mistyped op or
calculator name gets a did-you-mean suggestion.

### `akcli view <calc|live|SCH> [PATH] [--port N] [--no-browser] [--state-dir DIR] [--max-steps N]`
ONE local dashboard server for both pages (127.0.0.1 only, zero dependencies,
HTML bundled in the package). Default port `8765`, auto-incrementing when
busy. `/` is the **hub** — the entry page the browser opens on launch, with
one card per dashboard (the live card shows the watched file, step count and
latest ERC state in real time; `C`/`L` jump straight in).
`view <sch.kicad_sch>` is shorthand for `view live <sch>`; `view calc`
serves `/calc` alone.
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
JLCPCB/LCSC part search, BOM purchasability check (`jlc bom <sch>` — stock/tier-price/est-cost
per BOM line via LCSC/MPN parameters; `--qty N` evaluates at build quantity; `--suggest`/`--fix`
find and write catalog replacements for missing/dead part ids — `--fix` writes only
high-confidence matches, `--fix-all` also writes low-confidence ones; `--csv OUT.csv` exports a
JLCPCB upload BOM CSV, `'-'` = stdout), **datasheet resolution/download**
(`jlc datasheet <C-number|MPN|sch> [--fetch] [--out DIR]` — resolves szlcsc PDF links via the
EasyEDA record, `%PDF`-magic-verified downloads, whole-BOM batch mode; files cache under
`~/.cache/akcli/datasheets/`), and library conversion — the only **networked**
subcommand family. Network failures exit `7` (`ERROR: NETWORK: ...`); transient errors are
retried with backoff, and a stale cached response is served with a warning when retries are
exhausted. See [docs/jlc.md](jlc.md) for the full reference.

### `akcli sim <sch> [--sim FILE] [--deck-only] [--out PATH] [--gnd NET] [--wave OUT.csv] [--sweep NAME=v1,v2,...] [--timeout S] [--exit-zero]`
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

### `akcli doctor [--network] [--require CAPS] [--json]`
One-shot environment report. Probes — the same way the features themselves discover them —
**python** (>= 3.11), the **akcli** install (version + mode), packaged **schemas**,
**kicad-cli** (`KICAD_CLI` env → PATH → known install locations), **ngspice**
(`AKCLI_NGSPICE` honored; KiCad's bundled libngspice found automatically), and **config**
discovery. `--network` additionally probes the `jlc` endpoint (doctor is offline by default).
Every missing item prints a remediation hint; only python is a hard requirement.
`--require kicad-cli,ngspice,...` turns the report into a CI gate: exit `1` when a named
capability is missing (unknown capability names exit `2`). `--json` emits
`{checks: {name: {ok, detail, hint?}}, required, ok}`. The `akcli-setup` skill drives this
command for setup/repair flows.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success / no findings. |
| `1` | Check findings present (lint-style; tune with `check --fail-on`, `never` always exits `0`). |
| `2` | Usage / argument / config error (incl. `BAD_CONFIG`, `PATH_OUTSIDE_ROOT`). |
| `3` | Parse error (corrupt OLE2 or S-expression). |
| `4` | File not found. |
| `5` | Unsupported format (incl. `ALTIUM_UNSUPPORTED` features). |
| `6` | Op-list or verify failure (incl. `--strict-nets` refusal, `relink-symbols` gate refusal, `PROTOCOL_MISMATCH`). |
| `7` | Required external tool missing **or network failure** (`KICAD_CLI_MISSING`/`KICAD_CLI_TIMEOUT`, `jlc` `ERROR: NETWORK`, `BINFETCH_*`, `sim`'s `NGSPICE_MISSING`/`NGSPICE_FAILED`). |

## Structured errors

Without `--debug`, failures print a single structured line, e.g.:

```
ERROR: ALTIUM_FAT_CYCLE: FAT chain revisits sector 42 (cycle); aborting
```

Error codes (frozen registry in `src/akcli/errors.py`), grouped by the exit code they
surface as:

| Exit | Error codes |
|---|---|
| `3` (parse) | `ALTIUM_BAD_MAGIC`, `ALTIUM_FAT_CYCLE`, `ALTIUM_OOB_SECTOR`, `ALTIUM_BAD_SECTOR_SHIFT`, `ALTIUM_ALLOC_GUARD`, `ALTIUM_MALFORMED`, `KICAD_SEXPR_DEPTH`, `KICAD_SEXPR_UNTERMINATED`, `KICAD_SEXPR_TOOBIG` |
| `5` (unsupported) | `ALTIUM_UNSUPPORTED` |
| `6` (op-list/verify) | `SYMBOL_NOT_FOUND`, `BAD_ANGLE`, `NON_ORTHOGONAL_WIRE`, `OFF_GRID`, `OVERLAP`, `VERIFY_FAILED`, `OP_UNSUPPORTED`, `HIERARCHICAL_UNSUPPORTED`, `PROTOCOL_MISMATCH` |
| `2` (usage/config) | `PATH_OUTSIDE_ROOT`, `BAD_CONFIG` |
| `7` (tool/network) | `KICAD_CLI_TIMEOUT`, `KICAD_CLI_MISSING`, `BINFETCH_DOWNLOAD`, `BINFETCH_CHECKSUM` (and `jlc`'s `ERROR: NETWORK: ...` line) |

Two related but distinct vocabularies:

- **Per-op results** (`plan`/`draw` output) carry an `error_code` from the same registry; a
  crashing op handler is contained and reported as error code `INTERNAL` instead of a traceback.
- **Finding codes** (`check` output; these drive exit `1`, not the table above) now also include
  `NET_PIN_MIDSPAN_TOUCH`, `NET_LABEL_UNATTACHED`, `NET_WIRE_CORNER_ON_PIN`,
  `LAYOUT_POWER_ON_PIN`, `LAYOUT_WIRE_THROUGH_SYMBOL`, `LAYOUT_LABEL_OVER_WIRE`,
  `ERC_UNPLACED_UNIT`, `INTENT_PIN_UNKNOWN`, `INTENT_NET_NOT_FOUND`, `INTENT_MISSING_MEMBER`,
  `INTENT_EXTRA_MEMBER`, `INTENT_NETS_SHORTED`, `LIB_EMBED_STALE`, `LIB_EMBED_OLD_FORMAT`, and
  the connectivity-gate finding `DANGLING_BUS_ENTRY`.

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
