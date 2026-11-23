# Synthetic KiCad fixtures

Hand-authored, valid S-expression KiCad files used by the KiCad reader/netbuild tests.
The dev/CI box has **no KiCad libraries installed**, so every symbol these schematics
reference is vendored here and also cached inline (`lib_symbols`) in each schematic.
Every **schematic** here is hand-authored — coordinates and UUIDs are hand-chosen so the
files are small and reviewable. The exceptions are the three `netlist_*.kicad10.net` files,
which *are* real `kicad-cli` exports kept as netlist ground truth (see below).

## Netlist ground truth

`netlist_power_entry.kicad10.net` — `kicad-cli 10.0.4 sch export netlist` run on
`tests/fixtures/corpus/power_entry.kicad_sch`. It pins the grammar and the UUID identity
(`(design)`, per-comp `(sheetpath)` + `(tstamps)`, `(libparts)`) that
`src/akcli/exporters.py` must agree with, so `tests/test_e2e_netlist.py` can arbitrate the
exporter **everywhere**, not only on a box with KiCad installed (where the same test
re-derives it live and asserts the fixture has not rotted).

`netlist_hier_root.kicad10.net` — the same, run on `hier/root.kicad_sch` (below). It pins
**net NAMING** across a hierarchy, the rule a flat board cannot show.

Four machine-specific atoms were normalized on commit; nothing structural was touched:

| Field | KiCad wrote | Committed as |
|---|---|---|
| `(design (source ..))` | absolute path of the dev checkout | repo-relative posix path |
| `(design (date ..))` | export wall-clock time | `""` (determinism) |
| `(comp (property (name "Sheetname") (value ..)))` | localized root-sheet name | `"Root"` |
| `(libraries (library (uri ..)))` | `${KICAD10_SYMBOL_DIR}` | `${KICAD_SYMBOL_DIR}` |

## `hier/` — hierarchical net-name ground truth

`hier/root.kicad_sch` instantiates `hier/amp.kicad_sch` **twice** (`Amp`, `Amp Two`), and
that child instantiates `hier/deep.kicad_sch` once (`Deep`) — five sheet instances in all.
It exists to pin one rule: eeschema qualifies a net name with the **human-readable sheet
path** of its driver, unless the driver is a global label or a power pin.

| driver | net name KiCad writes |
|---|---|
| `(label "ROOT_LOCAL")` on the root sheet | `/ROOT_LOCAL` |
| `(label "AMP_LOCAL")` on a child sheet | `/Amp/AMP_LOCAL`, `/Amp Two/AMP_LOCAL` |
| `(label "DEEP_LOCAL")` on a grandchild sheet | `/Amp/Deep/DEEP_LOCAL` |
| `(hierarchical_label "DEEP_OUT")`, parent pin unlabelled | `/Amp/Deep/DEEP_OUT` |
| `(global_label "AMP_GLOBAL")` | `AMP_GLOBAL` — never qualified |
| `power:GND` symbol | `GND` — never qualified |

Every net here carries exactly **one** name, so the fixture tests the qualification rule
without depending on how ties between several names on one net are broken (akcli's pick is
priority-then-alphabetical; eeschema's is GLOBAL > POWER_PIN > LOCAL_LABEL > HIER_LABEL —
they historically disagreed on the `MID`/`VOUT` node of `board_v8` below;
since 0.21 both name it `VOUT`).

The child sheets live in a subdirectory on purpose: `tests/test_roundtrip_netlist.py` globs
`tests/fixtures/kicad/*.kicad_sch` and copies each file **alone** into a temp dir, which a
multi-file design cannot survive.

## `hier_partial/` — reference-resolution ground truth

`root.kicad_sch` instantiates `amp.kicad_sch` twice (`Amp`, `Amp Two`). One resistor
(`…00000000000b`) carries only the first instance's entry (`R102`); its `Reference`
property is `R?`. KiCad writes `R102` on BOTH sheet instances — the symbol's first
entry, never the property — and reports annotation errors
(`netlist_hier_partial_root.kicad10.net`, kicad-cli 10.0.4). akcli resolves the same way
(`readers/kicad.py::_reference`), and `check --integrity` reports the duplicate (the one
`_TREE_ALLOWLIST` entry in `tests/test_check_integrity.py`). Each file alone is clean.

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
    MID──+────VOUT──── C1.1            <- net "VOUT" (global wins), local label "MID"
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
| `VOUT` (aka `MID`) | local `label` + `global_label` | `R1.2`, `R2.1`, `C1.1` |
| `GND` | power port `#PWR02` | `R2.2`, `C1.2` |

