# Third-Party Notices

`altium-kicad-cli` is licensed under the MIT License (see [LICENSE](LICENSE)) and ships **zero bundled
third-party source code**. It has **no runtime dependencies** (Python standard library only).

This file records the **attribution chain** for projects that were used as **independent-design
references** — that is, for high-level *patterns* only. To be precise about provenance:

> **Independent reimplementation; no source copied; attribution retained where structures are
> referenced.** No source files, and no schema bytes, were copied from any project below. Our JSON
> Schema `$id`, titles, `ERROR` enum, and `protocol_version` field are original to this project.

## Provenance of the ported Altium parser logic

The OLE2/CFBF container-reading approach and the Altium record framing / `|KEY=VALUE|` field tokenizer
were **ported from the author's own prior work** and **relicensed by the same author** from a
proprietary header to MIT for this project. The net-naming
defect in that prior code was deliberately **not** reused; the net layer was rebuilt. This is
first-party material and is covered by this repository's MIT LICENSE; it is listed here only for a
complete provenance record.

## Attribution chain — Altium MCP pattern reference

The optional Windows live-bridge design (`drivers/altium_live/`) and the structured `ERROR: CODE`
convention were informed, as an **independent-design reference for high-level patterns only**, by the
Altium MCP lineage:

- **flaco-source / altium-mcp** (2026) — file-based JSON request/response bridge between an external
  process and a running Altium instance; a `protocol_version` field on the bridge protocol; structured
  `ERROR: CODE` response strings. Referenced for *concept*, not code.
- **coffeenmusic** and **Siddharth Ahuja** (2025) — earlier Altium scripting / MCP work that the above
  builds upon, providing the DelphiScript-drives-Altium pattern.

Patterns referenced (no source copied): a file-based JSON bridge with atomic request/response files; a
protocol-version handshake; structured machine-readable error strings. Everything in this repository
implementing those patterns — the schema namespace, the error-code registry, the op-list vocabulary,
the bridge directory/locking scheme, and all code — is original to this project.

These upstream projects are distributed under the MIT License. The full MIT License text, reproduced for
each attributed copyright holder, follows.

---

### MIT License — flaco-source / altium-mcp (2026)

```
MIT License

Copyright (c) 2026 flaco-source (altium-mcp)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### MIT License — coffeenmusic / Siddharth Ahuja (2025)

```
MIT License

Copyright (c) 2025 coffeenmusic
Copyright (c) 2025 Siddharth Ahuja

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## JLC/EasyEDA fetch + convert (subprocess / service; no source vendored)

`altium-kicad-cli` ships **zero bundled third-party source code**. The `jlc`
feature queries the following HTTP services over the network. No source files
were copied, imported, or linked from any of them.

### jlcsearch (tscircuit) — MIT

Public search service over the JLCPCB/LCSC catalog, queried by `akcli jlc search`/`show`.
<https://github.com/tscircuit/jlcsearch>. (MIT text reproduced below.)

### jlcparts — MIT

Open dataset/tooling behind the catalog data. <https://github.com/yaqwsx/jlcparts>.
(MIT text reproduced below.)

### EasyEDA / LCSC / JLCPCB — data source (not a code dependency)

`akcli jlc show` performs a light, read-only metadata + 3D-availability lookup against
EasyEDA's **unofficial** Std-editor REST backend (`easyeda.com/api/products/...`,
`modules.easyeda.com/...`). These endpoints are undocumented and may change without notice;
they are the same backend the `easyeda2kicad.py` project (uPesy, GPL/community) documents.
We do not vendor easyeda2kicad and do not reuse its conversion code — conversion is
queried directly. EasyEDA, LCSC, and JLCPCB are trademarks of their respective owners;
this project is not affiliated with or endorsed by them.

---

### MIT License — jlcsearch (tscircuit) and jlcparts

```
MIT License

Copyright (c) tscircuit — jlcsearch (https://github.com/tscircuit/jlcsearch)
Copyright (c) yaqwsx — jlcparts (https://github.com/yaqwsx/jlcparts)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

If you believe any attribution here is incomplete or incorrect, please open a GitHub issue so it can
be corrected.
