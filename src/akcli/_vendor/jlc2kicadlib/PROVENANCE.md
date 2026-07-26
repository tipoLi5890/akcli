# Provenance — vendored JLC2KiCadLib

- Upstream: https://github.com/TousstNicolas/JLC2KiCad_lib
- License: MIT (see `LICENSE` in this directory, preserved verbatim)
- Vendored commit: `48d36032108d64b0f59755234681f1ce8bc98d46`
- Files taken: `helper.py`, `footprint/{footprint,footprint_handlers,model3d}.py`,
  `symbol/{symbol,symbol_handlers}.py`. The upstream CLI entry
  (`JLC2KiCadLib.py`) is NOT vendored — akcli drives `create_footprint` /
  `create_symbol` directly from `drivers/jlc2kicad.py`.

## Local modifications (kept minimal; 1–2 are import-level)

1. `import requests` → `from .. import _http as requests` (stdlib urllib shim;
   keeps akcli zero-dependency).
2. `from KicadModTree import ...` → `from .._kmt import ...`. **KicadModTree is
   GPLv3+ and is deliberately not vendored or imported**; `_kmt.py` is an
   original, clean-room implementation of just the API surface these files use,
   written against the KiCad footprint *file format* (it emits the modern
   `(footprint ...)` s-expression dialect rather than the legacy `(module ...)`
   output of KicadModTree 1.x).

3. `symbol/symbol_handlers.py` — **pin electrical types**. Upstream maps
   EasyEDA's `electric` code straight through, which can only ever emit
   `unspecified`/`input`/`output`/`bidirectional`/`power_in`. EasyEDA's code
   space (5 values) cannot express KiCad's (11), so a plain 0603 resistor
   converts with two `input` pins and trips akcli's floating-input ERC rule.
   `electrical_type()` keeps the code table (`EASYEDA_ELECTRICAL`) and adds two
   context rules: a pin named `NC`/`DNC`/… becomes `no_connect`, and code `4`
   ("Power") becomes `power_out` when the pin name reads like a supplied rail
   (`VOUT`, `VREG`, …) instead of always `power_in`. A component-level
   `default_electrical_type` (set by `symbol.py`, see 4) replaces the
   meaningless undefined/input/output codes of passive parts with `passive`.
4. `symbol/symbol.py` — **Value = the part's value, not the MPN** (+ `MPN`
   property). Upstream writes the component title (an MPN) into `Value` and
   keeps the real value only in the hidden `Resistance`/`Capacitance`/… property.
   `component_value()`/`normalize_value()` pull the value attribute out of the
   EasyEDA `c_para` block and normalize it to KiCad house style (`10kΩ` →
   `10k`, `100nF` → `100n`); the MPN moves to a new hidden `MPN` property
   (id 6, shifting the `supported_value_types` properties to id 7+) and `Value`
   falls back to the MPN when the part has no value attribute.
   `default_electrical_type()` classifies two-pin R/C/L-class parts (reference
   prefix or EasyEDA tags) and hands `passive` to the pin handler.

5. **Windows text I/O** — every text-mode `open(..., "w")` that writes a KiCad
   artifact pins `encoding="utf-8", newline="\n"`. Upstream opens with the
   process defaults, which on Windows means the cp1252 locale codec (symbol
   names and value strings carry `Ω`/`µ`) plus `\n` → `\r\n` translation, so a
   library written on Windows differs byte-for-byte from the same conversion on
   Linux. Patched call sites:
   - `footprint/model3d.py:203` (`.step`/`.wrl` model wrapper)
   - `symbol/symbol.py:253` (`.kicad_sym` library)
   - `_kmt.py:338` (`.kicad_mod` footprint — akcli-original file, but the same
     rule and the same sentinel)

   `tests/test_text_io_portability.py` excludes `_vendor/` from its AST gate
   (vendored code is not ours to restyle), so these three are pinned instead by
   the `test_vendor_*` sentinels in that file. **Re-vendoring drops this patch
   silently** — re-apply it and keep the sentinel green.

Deviations 3 and 4 are additive helper functions plus the two f-string lines
that consume them — they rebase cleanly onto upstream. Deviation 5 is three
one-line keyword additions.

`_http.py` and `_kmt.py` are original akcli code (project MIT license), not
upstream files.