Each file contains: an inline `(lib_symbols ...)` cache, 5 symbol instances (R1, R2, C1
plus the two `#PWR` power symbols) each with a `(lib_id ...)` and a synced
`(instances ... (path ... (reference ...)))`, five `(wire ...)` segments, two
`(junction ...)` dots, one `(label ...)`, one `(global_label ...)`, and the GND/+3V3
power symbols. The `VOUT` net carries both a local label (`MID`) and a global label
(`VOUT`) on the same node, so it doubles as a multi-name / alias case for netbuild.

`tests/test_kicad_fixtures.py` validates these files structurally (balanced
S-expressions, required elements present, the v7/v8 format-marker differences) and
re-derives the three nets with a self-contained mini-parser to prove the hand-placed
wire endpoints actually coincide with the computed pin world-coordinates.

## `blank_project/` — project-scaffold ground truth

`blank.kicad_pro`, `blank.kicad_pcb`, `sym-lib-table`, `fp-lib-table` are the exact
bytes `akcli project new` writes for the non-schematic half of a KiCad project. Unlike
the schematics above these are **not hand-authored**: the project file is KiCad's own
`share/kicad/template/kicad.kicad_pro` with `meta.filename` + `sheets` filled in, and
the board's header/layers block is transcribed verbatim from
`share/kicad/template/Arduino_Mega/Arduino_Mega.kicad_pcb` (file version `20241229`,
new-style layer numbering). Nothing here is invented; see the module docstring of
`src/akcli/writers/blank.py` for why.

The `.kicad_pro` fixture pins the UUID `00000000-0000-4000-8000-000000000001` so the
file is byte-stable — `tests/test_scaffold.py` substitutes it when comparing against the
emitter, and asserts byte equality in both directions.

The board is a **container, not a layout**: zero footprints, zero tracks, no outline.
`tests/test_e2e_project.py` runs a real `kicad-cli pcb drc` / `pcb export svg` over both
the fixture and a freshly scaffolded project and requires exit 0; the single expected
`invalid_outline` violation is the "there is no outline" fact, not a defect.

Regenerate (only when the emitter deliberately changes, and re-run the e2e file):

```
PYTHONPATH=src python3 -c "
from pathlib import Path
from akcli.writers import blank
d = Path('tests/fixtures/kicad/blank_project')
u = '00000000-0000-4000-8000-000000000001'
d.joinpath('blank.kicad_pro').write_text(blank.blank_pro('blank.kicad_pro', u), encoding='utf-8', newline='\n')
d.joinpath('blank.kicad_pcb').write_text(blank.blank_pcb('A4'), encoding='utf-8', newline='\n')
d.joinpath('sym-lib-table').write_text(blank.blank_lib_table('sym'), encoding='utf-8', newline='\n')
d.joinpath('fp-lib-table').write_text(blank.blank_lib_table('fp'), encoding='utf-8', newline='\n')
"
```

## `drc_demo/` — KiCad DRC report ground truth

`release preflight --drc` turns `kicad-cli pcb drc --format json` output into gate
findings. The report shape is therefore a KiCad grammar claim, and this directory is its
ground truth: a board, its project file, and the **real** reports kicad-cli wrote for it.

| File | What it is |
|---|---|
| `drc_demo.kicad_pcb` | hand-authored, minimal: the `blank_project` header/layers, an `Edge.Cuts` rectangle, `R1` + `C1` (every footprint, pad and track carries a hand-chosen uuid), and four `F.Cu` segments |
| `drc_demo.kicad_pro` | `blank_project/blank.kicad_pro` with `meta.filename` set, `sheets` emptied, and `board.design_settings.rule_severities` turning `lib_footprint_issues` + `lib_footprint_mismatch` to `ignore` |
| `drc_demo.drc.kicad10.json` | `kicad-cli 10.0.4 pcb drc --format json --severity-error --severity-warning`, KiCad UI language English |
| `drc_demo.drc.kicad10.zh_TW.json` | the same run with KiCad's UI language at its macOS-system default (zh_TW) |

