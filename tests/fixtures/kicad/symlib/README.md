# Symbol-library fixtures (`library new-symbol` / `library lint-symbols`)

Generic parts only — an 8-pin op-amp shape and a 14-pin logic shape. No real
project, vendor or MPN is referenced anywhere here.

The directory is deliberately **not** `symbols/`: that one is a `--symbols`
search path for a dozen draw/arrange tests, and dropping extra libraries into it
changes what their `lib_id`s resolve to.

## `grammar_probe.kicad_sym` — kicad-cli ground truth

Hand-authored in exactly the shape `src/akcli/writers/symbol_lib.py` emits, then
**verified against a real `kicad-cli` 10.0.4** (macOS bundle). It is the
fixture-first evidence behind every grammar claim in that writer:

| claim | how it was verified |
|---|---|
| `(version 20231120)` (KiCad 8 token) is readable by KiCad 10 | `sym export svg` succeeds; `sym upgrade` rewrites it to `20251024` |
| all 12 electrical-type tokens survive a KiCad round-trip | `sym upgrade --force`, re-read with `readers/kicad_lib.py`: identical pin set — including `free` vs `unspecified`, which KiCad keeps distinct |
| `(hide yes)` as a direct `(pin ...)` child means *hidden* | `sym export svg` omits the pin; `--include-hidden-pins` draws it |
| a `Reference`/`Value`/`Footprint`/`Datasheet` property block with the KiCad 8 hide form `(effects … (hide yes))` is accepted | the export above |
| pins on all four sides (angles 0/90/180/270) | the export above |
| two pins on one point with the same name + type (stacked convention) | accepted, both preserved |

`tests/test_symbol_lib_e2e.py` re-runs the export live (skipped when no
`kicad-cli` is installed) so the fixture cannot rot silently.

## Lint fixtures

| file | what it pins |
|---|---|
| `coincident.kicad_sym` | the field incident: an `no_connect` pin (6) sitting exactly on the `power_in` GND pin (5) — `SYMLIB_PIN_COINCIDENT` (ERROR) |
| `stacked_ok.kicad_sym` | the sanctioned same-coordinate case: VCC/VCC and GND/GND pairs, same name **and** same type — clean |
| `multiunit_gates.kicad_sym` | 4 units drawn on one body, every unit's pins at the same coordinates by design, plus `_0_1` common power pins — clean (cross-unit coincidence must never fire) |
| `unspecified_type.kicad_sym` | `unspecified` + `free` pins (`SYMLIB_PIN_TYPE_UNSPECIFIED`) and pin 4 at 50 mil (`SYMLIB_PIN_OFF_GRID`) |

## Spec fixtures

`generic_opamp_8.spec.json` and `generic_logic_14.spec.json` are
`schemas/symbol-pins.schema.json` documents used by the layout, renderer and CLI
tests.
