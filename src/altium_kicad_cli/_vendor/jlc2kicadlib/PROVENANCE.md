# Provenance — vendored JLC2KiCadLib

- Upstream: https://github.com/TousstNicolas/JLC2KiCad_lib
- License: MIT (see `LICENSE` in this directory, preserved verbatim)
- Vendored commit: `48d36032108d64b0f59755234681f1ce8bc98d46`
- Files taken: `helper.py`, `footprint/{footprint,footprint_handlers,model3d}.py`,
  `symbol/{symbol,symbol_handlers}.py`. The upstream CLI entry
  (`JLC2KiCadLib.py`) is NOT vendored — akcli drives `create_footprint` /
  `create_symbol` directly from `drivers/jlc2kicad.py`.

## Local modifications (kept minimal, all import-level)

1. `import requests` → `from .. import _http as requests` (stdlib urllib shim;
   keeps akcli zero-dependency).
2. `from KicadModTree import ...` → `from .._kmt import ...`. **KicadModTree is
   GPLv3+ and is deliberately not vendored or imported**; `_kmt.py` is an
   original, clean-room implementation of just the API surface these files use,
   written against the KiCad footprint *file format* (it emits the modern
   `(footprint ...)` s-expression dialect rather than the legacy `(module ...)`
   output of KicadModTree 1.x).

`_http.py` and `_kmt.py` are original akcli code (project MIT license), not
upstream files.