The board is built to produce exactly three entries, one of each kind the gate maps:

| KiCad `type` | severity | cause | items (uuid suffix) |
|---|---|---|---|
| `clearance` | error | the `SIG` segment runs 0.05 mm beside the `GND` segment (default clearance 0.2 mm) | tracks `…402`, `…404` |
| `track_dangling` | warning | the `SIG` segment connects to nothing | track `…404` |
| `unconnected_items` | error | `+3V3` is never routed | pads `…111` (R1.1), `…211` (C1.1) |

Three properties of kicad-cli that the fixture pins, and that
`src/akcli/drivers/kicad_cli.py` is written around:

* **Descriptions are localized.** The two reports differ in every `description`
  (`Pad 1 [+3V3] of R1 on F.Cu` vs `1 上 [+3V3] 的焊盤 R1 F.Cu`) and in nothing else — so
  the gate keys on `type` / `severity` / `uuid` / `pos` and resolves designators and nets
  by looking the item `uuid` up in the board file, never by parsing text.
* **An item with no uuid in the file gets a fresh one at every load** (the pads of
  `../board.kicad_pcb` do): such an item resolves to nothing, which is why every item
  here carries one.
* **`pcb drc` writes into the project directory**: it creates `<board>.kicad_prl` beside
  the board when none exists — even when the board fails to load. The driver therefore
  runs KiCad on a staged copy; `tests/test_kicad_cli.py` asserts this directory is
  byte-identical before and after a real run. The `rule_severities` in the project file
  double as the proof that the `.kicad_pro` travels into that copy: without it KiCad
  falls back to defaults and reports `lib_footprint_*` for both footprints (which, being
  a function of the footprint libraries installed on the machine, would also make the
  report non-reproducible).

One atom was normalized on commit: `"date"` (run wall-clock time) → `""`. `kicad_version`
is kept as written. `tests/test_kicad_cli.py` re-runs kicad-cli on the board wherever it
is installed and compares every locale- and clock-independent field against the English
report.

Regenerate (only when the board deliberately changes; a throwaway `KICAD_CONFIG_HOME`
keeps the language choice out of your own KiCad settings, and the run happens on a copy
so no `.kicad_prl` lands here):

```
tmp=$(mktemp -d); mkdir -p "$tmp/proj" "$tmp/cfg/10.0"
cp tests/fixtures/kicad/drc_demo/drc_demo.kicad_p* "$tmp/proj/"
printf '{"system": {"language": "English"}}\n' > "$tmp/cfg/10.0/kicad_common.json"
KICAD_CONFIG_HOME="$tmp/cfg" kicad-cli pcb drc --format json --severity-error \
  --severity-warning --output "$tmp/drc.json" "$tmp/proj/drc_demo.kicad_pcb"
# then blank the "date" value and copy $tmp/drc.json over drc_demo.drc.kicad10.json
```

## `altium_import/` — KiCad's Altium footprint importer, frozen

`AKCLI_GFX.kicad_mod`, `AKCLI_LAYERS.kicad_mod` and `AKCLI_SKIP.kicad_mod` are what
`kicad-cli 10.0.4 fp upgrade` wrote when it converted a **synthetic** Altium `.PcbLib` —
the one `tests/test_pcblib_graphics.py::_oracle_library` builds from hand-packed records,
so no real library is involved. They are committed byte-for-byte as KiCad wrote them
(nothing normalized: the files hold no path, date or user name) and are ground truth for
`akcli library import-altium`:

