# Synthetic KiCad fixtures

Hand-authored, valid S-expression KiCad files used by the KiCad reader/netbuild tests.
The dev/CI box has **no KiCad libraries installed**, so every symbol these schematics
reference is vendored here and also cached inline (`lib_symbols`) in each schematic.
Every **schematic** here is hand-authored — coordinates and UUIDs are hand-chosen so the
files are small and reviewable. The one exception is
`netlist_power_entry.kicad10.net`, which *is* a real `kicad-cli` export kept as
netlist ground truth (see below).

## Netlist ground truth

`netlist_power_entry.kicad10.net` — `kicad-cli 10.0.4 sch export netlist` run on
`tests/fixtures/corpus/power_entry.kicad_sch`. It pins the grammar and the UUID identity
(`(design)`, per-comp `(sheetpath)` + `(tstamps)`, `(libparts)`) that
`src/akcli/exporters.py` must agree with, so `tests/test_e2e_netlist.py` can arbitrate the
exporter **everywhere**, not only on a box with KiCad installed (where the same test
re-derives it live and asserts the fixture has not rotted).

Four machine-specific atoms were normalized on commit; nothing structural was touched:

| Field | KiCad wrote | Committed as |
|---|---|---|
| `(design (source ..))` | absolute path of the dev checkout | repo-relative posix path |
| `(design (date ..))` | export wall-clock time | `""` (determinism) |
| `(comp (property (name "Sheetname") (value ..)))` | localized root-sheet name | `"Root"` |
| `(libraries (library (uri ..)))` | `${KICAD10_SYMBOL_DIR}` | `${KICAD_SYMBOL_DIR}` |

## Versions covered

| File | KiCad version | `(version ...)` | Format markers |
|---|---|---|---|
| `board_v7.kicad_sch` | KiCad 7.0 | `20230121` | bare `hide`, `(pin_numbers hide)`, `(fields_autoplaced)`, no `generator_version`, no `exclude_from_sim` |
| `board_v8.kicad_sch` | KiCad 8.0 | `20231120` | `(hide yes)`, `(pin_numbers (hide yes))`, `(fields_autoplaced yes)`, `(generator_version "8.0")`, `(exclude_from_sim no)` |
| `symbols/Device.kicad_sym` | KiCad 8.0 | `20231120` | `R`, `C`, `L`, and `C_Polarized` (`(extends "C")`) |
| `symbols/power.kicad_sym` | KiCad 8.0 | `20231120` | `GND`, `+3V3` power symbols |

The v7/v8 schematics are otherwise **identical circuits** — same components, coordinates
and topology — so a reader test can assert the two format versions parse to the same model.

## Symbol libraries

* `symbols/Device.kicad_sym` — `R`, `C`, `L` (each a passive 2-pin part with pins carrying
  electrical types) plus `C_Polarized`, which uses `(extends "C")` and therefore inherits
  its pins from `C` (exercises the `kicad_lib` extends-resolution path).
* `symbols/power.kicad_sym` — `GND` and `+3V3` power symbols, each a single `power_in` pin
  at the symbol origin `(at 0 0 ...)`.

Pin connection points (the `(at ...)` of each `pin`) are in symbol-local coordinates with
KiCad's library convention (+Y up). A placed instance at `(at PX PY 0)` (rotation 0, no
mirror) puts pin number `N` at world `(PX + lx, PY - ly)`.

## Circuit in board_v7 / board_v8

A resistor divider with a decoupling cap — small but enough for net inference:

```
        +3V3 (#PWR01)
          |
         R1  10k   (Device:R @ 50.8,50.8)
          |
    MID──+────VOUT──── C1.1            <- net "MID", local label + global_label "VOUT"
          |            |
         R2  10k      C1  100n          (R2 @ 50.8,63.5 ; C1 @ 66.04,63.5)
          |            |
          +─────+──────+                <- net "GND"
                |
               GND (#PWR02)
```

Expected nets (component-pin membership):

| net | source | members |
|---|---|---|
| `+3V3` | power port `#PWR01` | `R1.1` |
| `MID` (aka `VOUT`) | local `label` + `global_label` | `R1.2`, `R2.1`, `C1.1` |
| `GND` | power port `#PWR02` | `R2.2`, `C1.2` |

Each file contains: an inline `(lib_symbols ...)` cache, 5 symbol instances (R1, R2, C1
plus the two `#PWR` power symbols) each with a `(lib_id ...)` and a synced
`(instances ... (path ... (reference ...)))`, five `(wire ...)` segments, two
`(junction ...)` dots, one `(label ...)`, one `(global_label ...)`, and the GND/+3V3
power symbols. The `MID` net carries both a local label (`MID`) and a global label
(`VOUT`) on the same node, so it doubles as a multi-name / alias case for netbuild.

`tests/test_kicad_fixtures.py` validates these files structurally (balanced
S-expressions, required elements present, the v7/v8 format-marker differences) and
re-derives the three nets with a self-contained mini-parser to prove the hand-placed
wire endpoints actually coincide with the computed pin world-coordinates.
