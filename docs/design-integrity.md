# Design integrity: library workspace, contracts, fab policy, release gate

This page covers the design-integrity surface added after v0.7.0: reliable
format detection, footprint-library reading (`.PcbLib`/`.kicad_mod`), the
project library workspace (`akcli library`), schematic↔PCB equivalence
(`akcli verify`), design contracts (`akcli check --contract`), fab profiles
(`akcli fab`), and the release gate (`akcli release preflight`).

The common thread: **fail loudly, plan before writing, and keep policy in
versioned files with evidence** — never in hardcoded constants.

## Reading footprint libraries

```bash
akcli read part.PcbLib --json
akcli read module.kicad_mod
akcli read board.kicad_sch --strict
```

- `.PcbLib` (Altium, OLE2) decodes each footprint storage: pads carry
  position/size/drill/shape/rotation; anything undecoded (silkscreen, text,
  3D bodies) is surfaced as an `UNSUPPORTED_PRIMITIVE` warning — never
  silently dropped. An unrecognized OLE2 container exits 5 instead of being
  read as an empty schematic.
- `read --json` metadata now carries `detected_format`, `detection_method`
  and `object_counts`; a non-empty source that normalizes to nothing raises
  an `EMPTY_IMPORT` warning, and `--strict` turns that into exit 1.

## Importing an Altium PcbLib into a KiCad library

```bash
akcli library import-altium part.PcbLib --out vendor.pretty --courtyard 0.25
akcli library import-altium part.PcbLib --out vendor.pretty --courtyard 0.25 --apply
```

Pads are carried over verbatim (positions, sizes, drills — never recomputed);
pad geometry is cross-validated in CI against KiCad's own Altium importer.
Transformations are **declared**, not silent: filesystem-safe renames and the
optional synthesized courtyard are reported and recorded, together with the
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

Only the **project** tables are consulted (the global table belongs to the
user's KiCad config), so a globally-registered library reads as a NOTE, not a
false error.

`repair` productizes the two historically hand-`sed`-ed fixes as reviewable
plans (lossless S-expression rewrite, dry-run by default, `--apply` leaves a
`.bak` and re-audits).

## jlc add, integrated with the workspace

```bash
akcli jlc add C12345 --3d --footprint-lib proj_jlc --3d-path absolute
```

`--footprint-lib` sets both the output directory and the nickname written into
the symbol's Footprint field — pass the nickname your project's fp-lib-table
actually registers. `--3d-path` picks the 3D reference policy: `relative`
(portable, resolves only next to the library), `absolute` (always resolves on
this machine, not portable), or a `${VAR}` prefix. The trade-off is printed;
nothing is silently chosen.

## Schematic ↔ PCB equivalence

```bash
akcli verify board.kicad_sch board.kicad_pcb
akcli verify board.kicad_sch board.kicad_pcb --strict --json
```

Compares refdes presence, footprint/value assignment, and the **pad-level net
partition** — net names are untrusted (auto-named nets differ legitimately);
what must hold is that pins joined on the schematic are joined on the board
and vice versa. Findings are located to the designator/pad:
`SCHPCB_NET_SPLIT`, `SCHPCB_NET_MERGE`, `SCHPCB_PAD_MISSING`,
`SCHPCB_FOOTPRINT_MISMATCH`. `#PWR`/`#FLG` pseudo-components are excluded.

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
evidence = ["ACME-REG datasheet, Table 7"]
require = [ { pin = "U1.FB2", net = "VFB2" } ]
forbid  = [ { pin = "U1.FB1", net = "V3V3" } ]

[[contract]]
id = "usb-c-flip-pair"
require_same_net = [ ["J1.A6", "J1.B6"], ["J1.A7", "J1.B7"] ]
forbid_same_net  = [ ["J1.A2", "J1.B11"] ]

[[contract]]
id = "thermal-via"
waived = true
reason = "exposed-pad thermal vias approved"
owner = "hw-lead"
expires = "2027-01-01"
```

Pin specs `REF.PIN` match the pin number first, then the pin name, so rules
read like the datasheet. The report keeps three outcomes distinct: PASS
(`CONTRACT_PASS`, info), FAIL (at the rule's severity), and
SKIPPED-BY-EXCEPTION (`CONTRACT_WAIVED`, note). An expired exception raises
`CONTRACT_EXCEPTION_EXPIRED` instead of silently passing. Intent snapshots
(`check --intent`) keep doing net-membership regression; contracts express
policy — the two compose.

## Fab profiles and the order manifest

```bash
akcli fab check board.kicad_pcb --profile jlc-4l-1oz.toml
akcli fab check board.kicad_pcb --profile jlc-4l-1oz.toml --order order.toml
akcli fab explain FAB_VIA_IN_PAD --profile jlc-4l-1oz.toml
```

A profile is one vendor capability **revision** with its sources
(`[source] urls` + `retrieved_at` are mandatory — see
`examples/fab/jlc-4l-1oz.toml`). Severity policy:

- direct violations (paid small-via geometry, tented drill over the cap,
  via-in-pad, blind/buried vias, stackup drift) are **errors**;
- cost-threshold crossings (board size/area, drill density, fine multilayer
  traces) are **warnings** with the actual value vs the threshold;
- boundary-exact geometry passes with a **note** ("minimum margin");
- a registered `thermal_via` exception passes as an explicit note — and an
  expired one is an error;
- missing evidence (no Edge.Cuts outline) is itself a warning: the check
  never pretends it measured something it could not.

The order manifest is *declared purchase intent* — delivery format, surface
finish, via covering, material, copper weight. akcli validates completeness
and profile consistency but never derives these from the PCB; declaring ENIG
or panel delivery raises explicit review findings for the surcharge rules the
tool cannot compute offline.

## Release preflight

```bash
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --contract board.contract.toml --fab-profile jlc-4l-1oz.toml --order order.toml --out release-manifest.json
```

Runs every applicable gate — schematic checks, intent, contracts, library
audit, sch↔PCB equivalence, fab policy, order manifest, git cleanliness — and
writes a manifest binding input SHA-256s, the akcli version, the git revision
(and, with `--allow-dirty`, the recorded fact of dirtiness) and each gate's
findings. Gates without inputs are **skipped with a reason**, never silently
green. Exit 0 only when every gate passed.

## Writer safety: the GUI-open guard

```bash
akcli draw board.kicad_sch --ops edit.json --apply --allow-open
```

`draw`/`arrange`/`undo --apply` refuse with `TARGET_LOCKED` (exit 6) while
KiCad's `~<name>.lck` lock file is present: a write under an open GUI is a
losing race — the GUI's later save overwrites the file from memory.
`--allow-open` is explicit risk acceptance; after applying, the CLI reminds
you to File>Revert in KiCad. (akcli can refuse to write; it cannot stop an
open GUI from saving later — the reminder is the honest remedy.)