| File | Pins |
|---|---|
| `AKCLI_GFX` | pads, tracks → `fp_line`, arcs → `fp_arc` (which end KiCad starts from, and which side `mid` falls on), a 0°–360° arc → `fp_circle`, a full turn written with other angles → a closed `fp_arc` |
| `AKCLI_LAYERS` | one track per Altium layer id akcli maps: the KiCad layer each lands on |
| `AKCLI_SKIP` | what akcli does **not** copy from KiCad's importer: keepout objects (rule areas), a `MultiLayer` track (one copy on each of 32 copper layers), an internal-plane track (a rule area on layer `UNDEFINED`), a mechanical layer past 45 (dropped) |

`tests/test_pcblib_graphics.py` compares akcli's import of the same synthetic library
against these files on every platform, and — wherever `kicad-cli` is installed — re-runs
the conversion and asserts the files have not rotted.

Regenerate (only when `_oracle_library` deliberately changes):

```
PYTHONPATH=src python - <<'PY'
from pathlib import Path
from tests import test_pcblib_graphics as G
G._oracle_library(Path("/tmp/akcli_oracle.PcbLib"))
PY
kicad-cli fp upgrade --force --output /tmp/akcli_oracle.pretty /tmp/akcli_oracle.PcbLib
cp /tmp/akcli_oracle.pretty/AKCLI_*.kicad_mod tests/fixtures/kicad/altium_import/
```

## `fp_attrs/` — board-footprint attribute ground truth

`readers/kicad.py::read_pcb` takes a footprint's placement, assembly flags and 3D models
from its `(at …)`, `(attr …)` and `(model …)` nodes, and `verify` acts on them (DNP / BOM
mismatches, the `board_only` demotion, `verify --library`). What those nodes look like is a
KiCad grammar claim, so this directory holds what a real KiCad wrote.

| File | What it is |
|---|---|
| `attrs_v8.kicad_pcb` | hand-authored input, KiCad 8 syntax (`20240108`): six footprints covering every attribute form below |
| `attrs.kicad10.kicad_pcb` | **`kicad-cli 10.0.4 pcb upgrade`** of that input (`20260206`), then one `(variant …)` block inserted by hand into `R1` and the file re-saved with `pcb upgrade --force` — so KiCad parsed and re-wrote the block |
| `legacy_v5.kicad_pcb` | hand-authored input, KiCad 5 syntax (`20171130`, `(module …)` nodes) |
| `legacy_v5.kicad10.kicad_pcb` | **`kicad-cli 10.0.4 pcb upgrade`** of that input |
| `proj.pretty/*.kicad_mod` | **`kicad-cli 10.0.4 fp upgrade`** output of four hand-authored footprints — the library the board's `proj:` footprints come from |
| `fp-lib-table` | hand-authored: `proj` → `${KIPRJMOD}/proj.pretty` |
| `3d/*.step` | placeholders (not STEP bodies): only their existence and bytes matter |

What KiCad 10 wrote, per input form:

| Input (`attrs_v8` / `legacy_v5`) | KiCad 10 output | Reader |
|---|---|---|
| `(attr smd)` | unchanged | no flag — a type token (`smd` / `through_hole`) is not a flag |
| `(attr smd exclude_from_pos_files exclude_from_bom dnp)` | unchanged | `dnp`, `exclude_from_bom`, `exclude_from_pos` |
| `(attr board_only exclude_from_pos_files exclude_from_bom allow_missing_courtyard allow_soldermask_bridges)` | same tokens, **wrapped over three lines**; no type token (footprint type "unspecified") | `board_only`, `exclude_from_bom`, `exclude_from_pos` |
| no `(attr …)` node at all (`TP1`) | still none | no flag |
| v5 `(module … (attr virtual))` | `(attr exclude_from_pos_files exclude_from_bom)` | both exclusions, **not** `board_only` |
| v5 `(module …)` with a through-hole pad and no `attr` | `(attr through_hole)` | no flag |
| `(at 110 100 270)` (v5) | `(at 110 100 -90)` | `rotation` kept as written (`-90.0`) |
| `(model "…" (hide yes) …)` | unchanged | still a model reference |

Four properties the reader is built on:

* **DNP is a token of `(attr …)`, in every version through KiCad 10.** There is no
  footprint-level `(dnp yes)` node on a board (that spelling belongs to the *schematic*):
  kicad-cli 10.0.4 refuses to load a board whose footprint carries one —
  `test_e2e_footprint_level_dnp_node_is_not_kicad_grammar` re-proves it live.
