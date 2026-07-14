# Acknowledgments

`akcli` is MIT-licensed and ships no third-party code: external tools run as
subprocesses and public services are queried over HTTP. Design *patterns* that informed the
project are credited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Fetch + convert tools (subprocesses)


## Search + parts data

- **JLC2KiCadLib** — LCSC/EasyEDA → KiCad symbol/footprint/3D converter, by
  **TousstNicolas**. MIT. <https://github.com/TousstNicolas/JLC2KiCad_lib>.
  Vendored (conversion core) into `src/akcli/_vendor/jlc2kicadlib/`;
  see `THIRD_PARTY_NOTICES.md` and the in-tree `PROVENANCE.md`.
- **jlcsearch** (tscircuit) — MIT — search front end over the JLCPCB/LCSC catalog; used by
  `akcli jlc search` / `jlc show`. <https://github.com/tscircuit/jlcsearch>.
- **jlcparts** — MIT — open dataset/tooling behind that catalog data.
  <https://github.com/yaqwsx/jlcparts>.

## Data source

- **EasyEDA / LCSC / JLCPCB** — component, symbol, footprint, and 3D-model data; `akcli jlc show`
  does a read-only metadata lookup against EasyEDA's unofficial backend. Trademarks of their
  respective owners; this project is not affiliated with or endorsed by them.

## Design-pattern reference

- **flaco-source / altium-mcp** (2026) and the earlier **coffeenmusic** / **Siddharth Ahuja**
  (2025) Altium-MCP lineage — referenced as a design-pattern source only (file-based JSON bridge,
  `protocol_version` handshake, `ERROR: CODE` strings) for the optional Windows live driver. Full
  attribution in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