* **KiCad 10 adds per-footprint `(variant (name "NoFit") (dnp yes) (exclude_from_bom yes)
  (exclude_from_pos_files yes))` blocks** (also `(field (name …) (value …))`; `(in_bom …)` is
  refused there). They are assembly-variant overrides, not the footprint's own flags: `R1`
  carries one while its `(attr smd)` says fitted, and the reader reports the base flags. Only
  the footprint's **own** children are read (`SNode.find_all` is not recursive), which is what
  keeps the nested `(dnp yes)` out.
* **`virtual` means "not in BOM, not in position files"** — KiCad rewrites it to exactly those
  two tokens, for a v5 `(module …)` and for a `(footprint … (attr virtual))` alike.
* **KiCad re-orders footprints on save** (the `B.Cu` footprint `J1` moves last), so tests key on
  the designator, never on position in the file.

`read_pcb` reads `(footprint …)` nodes only: the un-upgraded `legacy_v5.kicad_pcb` yields no
footprints at all (pre-existing behaviour, unrelated to attributes). It is committed as the
*input* of the upgrade, not as a reader case.

Real KiCad 9 output (`20241229`) was checked against the template boards KiCad ships
(`share/kicad/template/*/*.kicad_pcb`: `(attr board_only exclude_from_pos_files
exclude_from_bom)`, `(attr exclude_from_pos_files)`, `(attr through_hole)` — the same grammar).
They are not committed; `test_e2e_boards_shipped_with_kicad_read_consistently` re-reads whatever
templates sit beside the installed kicad-cli.

`tests/test_fp_attrs.py` pins all of the above, and — where kicad-cli is installed — re-runs
both upgrades and compares the PARSED fields with the committed output (not bytes: KiCad mints
fresh uuids for the `Datasheet` / `Description` properties it adds on every save).
`tests/test_fplib.py` and `tests/test_library_baseline.py` use the directory as a project.

Regenerate (only when an input deliberately changes; a throwaway `KICAD_CONFIG_HOME` keeps the
run out of your own KiCad settings, and it happens on copies because `pcb upgrade` rewrites its
argument in place and drops a `.kicad_prl` beside it):

```
tmp=$(mktemp -d); mkdir -p "$tmp/cfg/10.0"; d=tests/fixtures/kicad/fp_attrs
printf '{"system": {"language": "English"}}\n' > "$tmp/cfg/10.0/kicad_common.json"
export KICAD_CONFIG_HOME="$tmp/cfg"
cp $d/attrs_v8.kicad_pcb "$tmp/attrs.kicad_pcb";   kicad-cli pcb upgrade "$tmp/attrs.kicad_pcb"
cp $d/legacy_v5.kicad_pcb "$tmp/legacy.kicad_pcb"; kicad-cli pcb upgrade "$tmp/legacy.kicad_pcb"
# insert after R1's "(attr smd)" in $tmp/attrs.kicad_pcb:
#   (variant (name "NoFit") (dnp yes) (exclude_from_bom yes) (exclude_from_pos_files yes))
kicad-cli pcb upgrade --force "$tmp/attrs.kicad_pcb"
# then copy $tmp/attrs.kicad_pcb -> attrs.kicad10.kicad_pcb, $tmp/legacy.kicad_pcb -> legacy_v5.kicad10.kicad_pcb
```

## `fab/` — outline, via, tenting and drill ground truth for `akcli fab check`

What `read_pcb` tells `fab check` about a board — its true `Edge.Cuts` area, each via's type
and tenting, both dimensions of an oval drill — is a pile of KiCad grammar claims. Every board
here was **written by kicad-cli 10.0.4** (`pcb upgrade --force` of a minimal hand-authored
input, every item carrying a hand-chosen uuid; file version `20260206`), and beside each sits
what the same KiCad **measured** for it: `<name>.stats.kicad10.json` is `kicad-cli pcb export
stats --format json`, UI language English (the report localizes its `vias` / `pads` keys and
the drill `shape` / `source` values). One atom was normalized on commit: `metadata.date` → `""`.

| File | What it pins |
|---|---|
| `outline_cutouts.kicad_pcb` | a 40 × 30 mm board with r = 2 mm corners from `gr_line` + `gr_arc (start) (mid) (end)`; cutouts from `gr_circle (center) (end)`, `gr_rect`, `gr_poly` |
| `outline_circle.kicad_pcb` | a board that is ONE `gr_circle` (r = 15 mm) |
| `outline_poly_arc.kicad_pcb` | a `gr_poly` whose `pts` hold an `(arc (start) (mid) (end))` between `xy` points; the polygon closes implicitly |
| `outline_rect_radius.kicad_pcb` | KiCad 10 rounded rectangles: `(gr_rect (start) (end) (radius 2))` as the body, more as cutouts — one whose radius kicad-cli clamped (written `5`, re-saved `2`: half the short side), an `fp_rect` with `(radius 1)`, and the same in a footprint rotated 30°, which kicad-cli re-saved as an `fp_poly` of four `(arc …)` and no `xy` |
| `outline_open.kicad_pcb` | the rounded board with one corner arc missing — KiCad: `has_outline: false`, `area: null` |
| `outline_footprint.kicad_pcb` | outline pieces drawn INSIDE footprints: a back-side footprint rotated 90° supplies a notch (`fp_line` + `fp_arc`), others supply cutouts (`fp_circle`, `fp_rect`, `fp_poly`) |
| `vias_tenting.kicad_pcb` | 4 copper layers; `(setup (tenting (front yes) (back no)))`; vias: through ×3 (one following the board, one `(tenting (front no) (back no))`, one `(front yes) (back yes)`), `blind`, `buried`, a `blind` token on `In1.Cu`–`In2.Cu`, `micro`, a `blind` from the back |
| `slots.kicad_pcb` | pad drills: `(drill oval 0.8 2.2)`, `(drill oval 0.9)`, `(drill oval 2.2 0.8 (offset 0.2 0))`, `(drill 0.8)`, plated and `np_thru_hole` |
| `rules/rules_demo.kicad_pro` | a project file whose `board.design_settings.rules` object is transcribed verbatim from `share/kicad/template/Arduino_Mega/Arduino_Mega.kicad_pro` (written by KiCad 9), on the `blank_project` skeleton — in its own directory so no board here picks it up by accident |

What the real files establish (each is asserted by `tests/test_kicad_pcb_fab_facts.py`):

* **Area.** KiCad's `area` is the board bodies only; `--subtract-holes-from-board`
  (`*.stats-subtract-holes.kicad10.json`) also subtracts the `Edge.Cuts` cutouts and every
  drilled hole. KiCad tessellates arcs (5 µm error), so its numbers sit a hair under the exact
  ones: a rounded 40 × 30 board is `1196.557 mm²` to KiCad and `1196.566` analytically.
* **Chaining tolerance.** KiCad joins two outline ends 0.009 mm apart and refuses 0.011 mm —
  hence `geometry.outline.CHAIN_TOLERANCE_MM = 0.01`.
* **Footprint edges count.** Remove the footprints from `outline_footprint` and the outline no
  longer closes; a single dangling `fp_line` on `Edge.Cuts` turns `has_outline` false. (187 of
  the 15 435 footprints KiCad 10.0.4 ships draw on `Edge.Cuts`.) A back-side footprint needs no
  extra mirror, and kicad-cli re-saves an `fp_rect` in a rotated footprint as an `fp_poly`.
* **Buried vias.** KiCad 10 has a `buried` token of its own. It does **not** apply it on
  re-save: the `(via blind … "In1.Cu" "In2.Cu")` here stayed `blind`, and KiCad's statistics
  count it as blind (`through 3, blind 3, buried 1, micro 1`). akcli reports it as buried — a
  via touching neither outer layer is one — so its count is `blind 2, buried 2`.
* **Tenting.** KiCad 10 always writes the board setting, and writes a via's `(tenting …)` only
  when the via overrides it (per side: `yes` / `no` / `none`).
* **Drills.** An oval whose two sizes are equal is written with ONE number; KiCad's drill table
  still lists it as a `Slot`.
* **Not modelled: Bezier.** KiCad accepts `(gr_curve (pts (xy) (xy) (xy) (xy)))` in an outline
  (and measures it); akcli does not model it — such an outline is reported as not closed and
  the area falls back to the bounding box, with the reason. No fixture: it is a stated gap.

### Legacy grammars — hand-authored, proven by their re-save

No KiCad 5, 8 or 9 binary was available, so these three shapes could not be verified against a
file *written* by the version that used them. Each is a hand-authored input in the old grammar
plus `<name>.kicad10.kicad_pcb`, the file kicad-cli 10.0.4 made of it — the proof is that akcli
reads the input the way KiCad does.

| File | Old shape | What kicad-cli 10 made of it |
|---|---|---|
| `legacy_v5.kicad_pcb` (`20171130`) | `(gr_arc (start <centre>) (end <first point>) (angle <deg>))`, also as `fp_arc` inside a `(module …)` | `(start) (mid) (end)`; a positive angle is clockwise on screen. Same area either way: `1122.009 mm²` |
| `legacy_v5.kicad_pcb` | `(pcbplotparams (viasonmask true))` — KiCad ≤ 8 kept "do not tent vias" as a plot option | `(setup (tenting (front no) (back no)))` (`viasonmask false`, or absent → `yes` / `yes`) |
| `tenting_v9.kicad_pcb` (`20241229`) | board `(tenting front)`; vias `(tenting front back)`, `(tenting none)`, `(tenting back)` | board: front yes / back no. Vias: a NAMED side → `yes`; an unnamed one is dropped (`(tenting none)` vanishes, `(tenting back)` becomes `(front none) (back yes)`) |

The last row is the one open question. kicad-cli 10 reads an unnamed side of a KiCad 9 *via* as
"follow the board"; the token list itself reads as "not tented". Which KiCad 9 meant is
unverified, so the reader reports such a side as unknown unless the board side is itself open
(both readings then agree).

Real KiCad 9 output was checked against the template boards KiCad ships (`20241229`:
`gr_arc (start) (mid) (end)`, `gr_circle (center) (end)`, `gr_rect`, `gr_poly`, and
`(setup (tenting front back))` in all 19) — not committed; `tests/test_e2e_fab.py` measures
whatever templates sit beside the installed kicad-cli with both akcli and KiCad.

`tests/test_e2e_fab.py` re-derives everything above live wherever kicad-cli ≥ 10 is installed:
the committed statistics against a fresh export, the reader against the live numbers, and each
legacy input against a fresh re-save (parsed fields, not bytes — KiCad mints new uuids for the
properties it adds).

Regenerate (only when a board deliberately changes; on copies, because `pcb upgrade` rewrites
its argument in place and every kicad-cli run drops a `.kicad_prl` beside the board):

```
tmp=$(mktemp -d); mkdir -p "$tmp/cfg/10.0"; d=tests/fixtures/kicad/fab
printf '{"system": {"language": "English"}}\n' > "$tmp/cfg/10.0/kicad_common.json"
export KICAD_CONFIG_HOME="$tmp/cfg"
for n in outline_cutouts outline_circle outline_poly_arc outline_rect_radius outline_open outline_footprint vias_tenting slots; do
  cp "$d/$n.kicad_pcb" "$tmp/$n.kicad_pcb"; kicad-cli pcb upgrade --force "$tmp/$n.kicad_pcb"
  kicad-cli pcb export stats --format json --output "$tmp/$n.stats.kicad10.json" "$tmp/$n.kicad_pcb"
done
for n in outline_cutouts outline_rect_radius outline_footprint; do
  kicad-cli pcb export stats --format json --subtract-holes-from-board \
    --output "$tmp/$n.stats-subtract-holes.kicad10.json" "$tmp/$n.kicad_pcb"
done
for n in legacy_v5 tenting_v9; do
  cp "$d/$n.kicad_pcb" "$tmp/$n.kicad10.kicad_pcb"; kicad-cli pcb upgrade --force "$tmp/$n.kicad10.kicad_pcb"
done
# then blank "date" in each stats JSON and copy the boards + JSON back over $d
```
