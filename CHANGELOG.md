# Changelog

All notable changes to `akcli` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning policy

`akcli` ships **three** version numbers; this section is their contract.

- **Package version (SemVer `MAJOR.MINOR.PATCH`).** The single source of truth is
  `pyproject.toml`; `tools/sync_version.py` stamps it into `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, and `.codex-plugin/plugin.json`, and CI fails on drift.
  SemVer rules:
  - **MAJOR** — backwards-incompatible change to the public CLI surface, the normalized data model, or
    the on-disk JSON exports.
  - **MINOR** — backwards-compatible new subcommands, flags, checks, readers, or ops.
  - **PATCH** — backwards-compatible bug fixes and internal changes.

- **`schema_version`** (stamped on every `Schematic`/`Pcb`/`Library` JSON export; currently `"1.0"`).
  Bumped independently of the package version. A **minor** schema bump only adds optional fields
  (consumers must ignore unknown keys); a **major** schema bump may remove or rename fields and
  coincides with a package MAJOR bump.

- **`protocol_version`** (integer; currently `1`) governs the op-list document and the Windows live
  bridge. It is bumped **only** on a breaking change to op shapes, the result object, or the bridge
  handshake. Executors and the bridge **reject a higher major `protocol_version`** with
  `ERROR: PROTOCOL_MISMATCH` rather than guessing. Adding a new optional op or optional op field does
  **not** bump `protocol_version`.

When in doubt, prefer additive, backwards-compatible changes and leave the version contracts untouched.

## [Unreleased]

### Added
- **Altium `.PcbLib` reading** — `akcli read part.PcbLib` decodes footprint
  storages into the new `FootprintDef`/`FootprintPad` library model (pads with
  position/size/drill/shape/rotation, NPTH vs plated, per-type
  `UNSUPPORTED_PRIMITIVE` warnings for undecoded graphics). Pad geometry is
  cross-validated against KiCad's own Altium importer (43/43 pads on a real
  vendor module, zero mismatches). `.kicad_mod` and `.pretty` read into the
  same model.
- **`akcli library` namespace** — the project library workspace as a
  first-class object:
  - `library audit` cross-checks schematics ↔ `sym-lib-table`/`fp-lib-table`
    ↔ library contents ↔ 3D models: unregistered footprint nicknames (the
    "cannot find footprint" trap), missing footprints/symbols, unresolvable
    table URIs, missing/unportable 3D model paths, and legacy pre-v6
    footprints that parse via API but are invisible to the KiCad GUI.
  - `library repair` productizes the two historically hand-`sed`-ed fixes as
    reviewable plans: `--rename-footprint-lib OLD=NEW` (Footprint-field
    nickname rewrite via the lossless S-expression parser) and
    `--3d-path absolute|'${VAR}'`; dry-run by default, `--apply` writes
    atomically with `.bak` and re-audits.
  - `library import-altium part.PcbLib` converts to a `.pretty` library —
    pads verbatim (never recomputed), optional declared `--courtyard`,
    filesystem-safe renames reported, and a `provenance.json` recording the
    source SHA-256, converter version, options and every warning.
- **Deep `.kicad_pcb` reading (schema 1.2)** — pad-level net bindings (both
  `(net N "name")` and KiCad 10 `(net "name")` dialects), absolute pad
  positions (rotation validated against `pcbnew` on a real 4-layer board),
  tracks, vias (through/blind/micro), zones, board setup (copper layers,
  thickness, setup values) and the Edge.Cuts outline bbox.
- **Schematic ↔ PCB equivalence** — `akcli verify sch.kicad_sch
  board.kicad_pcb` compares refdes presence, footprint/value assignment and
  the pad-level net PARTITION (net names untrusted): `SCHPCB_NET_SPLIT`,
  `SCHPCB_NET_MERGE`, `SCHPCB_PAD_MISSING` and friends, each located to the
  designator/pad. `#PWR`/`#FLG` pseudo-components are excluded.
- **Design contracts** — `akcli check --contract contract.toml` asserts
  datasheet-backed topology rules ERC cannot express: require/forbid pin-net,
  require/forbid same-net pin pairs, component values, NC pins — plus
  approved exceptions with owner/reason/expiry. PASS, FAIL and
  SKIPPED-BY-EXCEPTION are all explicit findings; an expired exception warns
  instead of silently passing.
- **Fab profiles** — `akcli fab check board.kicad_pcb --profile p.toml`
  checks a versioned, evidence-carrying vendor policy (TOML with mandatory
  `[source]` urls): free-via geometry boundaries, tenting drill cap,
  via-in-pad (with registered `thermal_via` exceptions), blind/buried bans,
  stackup drift, and cost thresholds (board size/area, drill density, fine
  traces). `--order order.toml` validates the declared purchase intent
  (delivery format, finish, via covering, …) — never guessed from the PCB.
  `fab explain CODE` prints the rule, fix direction and profile sources.
- **Release gate** — `akcli release preflight --sch … [--pcb --contract
  --fab-profile --order --intent]` runs every applicable gate
  (check/intent/contract/library-audit/sch-pcb/fab/order/git-clean), skips
  explicitly with reasons, and writes a manifest binding input SHA-256s,
  tool version, git revision/dirtiness and per-gate findings (`--out`).
- **Fail-loud format detection** — `.PcbLib` is detected by extension; a
  bare OLE2 container is classified by its storage layout (`Board6` → PcbDoc,
  `Library` → PcbLib, `FileHeader` sniff for SchDoc/SchLib) instead of being
  assumed a schematic; an unknown layout exits 5. `read` stamps
  `detected_format`/`detection_method`/`object_counts` into the JSON
  metadata, and a non-empty source normalizing to nothing raises an
  `EMPTY_IMPORT` warning (`read --strict` makes it exit 1).
- **GUI-open write guard** — `draw/arrange/undo --apply` refuse with
  `TARGET_LOCKED` (exit 6) while KiCad's `~<name>.lck` is present;
  `--allow-open` is explicit risk acceptance and successful applies under an
  open GUI print a File>Revert reminder.
- **`jlc add` library integration** — `--footprint-lib NICKNAME` controls
  both the output directory and the nickname written into the symbol's
  Footprint field (previously hardcoded `footprint:` — the #1 "cannot find
  footprint" cause); `--3d-path relative|absolute|'${VAR}'` sets the 3D
  model path policy, with the portability/usability trade-off stated on
  stderr.

### Changed
- `schema_version` 1.1 → **1.2** (additive): `Pcb` gains
  `zones`/`board`/`warnings`/`metadata`, `Library` gains
  `footprints`/`warnings`/`metadata`, new `FootprintDef`/`FootprintPad`.
- New frozen error code `TARGET_LOCKED` (exit 6).

## [0.7.0] - 2026-07-14

### Changed
- **Renamed the project to `akcli`** — the distribution, the Python import
  package (`altium_kicad_cli` → `akcli`), and the Claude Code / Codex plugin
  name. The `akcli` CLI command is unchanged. Positioning is now stated
  honestly: an **AI-native KiCad design agent** (KiCad is the writable target;
  Altium `.SchDoc`/`.SchLib`/`.PcbDoc` are read/imported into the same model,
  with an optional Windows live bridge to a running Altium instance) — not a
  symmetric Altium↔KiCad converter. The config file is now `akcli.toml`; the
  legacy `altium-kicad-cli.toml` is still read as a fallback. Import sites must
  update `import altium_kicad_cli` → `import akcli`. Plugin/marketplace name and
  the `/<plugin>:` command namespace are now `akcli` (install with
  `akcli@akcli`).
- **ROADMAP.md rewritten around the repositioning** — re-anchored from the stale
  "v0.1.0" snapshot to v0.6.0 reality: an honest shipped-history table (the
  planned "v0.5 Altium alive" arc was displaced by KiCad-native depth), ~8
  silently-completed items acknowledged (net-diff delta, waivers, fuzzing,
  kicad-cli parity, BOM pricing, SPICE→full sim, hierarchical write, watch→view),
  forward milestones re-planned (v0.7 agent-contract completeness → v1.0), the
  Altium-interop items demoted to a demand-driven optional track, and the
  PyPI/MCP deferrals recorded as decisions. README roadmap sections (en/zh-Hant/
  zh-Hans) synced.

### Added
- **`akcli doctor`** — one-shot environment report (python / akcli install /
  packaged schemas / kicad-cli / ngspice / config, opt-in `--network` probe of
  the jlc endpoint), each probed exactly the way the features discover them,
  with a remediation hint per missing item. `--require kicad-cli,ngspice,...`
  turns it into a CI gate (exit 1 on a missing capability).
- **`akcli-setup` skill** (9 skills now) — probe-first environment setup and
  repair: drives `akcli doctor`, per-OS remediation for kicad-cli/libngspice/
  network/config, PEP 668 guidance, and a failure-mode quick table.
- **JLCPCB manufacturing-handoff docs** — `docs/jlc.md` and the
  `akcli-jlcpcb-capabilities` skill now cover producing all four order
  artifacts from KiCad: headless `kicad-cli` Gerber/drill/CPL exports (flags
  verified on KiCad 10) + `akcli jlc bom --csv` for the BOM, the CPL header
  renames JLCPCB expects, and links to JLCPCB's own KiCad 8 GUI guides.

### Changed
- **README (en/zh-Hant/zh-Hans) restructured KiCad-first** — the body now leads
  with authoring (op-list write → checks → sim → parts → calculators), reading
  moves late, and "Read Altium files" became **"Import Altium designs"** with
  the read-only wording clarified (file access read-only; the optional live
  bridge drives a running instance). Highlights re-led with the agent-defining
  bullets (AI-agent native, net-diff rails, sim).
- **One kicad-cli discovery ladder** — `drivers.kicad_cli.find()` (KICAD_CLI
  env → PATH → known bundle/install locations, Windows versions numerically
  sorted) now backs the advisory ERC, the `view live` exporter, and
  `akcli doctor`. Real fix: advisory ERC after `draw --apply` previously
  probed PATH only and silently skipped on bundle-only installs (macOS).
- **docs-conformance gate widened** to `INSTALL.md` and `ROADMAP.md` (it
  immediately caught a stale historical calculator count); `INSTALL.md`'s
  verify section now leads with `akcli doctor`.
- **Every skill now carries the `akcli-` prefix** (`akcli-circuit-design`,
  `akcli-design-calc`, ... `akcli-setup`) so loose-folder installs group
  together and users can discover them by typing `akcli` — matching the
  plugin's `/akcli:` command namespace.

### Fixed
- **`sim/builtin.lib` is now shipped in the wheel** — it is read at runtime via
  `Path(__file__)`, but `package-data` declared only `webui/*.html` and
  `schemas/*.json`, so a non-editable (pip) install had no builtin SPICE models
  and `akcli sim` with a builtin comparator/555/phototransistor failed. Added
  `akcli.sim = ["*.lib"]` (and the vendored JLC2KiCadLib `LICENSE`/`PROVENANCE`
  for compliance); a fresh-wheel install now loads all three builtin subckts. A
  packaging test asserts every `Path(__file__)`-loaded resource is declared.
  (Pre-existing gap, surfaced by fresh-wheel verification during the rename.)

## [0.6.0] - 2026-07-13

### Added
- **`akcli sim <sch>` — simulate a schematic with libngspice and assert on the results.** The
  schematic is rendered to a SPICE deck (net → node mapping, component → device via a first-hit-wins
  model-resolution ladder of `Sim.*` KiCad-native fields → `sim.json` `models` overrides → prefix
  heuristic), run through libngspice **in an isolated child subprocess** (crash- and timeout-safe),
  and its `.meas` results are compared against pass/fail bounds (`gt`/`lt`/`ge`/`le`/`approx`+`tol`,
  engineering notation accepted) declared in a `sim.json` spec. `--deck-only` emits the deck and
  exits `0` without any engine (engine-free plan/review mode); `--wave OUT.csv` dumps simulated
  vectors; `--gnd NET` names SPICE node `0`; `--timeout`, `--exit-zero`, and `--json` round out the
  flags. Findings: `SIM_ASSERT_FAIL`/`SIM_MEAS_FAILED` (ERROR), `SIM_UNMODELED`/`SIM_DANGLING_PIN`/
  `SIM_BAD_STIMULUS` (WARNING). The engine is discovered via `AKCLI_NGSPICE` (a path, or `off`) →
  macOS KiCad bundle → `find_library` → Linux sonames → Windows KiCad; a missing engine exits `7`
  (`NGSPICE_MISSING`). `sim.json` is validated by `schemas/sim.schema.json`. The deck builder
  centralizes SPICE's `M`-means-milli / mega-is-`MEG` value fix and the title-line and B-source
  whitespace gotchas. Includes `sim.models.fit_diode()` for turning datasheet forward-voltage points
  into a `.model` line (table-anchored single-point fit preferred over eyeballed curve fits). See
  [docs/sim.md](docs/sim.md).
- **`akcli sim fit-diode` — datasheet → SPICE model, on the command line.** Fit a diode `.model`
  from `--point V@I` datasheet forward-voltage points (`--rs-point` for series resistance, `--cjo`
  for junction capacitance, `--n-prior`/`--name`); prints the `.model` card + `Sim.Params` (or
  `--json`). `--apply SCH --designator REF` plans a native `set_component_parameters` write that
  stamps `Sim.Device`/`Sim.Params` onto that component — a **dry run** printing the op-list unless
  `--write` (which commits through the KiCad writer with a rotated `.bak`). Closes the datasheet →
  model loop with `jlc datasheet` (+ `jlc datasheet --resolve-mpn`, below).
- **`akcli sim --sweep NAME=v1,v2,...` — corner matrices.** Re-runs the assert pass across a
  repeatable Cartesian corner matrix (component-value override `R21=2.2k,3.3k` on a schematic deep
  copy, or `temp=0,25,60` injected as `.option temp=`); ≤64 corners, engine-only. Prints a
  per-corner verdict table (or `{corners:[...]}` JSON) and exits `1` if any corner fails.
- **Two-sided asserts** — a `sim.json` assert entry may now carry both a lower (`gt`/`ge`) **and** an
  upper (`lt`/`le`) bound in one entry (e.g. `{"ge":"3.0","le":"3.6"}` for a rail window); the
  violated side is named on failure. `approx` stays exclusive.
- **`--wave OUT.csv` now writes a tidy CSV** — a single `time` column plus one verbatim column per
  `options.wave_vectors` entry (ngspice's `wrdata` repeats the scale column in front of every vector;
  `sim/wave.rewrite_wrdata()` collapses it). Paths with spaces are handled.
- **Sim floating-node hardening.** The deck builder detects a net with no DC path to ground
  (`SIM_FLOATING_NODE`, naming the stranding parts) and, under the default `options.rshunt` policy
  (absent/`"auto"`), auto-appends `.option rshunt=1e12` to keep the DC operating point solvable
  (`SIM_RSHUNT_ADDED` NOTE); `false` never emits it, a number/string always does. A power-named net
  (`+*`/`VCC*`/`VDD*`/`VBAT*`/`VSUP*`) with no voltage-source drive warns `SIM_UNDRIVEN_RAIL`.
- **`akcli jlc datasheet --resolve-mpn`** (schematic target) — MPN-only BOM lines get an exact-match
  catalog lookup (same policy as `jlc bom`) to pin a C-number before the EasyEDA resolve; misses stay
  `not-found` with a nearest-MPN note, network errors exit `7`.
- **Anti-drift docs gate** (`tests/test_docs_conformance.py`) — every ` ```-fenced `akcli …` line in
  the docs is validated against a live `build_parser()` (unknown subcommand or flag fails the test),
  and the `N ops` / `N macros` / `N calculators` counts (plus their zh-Hans/zh-Hant forms) are
  asserted equal to the live registries. A ` # doc-noqa` comment opts a genuinely non-command fenced
  line out.

### Changed
- **`akcli jlc datasheet` output now cross-links the datasheet → model loop** and its structured
  positions coverage was extended: the `intent`, `bom`, and `libsync` checkers now attach structured
  `pos`/`anchors` to their findings (SARIF fingerprints stay byte-stable for genuinely positionless
  cases).
- **CI: mypy beachhead extended to `src/akcli/calc/`** (added to `[tool.mypy] files` in
  `pyproject.toml` — enforced by the existing bare-`mypy` CI step) alongside `parts/`.
- **CI: GitHub Actions bumped** to current major versions (`actions/checkout` v7, `setup-python` v6,
  `setup-node` v6, `upload-artifact` v7, `download-artifact` v8, `softprops/action-gh-release` v3),
  and the Linux `kicad-cli` job now installs libngspice and **fails** if any libngspice-gated live sim
  test is skipped (no silent skips).
- **`bus_alias` is documented as an arbitrated no-op** — ground-truth tested against `kicad-cli`
  10.0.4, a `(bus_alias ...)` declaration is ignored for netlisting (an alias-labeled bus is
  member-less), so `akcli` matches it by design.

### Fixed
- **docs/sim.md gained a floating-node troubleshooting section** — skipping an IC can strand cap-only nets (555 CV, regulator VIN), the DC operating point goes singular, and the transient starts unconverged; the documented `options.extra_cards: [".option rshunt=1e12"]` remedy was live-validated (spiro rev C channel matches its reference sim to 6 significant figures).
- **`akcli sim` adversarial-review round.** (1) **Diode/BJT polarity** — the deck builder now derives
  a diode's/transistor's SPICE terminal order from pin **names** (`A`/`K`, `C`/`B`/`E`), not pin
  numbers; KiCad numbers stock diode pins `K`=1/`A`=2, so the old pin-number order silently reversed
  polarity (a peak/envelope detector charged the wrong way). Ambiguous names now emit a
  `SIM_PIN_ORDER_ASSUMED` warning. (2) A net literally named `N1` is no longer merged with a generated
  unnamed-net node — the generator skips reserved tokens (`SIM_NODE_COLLISION` on a true clash).
  (3) A `spec.models` entry with a missing/unknown `device` infers the element letter from its
  `model_card`/subckt (or fails `BAD_CONFIG`) instead of emitting a bare designator that ngspice
  mis-parses. (4) A deck ngspice cannot parse is now an engine failure (exit `7` with the parse error
  visible) instead of a false clean exit, even for a zero-assertion spec. (5) `engine.run` resolves a
  relative `workdir` before spawning the child. (6) A stimulus `node` matching no net warns
  `SIM_UNKNOWN_STIMULUS_NODE` with close-match suggestions. (7) Stimulus `name` is now required,
  identifier-shaped (`^[A-Za-z][A-Za-z0-9_]*$`), and unique. (8) `fit_diode` validates/clamps its
  single-point `n_prior` and rejects non-positive `rs_point` inputs. (9) Windows KiCad discovery sorts
  install versions numerically (`10.0` > `9.0`). (10) Node sanitizing restricts to ASCII
  `[A-Za-z0-9_]`. (11) `--wave` paths containing spaces now work. (12) The unmodeled-device hint points
  at real surfaces (`Sim.*`, `spec.models`, `models.fit_diode`) instead of a then-nonexistent
  `akcli sim fit-diode` subcommand (which this cycle now ships — see Added above).

## [0.5.0] - 2026-07-13

### Added
- **`akcli jlc datasheet <C-number|MPN|sch> [--fetch] [--out DIR] [--force]`**
  — datasheet PDF resolution and download. Targets one C-number, one exact
  MPN (catalog-matched like `jlc bom`), or a schematic (every BOM line with
  an `LCSC` parameter, batch). Resolution reads the part's EasyEDA component
  record — the jlcsearch mirror never carries datasheet links and lcsc.com
  bot-gates plain fetches, but the record embeds the szlcsc-hosted PDF URL
  in `head.c_para.link` (symbol *or* footprint side; both checked, which
  also fixed `jlc show --easyeda` missing footprint-side links). `--fetch`
  verifies the `%PDF` magic so an HTML challenge page is never saved as a
  `.pdf`, writes atomically under `--out`/`AKCLI_DATASHEET_DIR`/
  `~/.cache/akcli/datasheets/` as `C<digits>_<MPN>.pdf`, and treats existing
  files as a cache (`--force` refetches). Links are **classified**, not
  trusted: direct `.pdf` → `resolved`; product/viewer pages (JS shells,
  bot-gated hosts) → `page-link` with the URL surfaced for a browser-grade
  fetcher; search-engine junk in the EasyEDA data → `no-link` with the LCSC
  product-page hint. Lint-style exits: `1` while anything short of a PDF
  remains, `7` on network failure, `no-lcsc` lines are advisory.
- **skills**: `schematic-authoring` gained a datasheet-driven design loop
  (fetch → read electrical characteristics → cross-check with `akcli calc`
  → margin-check against absolute maximum ratings); `design-calc` now points
  calculator inputs at datasheet values instead of guesses.
- **`akcli new [path] [--paper SIZE] [--title T] [--force]`** — bootstrap a
  minimal blank `.kicad_sch` (root uuid + paper + optional title block) that
  `draw`/`plan` can append to immediately, replacing the hand-seeded skeleton.
  `--json` = `{created, target, paper, title, status}`.
- **Multi-level `akcli undo`** — `draw --apply` now rotates up to 3 backups
  (`<name>.bak` newest, `.bak2`, `.bak3`). `undo --list` prints the stack
  (level/path/size/mtime, newest first); `undo --steps N` walks back N snapshots
  leaving one redo available.
- **`akcli add_sheet` op (18 ops now)** — hierarchical child-sheet reference:
  emits `(sheet …)` with `Sheetname`/`Sheetfile`, deterministic uuids, and
  edge-computed sheet pins (`pins:[{name, type, side, offset_mil}]`). `at` =
  top-left mils; wires attach to a sheet pin BY COORDINATE (no `Sheet.Pin`
  endpoint). A cross-sheet net is a parent sheet-pin paired with the child's
  same-name hierarchical label (matches eeschema). KiCad only; the child
  `.kicad_sch` is authored separately (`akcli new`).
- **`check --fail-on {info,note,warning,error,never}`** — sets the minimum
  (post-waiver) finding severity that exits non-zero (default `warning` = prior
  behavior). `--exit-zero` is now a deprecated alias for `--fail-on never`.
- **`[[waiver]]` config table** (checker-agnostic, independent of the ERC-only
  `[[erc_waiver]]`): `{code (fnmatch), refs? (fnmatch glob/list), severity?
  off|note|info, reason?}` — `off` drops, `note`/`info` demote a finding,
  applied centrally before rendering and the exit decision. Bad shape →
  `BAD_CONFIG`. The metadata header always prints `config-waived: N (M demoted)`.
- **Structured finding positions** — findings may carry `pos` (`[x_mil, y_mil]`,
  model frame) and `anchors` (`{kind,id[,pos]}`, kind ∈ component|pin|net|label).
  Text appends ` @ (x,y)`; JSON emits `pos`/`anchors` only when present (positionless
  findings keep their exact prior shape); SARIF adds `logicalLocations` +
  `properties.akcli.{pos,anchors}` while `partialFingerprints` stay byte-stable.
- **Design-intent v2** (`check --intent`, additive; `protocol_version` stays 1):
  per-net mode override (`{"members":[…], "mode":"exact"|"subset"}` overrides the
  document mode for one net) and `fnmatch` wildcards on the REF part of a member
  (`"R*.2"`, `"U?.1"` — pin literal). A wildcard is an existence assertion (≥1
  matching pin; zero → `INTENT_MISSING_MEMBER` naming the pattern) and is ignored
  when computing `INTENT_EXTRA_MEMBER` in exact mode.
- **Live web UI lint overlay** — the `/live` dashboard gained a dashed-square
  findings overlay (toolbar `lint` / key `G`) drawn from a new
  `GET /api/findings` (fast offline nets+geom+layout lint, mtime-cached, mils→mm
  via `MIL_TO_MM = 0.0254`), plus per-line **datasheet links** on the networked
  BOM `?check=1` view (resolved via `parts.datasheet.resolve`, per-line tolerant).
- **Real bus netlist semantics (stage 2)** — `netbuild` models buses arbitrated
  against `kicad-cli`: `(bus_entry)` conduction, labeled-rip member selection
  (unlabeled rips float), `NAME[a..b]` inclusive vector expansion, and
  scope-correct cross-sheet bus-name merging. `akcli net`/`diff` on imported
  KiCad bus designs now report bus-carried nets. `model.NetPrimitives` gained
  `buses`/`bus_entries` (default-empty; Altium reader untouched).

### Changed
- **`netbuild` is near-linear** — a shared `O(log n + k)` orthogonal-segment
  index replaces the O(n²) geometric scans in net building and the connectivity
  writer; a ~5100-segment sheet now builds in a fraction of a second with
  byte-identical semantics.
- **`battery-life` default `derating` 0.7 → 0.8** — now aligned with `battery`,
  so the two give identical hours for the same capacity/current (override with
  `derating=`). `capacity=2500 i_avg=10` → 200 h / 8.33 d.
- **calc input-suffix rule** — a parameter whose declared unit is already
  milli-denominated (`battery-life`'s `capacity` in mAh, `i_avg` in mA) now
  takes a bare number and **rejects** a trailing engineering `m`
  (`capacity=2000m` → `ERROR: capacity is already in mAh`) instead of silently
  applying a compounding 1000× milli. The generic length unit `m` (meters) is
  unaffected.

### Fixed
- **`akcli view` port auto-increment now works on Windows** — the server
  inherited `allow_reuse_address` (SO_REUSEADDR), which on Windows means
  "bind even while another socket is LISTENING": a second `akcli view`
  silently shared/hijacked port 8765 instead of moving to 8766. The flag is
  now POSIX-only (there it merely skips TIME_WAIT). CI hardening from the
  same round: the webui test kicad-cli stub runs through a per-OS launcher
  (Windows CreateProcess cannot exec shebang scripts — WinError 193), and
  the browser regression asserts the theme *flips* instead of hardcoding
  the end state (the initial theme follows the runner's
  `prefers-color-scheme`).

## [0.4.0] - 2026-07-11

### Added
- **Net-connectivity diff on every `plan`/`draw`** — the op-list is
  dry-applied to a temp copy and the before/after netlists are diffed by
  **pin membership** (never by name, so renames can't masquerade as
  remove+create). A deterministic "Net changes:" block prints splits,
  merges, membership edits, renames, created and removed nets most-severe
  first (`! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.2>(2)`); `(none)`
  when connectivity is provably unchanged. `--no-net-diff` opts out;
  `draw --apply --strict-nets` **refuses the write** (exit 6) when a
  split/merge touches a named net. `--json` carries
  `net_diff: {equivalent, risk, lines}`.
- **`akcli nets <sch>`** — every net → sorted members on one line each
  (`--json` for machines); `--intent-snapshot OUT.json` writes the netlist
  as a design-intent document (`--include-unnamed` keys unnamed nets by
  stable id).
- **`akcli check --intent FILE`** — first-class **design-intent
  assertions**: a JSON file (`{"protocol_version":1, "mode":"exact"|"subset",
  "nets": {"SWCLK": ["U1.4","J2.2"]}}`) is asserted against the built
  netlist, matched by pin membership. Finding codes: `INTENT_PIN_UNKNOWN`,
  `INTENT_NET_NOT_FOUND`, `INTENT_MISSING_MEMBER`, `INTENT_EXTRA_MEMBER`
  (exact mode), `INTENT_NETS_SHORTED`. Snapshot → edit → assert round-trips
  cleanly; `--intent` alone runs as a pure selector like `--erc`.
- **`akcli relink-symbols <sch>`** — automated re-embed of stale
  `lib_symbols` cache entries from fresh `.kicad_sym` libraries (`--libs`
  dirs/files, default KiCad.app SharedSupport; `--only` scopes nicknames).
  Dry-run by default; `--apply` is gated by a **net-membership equivalence
  proof** (a moved pin in the new library refuses with `VERIFY_FAILED`,
  file untouched) and leaves `<name>.bak`. Companion check:
  `check --libsync [--symbols DIR]` warns `LIB_EMBED_STALE` on
  pin-signature drift (graphics-only drift stays silent) or notes
  `LIB_EMBED_OLD_FORMAT` without sources (opt-in only).
- **`rename_net` core op** (17 ops now) — rewrites matching label texts and
  power-port net Values; optional `scope` restricts the label kind; zero
  matches is a replay-safe note; match count reported. KiCad only.
- **`delete_component` `cascade: true`** — also deletes wires ending on any
  deleted pin's coordinate plus labels/no-connects/junctions anchored
  there (cascaded uuids reported). **`delete_object` `match`** selector
  (`{kind, name?, at?}`) addresses an object without a uuid —
  exactly-one semantics (0 = replay-safe note, >1 = error listing
  candidates).
- **`mid(REF.PIN,REF.PIN)` anchors** — `add_net_label` and the power-port
  ops accept the midpoint of two axis-aligned pins (25-mil tolerance,
  grid-snapped along the wire axis, clamped into the span); labels
  auto-orient along the wire.
- **Three connectivity macros** (9 macros now): `connect_and_label`
  (pin-to-pin wire + ONE mid-wire label — the fix for facing-pin label
  collisions), `place_pwr_flag` (`power:PWR_FLAG` placed MID-WIRE, never
  on-pin), `terminate_unused_unit` (place a spare op-amp/comparator unit +
  tie the inputs + no-connect the output in one op).
- **Op-list validator hardening** — unknown fields are now errors with a
  did-you-mean suggestion (`_`-prefixed keys stay annotation-safe);
  per-op field types are enforced with the op index and field named;
  duplicate `(designator, unit)` placements are a lint error
  (`delete_component` releases the designator); a crashing op handler is
  contained as a per-op `INTERNAL` error result, never a traceback.
- **New offline checks** (all advisory): `check --nets` on `.kicad_sch`
  gains `NET_PIN_MIDSPAN_TOUCH` (pin tip on a wire mid-span with no
  junction — NOT connected in eeschema), `NET_LABEL_UNATTACHED`, and
  `NET_WIRE_CORNER_ON_PIN` (the L-wire short trap); `check --layout` gains
  `LAYOUT_POWER_ON_PIN` (a PWR_FLAG/power symbol anchored on another
  symbol's pin tip), `LAYOUT_WIRE_THROUGH_SYMBOL`, and
  `LAYOUT_LABEL_OVER_WIRE`; `check --erc` gains `ERC_UNPLACED_UNIT`
  (unplaced units of a multi-unit part, `.kicad_sch` only; waiver token
  `unplaced_unit`).
- **Configurable schematic grid** — `[project] grid` in
  `akcli.toml` (bare number = mils, or `"50mil"`/`"1.27mm"`/
  `"0.5mm"`; default 50 mil); `check --nets` compares in exact integer
  nanometres, so metric grids are first-class.
- **Bus-entry connectivity gate** — wires may terminate on a bus entry's
  ends without false-dangling, and every bus entry end must land on a bus
  or a wire: a floating end is a new `DANGLING_BUS_ENTRY` ERROR that
  refuses the write like `DANGLING_ENDPOINT`.
- **`jlc bom --csv OUT.csv`** — JLCPCB upload BOM CSV
  (`Comment,Designator,Footprint,LCSC Part #`; refs comma-joined in one
  quoted cell; unresolved lines get a blank LCSC cell so a dead C-number
  never lands in an order file; `'-'` = stdout). **`--fix` is now
  confidence-gated** (writes only when the package matched AND the value
  is visible in the candidate description/MPN); **`--fix-all`** also
  writes low-confidence suggestions.
- **jlc network resilience** — transient failures (URLError/timeout/HTTP
  429/5xx) retry with exponential backoff honoring `Retry-After`; when
  retries are exhausted a stale cached response is served with a stderr
  warning (`AKCLI_JLC_CACHE_STALE=off` restores hard failure); cache
  writes are atomic.
- **Four more calculators (60 total)** — `battery-life` (datasheet mAh →
  runtime, ANSI C18.1M), `comparator-hysteresis` (open-drain-aware
  thresholds, TI SLVA954), `envelope-detector` (RC validity verdict),
  `ldo-headroom` (go/no-go + dissipation, TI SLVA079).
- **CLI UX** — one unambiguous status line on plan/draw
  (`dry-run — nothing written` / `APPLIED — wrote ... (backup ...; akcli
  undo reverts)` / `REFUSED — nothing written`); did-you-mean suggestions
  for mistyped calc and ops names; duplicate-designator reader warnings
  now print to stderr; `ops list`/`arrange`/`undo` honor `--json`.
- **Packaging & CI honesty** — `[project.urls]`, setuptools ≥ 77 floor
  (PEP 639), Python 3.14 classifier + CI matrix entry, a fast `ruff` lint
  job, and a wheel-smoke job (build → install into a fresh venv →
  `akcli --version && akcli ops list && akcli ops template add_wire`);
  the ops schema ships inside the wheel
  (`akcli/schemas/`, repo-root `schemas/` stays canonical).
- **`akcli arrange <sch>`** — closes the layout loop: nudges **free**
  components (no wire endpoints or label anchors on any pin — moving
  anchored parts would strand their connectivity) until no symbol boxes
  overlap. Greedy first-fit in reading order, `--grid`/`--margin` tune the
  packing, dry-run by default; `--apply` writes through the standard draw
  pipeline (`.bak` + connectivity re-verify), so `akcli undo` reverts it.
- **`jlc bom --qty N`** — purchasability at build quantity: each line needs
  `qty × refs` pieces, stock is checked against that, the applicable
  **price tier** is selected at that quantity, and the table gains
  NEED/UNIT/EXT columns plus an estimated parts cost per run
  (`totals` in `--json`).
- **`jlc bom --suggest` / `--fix`** — for `not-found` / `no-part-id` lines,
  search the catalog by value + footprint package (`100n` + `C_0402_…` →
  `100nF 0402`; candidates must match the package, in-stock Basic parts
  win) and print the best replacement; `--fix` writes the C-number back
  into the schematic's LCSC parameter (same key when one existed) through
  the draw pipeline (`.bak`, undo-able), then re-checks. Suggestions are
  heuristics — verify the datasheet.
- **jlc HTTP cache on by default** — `search`/`show`/`bom` reuse the
  existing on-disk cache (1 h TTL) under `~/.cache/akcli/jlc`
  (`AKCLI_JLC_CACHE` relocates or disables it; tests isolate via conftest).
- **Four more macro ops** — `place_pullup`, `place_led_indicator`,
  `place_rc_filter`, `place_crystal` (ST AN2867 topology), all label-on-pin.
  The validator now accepts **un-expanded** macro documents (checking macro
  required fields), so externally validated op-lists may carry macros.
- **`calc --ops` emits macros** — `vdivider-design`, `led`, `i2c-pullup`
  and `crystal-caps` now produce compound ops with placeholder net names
  (edit, `plan`, draw) instead of loose part strips: the parts arrive
  connected.
- **Live dashboard BOM panel** — `b` / the `bom` toolbar button opens the
  watched sheet's BOM (offline); *check purchasability* triggers the ONE
  networked action (`GET /live/bom?check=1`) with stock/price/est-cost.
- **CFBF/Altium fuzz suite** — seeded mutations (header damage, FAT
  surgery, truncation, byte noise) over the OLE2 container reader; the
  `ALTIUM_*` guard rails held with zero findings.
- **Schema contract tests** — `read --json` exports of KiCad *and* Altium
  fixtures now validate against `schemas/schematic.schema.json` in CI, and
  the schema's `schema_version` const is pinned to the model's.

- **`akcli view` — ONE dashboard server** (HTML ships as package data; binds
  127.0.0.1, zero deps): `/calc` and `/live` are served by a single process
  on port 8765 (auto-increments when busy). `/` is the **hub entry page** the
  browser opens on launch — one card per dashboard, the live card streaming
  the watched file, step count and latest ERC state over SSE; all pages
  cross-link. `view calc` serves the bench alone;
  `view live <sch>` (or the shorthand `view <sch.kicad_sch>`) additionally
  watches the schematic: each change exports every sheet's SVG via
  `kicad-cli`, counts parts/nets with the in-process reader, and appends a
  timeline step **immediately** — KiCad's JSON ERC back-fills the step
  seconds later, so a draw shows up in ~3 s instead of ~15 s. Updates are
  pushed over Server-Sent Events (`/live/events`); responses gzip when
  accepted; step SVGs serve as immutable. New endpoints: `POST /live/note`
  (annotate the next step from the UI) and `POST /live/clear`; new flag
  `--max-steps N` bounds the timeline (default 500, oldest SVGs deleted).
  The standalone `tools/calc-view/` and `tools/live-view/` directories are
  removed.
- **`view` bench UI (`/calc`)** — full dashboard rebuild: home launcher over
  all groups, ⌘K command palette + fuzzy sidebar filter, debounced
  auto-compute with live engineering-notation parse hints (`4k7` → `= 4.7 kΩ`)
  and field-level error highlighting, defaults shown in typed-back notation
  (`35u`, not `0.000035`), per-result change chips vs the previous run,
  click-to-copy exact values (with mm/mil tooltips), copy-as-markdown/JSON/CLI,
  diagram captions annotated with the computed values (Z0, Vout, τ/fc, …),
  a persistent session log, an `⤓ op-list` button (backed by a new
  `GET /api/ops`, the web twin of `calc --ops`) on the 8 mappable calculators,
  pinned/recent lists, shareable URL hashes, dark bench / light datasheet
  themes (theme-aware SVG illustrations), print stylesheet, and a status bar
  that mirrors the equivalent `akcli calc` command. `/api/list` now carries
  `meta` (count, version, watched file) and a per-calculator `mappable` flag.
- **`view` watch UI (`/live`)** — same rebuild: per-step **ERC violation
  panel** (each step stores the KiCad JSON ERC findings; **NEW** findings vs
  the previous step are tagged and counted, resolved ones reported; click a
  finding to zoom to its marker on the sheet), ERC marker overlay, **diff
  mode** (previous step ghosted in red under the current one), sheet tabs for
  hierarchical designs, timeline replay, parts/nets delta per step,
  parts-trend sparkline, PNG export of the current view, `ink` dark-paper
  mode, an in-UI note box for the next step, a clear-timeline action,
  relative timestamps (steps carry an epoch `ts`), and a keyboard map
  (`←/→ L F C D E I space ?`).
- **Browser UI regression suite** — `tools/ui-test/` drives the system Chrome
  (puppeteer-core, no download) through ~35 checks on all three pages;
  `tests/test_webui_browser.py` wires it into pytest (auto-skips when
  node/Chrome are absent) and CI runs it on the macOS runner.
- **Shared bench chrome** — hub, `/calc` and `/live` now carry the identical
  top bar: the `⌂ akcli` mark and a `⌂ | calc | live` page switcher on the
  left, theme + help on the right, `h` returns to the hub from any page.
  One layout to learn; no page is ever a dead end.
- **`akcli verify <a> <b>`** — a net-equivalence proof between two schematics
  (e.g. an Altium original vs its KiCad conversion): PASS iff the component
  set matches and every net's pin membership is identical; net renames are
  reported but do not fail; `--strict` also fails value/footprint drift.
  Exit 0/1; `--json` carries the full diff report.
- **`akcli undo <sch>`** — swaps a `.kicad_sch` with the `<name>.bak` that
  `draw --apply` leaves beside it (dry-run preview by default, `--apply` to
  swap; undo twice = redo). The preview shows the part/net delta.
- **Macro ops** — `place_divider` and `place_decoupling` expand to core ops
  before validation (never touching `protocol_version`, the schema, or the
  executors), using the collision-proof label-on-pin pattern for
  connectivity. `ops list` shows them; `ops template <macro>` works.
- **`akcli check --nets`** — connectivity-hygiene checks, in the default
  check set: `NET_SINGLE_PIN` (floating label / undriven power port) and
  `NET_OFF_GRID` (pins off the 50-mil grid — the classic wire-that-touches-
  but-never-connects trap).
- **Parser fuzzing** — seeded stdlib fuzz of the s-expression parser
  (truncation, paren storms, quote damage, Unicode noise, depth bombs);
  the contract is "SNode or structured AkcliError, never a crash".

- **`akcli jlc bom <sch>`** — BOM → JLCPCB purchasability bridge: every BOM
  line resolves to a catalog part (explicit LCSC C-number parameter first,
  then exact-MPN search preferring in-stock Basic parts) and reports stock /
  price / Basic-Preferred, with `low-stock` (`--min-stock N`),
  `out-of-stock`, `not-found` and advisory `no-part-id` statuses. Lines
  group by identity (one lookup per part, `QTY` = ref count), `#`-virtual
  parts are excluded, Altium and KiCad inputs both work. Lint-style exit 1
  on problems, exit 7 on network errors, `--json` for machines.
- `AKCLI_JLC_BASE_URL` overrides the jlcsearch endpoint (self-hosted
  instance, moved service, or exercising the `NETWORK`/exit-7 path in tests).

- **`akcli check --layout`** — geometric-overlap lint for `.kicad_sch` (also in
  the default check set): estimates world-space boxes for symbol bodies (from
  the embedded `lib_symbols` graphics) and label text, then reports
  `LAYOUT_SYMBOL_OVERLAP`, `LAYOUT_LABEL_OVER_SYMBOL`, `LAYOUT_LABEL_OVERLAP`,
  and `LAYOUT_COINCIDENT_TEXT` findings. A schematic can pass ERC with every
  label drawn on top of the part it names — ERC never checks graphics.
- **`"at": "REF.PIN"` anchors for labels and power ports** — `add_net_label`,
  `place_power_port`, `place_gnd`, `place_vcc` accept a pin reference as `at`
  (exact world coordinate, never grid-snapped), making the collision-proof
  label-on-pin pattern first-class.
- **Net labels auto-orient away from the symbol** — a label anchored on a pin
  (via `"REF.PIN"` or a coordinate that hits a pin tip) with no explicit
  `orientation` is rotated so the text extends away from the body
  (`geometry.label_angle_away`; `model.Pin` now records the lib-frame pin
  orientation). Explicit `orientation` always wins.
- `readers.kicad_lib.body_extent_mil()` / `is_power_symbol()` — shared
  symbol-body extent (graphics, not pins) and power-marker detection, used by
  text autoplacement and the layout lint.

- **`akcli pins <lib_id>`** — op-list authoring helper that prints every pin's
  number, name, electrical type, and **world coordinate** for a
  `--at`/`--rotation`/`--mirror` placement, resolved from the same symbol sources
  the writer uses (`--symbols` / config `.kicad_sym`) and computed with the writer's
  own `geometry.pin_world`. Removes the guesswork of targeting pin coordinates when
  hand-authoring wires/labels/power ports. `--json` for machine output.
- **20 more calculators (56 total)**, all standards-cited: differential pairs
  (IPC-2141A over Hammerstad–Jensen/Cohn single-ended), `tracktemp` (IPC-2221 solved for ΔT),
  unit conversions (dBm/W/Vrms per IEEE Std 100; mil/mm exact 25.4; oz/µm copper nominal +
  pure-Cu physics), comparator hysteresis analysis+design (TI SLVA954, round-trip-tested),
  RS-485 fail-safe bias (TIA-485-A/SLLA070D), CAN split termination (ISO 11898-2), LDO
  dissipation/dropout/thermal, MOSFET gate drive (TI SLUA618A), current-sense shunts
  (TI SBOA170), Sallen–Key equal-component design (TI SLOA024B), ADC LSB/SNR/settling
  (MT-001), TVS selection (IEC 61000-4-5 surge), fuse derating on the IEC 60127 R10 ladder,
  NTC inrush sizing (TDK/EPCOS guide), L- and PI-section matching (Pozar §5.1/Bowick),
  flyback first-order design (Erickson ch. 6). **IPC-2152 deliberately not faked** —
  chart-based licensed data with no public closed form.
- **`akcli calc` tooling:** `calc batch <file|->` (JSON job list → envelope array, exit 1
  if any job fails), `--md` (markdown result table), and `--ops FILE` — design-type
  calculators emit a schema-valid `place_component` op-list with the computed E-series
  values filled in, validated against `ops.validate_oplist` in tests.
- **`tools/calc-view/`:** local web UI (stdlib server, localhost-only) for all 56
  calculators — grouped sidebar with filter, auto-generated forms, results with units and
  the formal reference, ~45 physical-style SVG illustrations (via/trace cross-sections,
  stackups, LM317/555/I²C/RS-485/CAN/flyback networks; the resistor color-code diagram
  colors its bands from the actual result), pinned/recent calculators, and shareable URLs
  (the hash carries calculator + inputs and re-runs on load).
- **`akcli calc` — 36 offline engineering calculators**, each stamped with its formal
  reference: E-series snap + 2–4-resistor combination search (IEC 60063:2015, tabulated
  E1–E24 + formula E48/E96/E192 with the 9.20 exception), dividers/LED/RC/LC
  (Horowitz & Hill 3rd ed.), LM317/FB regulator networks with exhaustive worst-case corner
  analysis (TI SLVS044Y), IPC-2221B §6.2 track width ↔ current and Table 6-1 clearance
  (incl. >500 V slopes), via R/thermal/ampacity/L/C/rise (Johnson & Graham 1993),
  Onderdonk/Preece fusing, ASTM B258 AWG, microstrip (Hammerstad–Jensen 1980), stripline
  (Cohn 1954 exact via AGM), coax/twin-lead (Pozar), PI/TEE/bridged-TEE attenuators,
  buck/boost stages (TI SLVA477B/372C), NE555 (SLFS022I), op-amp pairs (SLOD006B),
  I²C pull-up window (NXP UM10204 §7.1), crystal caps (ST AN2867), JESD51 thermal, battery
  life, resistor color/SMD/EIA-96 codes (IEC 60062:2016), galvanic pairs (MIL-STD-889C).
  Inputs take engineering notation (`4k7`, `100n`); `--json` returns
  `{calc, inputs, results, reference}`. Numerics cross-validated against KiCad's
  pcb_calculator outputs (independent clean reimplementation — no GPL code) and published
  handbook values in `tests/test_calc.py`. New **`design-calc` skill** teaches agents to
  compute-then-place E-series values instead of guessing.
- **Binary `.PcbDoc` copper decoded:** `Tracks6`/`Vias6`/`Arcs6`/`Pads6` are now parsed
  (new `readers/altium_pcb_bin.py` — packed little-endian records, coordinates in mils,
  native +Y-up frame) and land on the `Pcb` model as `tracks`/`vias`/`arcs`/`pads`;
  `--json` schema is now **1.1**. Layouts were cross-validated item-by-item against KiCad's
  own Altium importer (`pcbnew`) on real boards from KiCad's QA corpus: 778/778 board-level
  copper tracks, 20/20 vias, 236/236 arcs, 48/48 pads (names, sizes, drills, positions exact
  modulo ±3 nm importer rounding); a second board from a different AD version (3661 tracks /
  321 vias / 88 arcs / 468 pads) decodes with zero errors. Unknown record types inside a
  known section fail `ALTIUM_UNSUPPORTED`; truncated records fail `ALTIUM_MALFORMED` —
  nothing is silently skipped anymore. `Fills6`/`Regions6`/`Texts6`/`Polygons6` remain out
  of scope (still skipped, documented).
- **Hierarchical sheets (Altium reader):** a `.SchDoc` root recurses into sheet symbols
  (RECORD 15 + name/file 32/33), each instance in its own geometric namespace, with
  sheet-entry (RECORD 16: `Name`/`Side`/`DistanceFromTop`) ↔ child-PORT pairing per Altium's
  *Automatic* net-identifier scope — ports merge globally only in designs WITHOUT sheet symbols,
  so two children exposing the same port name stay separate; flat designs read exactly as
  before. `.PrjPcb` is accepted as input: akcli finds the top sheet (the one no sheet symbol
  references) and honors `PowerPortNamesTakePriority`. The previous RECORD-16 handling was dead
  code (it read `Text`/`Location` — real entries carry `Name`/`Side`/`DistanceFromTop`).
  Runtime-generated hierarchical fixtures; sheet-entry position scale follows the documented
  convention, real-AD validation flagged as pending.
- **Op-list authoring kit:** `docs/op-list-authoring.md` (envelope, coordinate contract, all
  16 ops with notes, pipeline, idempotency rules) plus `akcli ops list` (vocabulary + required
  fields + executor support) and `akcli ops template <op>` (fill-in JSON skeleton); the in-code
  tables are drift-guarded against `schemas/ops.schema.json` by tests.
- **Autoplace collision avoidance:** visible Reference/Value anchors register in a per-apply
  registry; a new label landing within one label extent of an existing one bumps outward
  deterministically (replays stay byte-identical). Fixes neighboring parts' texts stacking
  (the `+3V3`-on-`C2` case).
- **`check --format sarif|junit`:** SARIF 2.1.0 output for GitHub code scanning (stable
  `partialFingerprints`, schematic path as artifact URI, rule table) and JUnit XML for CI test
  reporters (WARNING+ findings as failed testcases; NOTE/INFO as passed cases with
  `system-out`; clean runs emit one passed case). Lint-style exit semantics unchanged.

### Fixed
- **Rotation transform now matches eeschema exactly** — a file angle of
  +90° rotates counter-clockwise on screen (`(x,y) → (y,−x)` in the
  +Y-down frame); akcli's writer and reader each implemented a different
  wrong order (the truth is rotate-by-minus-angle THEN mirror). Both now
  share one transform, locked by a 12-combo rotation/mirror truth table
  verified against kicad-cli's own netlister. **This corrects netlists of
  schematics with rotated polarized parts** (e.g. LEDs at 270° had
  anode/cathode swapped in the derived netlist); on the reference design
  the corrected netlist now matches kicad-cli's export exactly.
- **KiCad junction dialect** — eeschema does NOT connect a wire end
  touching another wire's mid-span without a junction node; the KiCad
  reader now matches (the Altium reader keeps Altium's bare-T-connects
  dialect). Same-sheet local-label ↔ global/power merging was verified
  against eeschema and kept: a local label DOES merge with a same-name
  global label or power port on the same sheet even when physically
  disconnected, and never across sheets.
- **Duplicate designators are no longer silently merged** — a re-placement
  of the same unit under an existing designator is kept as a distinct
  component (with a reader warning and an `akcli_duplicate` parameter), so
  `BOM_DUPLICATE_DESIGNATOR` now fires for KiCad inputs, matching how
  eeschema netlists such placements.
- **`check --nets` measures in exact integer nanometres** — off-grid and
  coincidence comparisons no longer accumulate float error; the grid is
  configurable (see `[project] grid`).
- **Wires ending on a bus entry no longer report `DANGLING_ENDPOINT`**
  (bus entries are now connection anchors; see `DANGLING_BUS_ENTRY`).
- **`akcli view` hardening** — the live dashboard rejects non-loopback
  `Host` headers (DNS-rebinding) and cross-origin POSTs (CSRF); a watcher
  crash now surfaces as a dashboard banner instead of silently stopping;
  step SVGs are cache-fingerprinted so a cleared timeline can never serve
  stale renders; `/live/bom` failures return structured errors; timeline
  steps are keyboard-accessible; `view <sch>` opens the browser on
  `/live` directly.
- **`set_component_parameters` no longer piles visible text on the symbol**
  — a NEW property node sits at the symbol anchor, so custom fields (LCSC,
  MPN, …) rendered as raw text over the body. The writer now creates every
  field except Reference/Value hidden, matching KiCad's own default.
- `schemas/schematic.schema.json` declared net `name` as `string`, but
  unnamed nets export `null` (`is_named: false`) — the schema now matches
  the long-standing export shape.

- **BOM checks no longer flag `#`-prefixed virtual parts** — power ports and
  PWR_FLAG have no value/footprint by design and never appear on a BOM;
  `#PWR01 has no footprint` warnings were pure noise.
- **netbuild: local labels now join same-name power/global nets on the same
  sheet** — KiCad merges a local `+3V3` label into the `+3V3` power net;
  akcli kept them as two nets, so label-on-pin connections to rails were
  invisible to `net`/`export`/`diff`/checks. Verified against kicad-cli
  netlist output.
- **sexpr: Unicode whitespace no longer crashes the tokenizer** — the bare-
  atom regex used `\s` (Unicode) while the scanner skips ASCII whitespace
  only; an NBSP between tokens raised a raw AttributeError instead of a
  structured error (found by the new fuzz suite).

- **Labels now carry the `(justify ...)` their angle needs.** KiCad never
  draws text upside-down: a global label at 180° WITHOUT `(justify right)`
  still renders its text toward +X — i.e. straight over the symbol it names.
  The writer now emits eeschema's exact angle/justify pairs
  ((0,left) (90,left) (180,right) (270,right); local labels add `bottom`).
- **Reference/Value of rotated instances render level and clear of the body.**
  Property text angle now counter-rotates the instance rotation (mod 180 — a
  180° property would render inverted), and autoplacement works from the pin
  box UNION the drawn body extent, so a rotated resistor's value no longer
  prints vertically through its own body, and a connector's value (pins all on
  one side) no longer lands inside the outline.
- **Any `(power)` symbol hides its Reference** — a `PWR_FLAG` placed as
  `FLG1` (no `#` prefix) printed its designator into the schematic. Power-port
  Value text is now placed past the side the body extends to (a +5V arrow's
  name above it, GND's below), matching eeschema.
- **`power:PWR_FLAG` no longer merges rails in net inference.** A `PWR_FLAG` power
  symbol is meant only to mark a net as driven for ERC; the KiCad reader was
  injecting a `"PWR_FLAG"` power-net name at its pin, so two flags (e.g. one on
  +5V, one on GND) unioned every rail they touched into a single net — a false
  +5V↔GND short in `akcli net`/`check` (KiCad ERC was unaffected). The reader now
  emits the flag's pin (keeping it electrically on its net, satisfying KiCad's
  `power_pin_not_driven`) but never names/merges a net from it.
- **`calc` output never SI-prefixes non-base units:** values already carrying a prefixed or
  compound unit (mm, °C/W, m², Ω/km) print plain — the clearance table rendered 0.2 mm as
  "200 mmm".

## [0.3.1] - 2026-07-07

### Added
- **`akcli expected` subcommand:** extract an expected pin→signal table from a Zephyr
  devicetree source/overlay (gpio phandles + Nordic `NRF_PSEL` pinctrl) or a markdown pinout
  table, as the JSON `pinmap --expected` consumes. Empty extraction exits `1` (a vacuous
  table must not read as success).
- **CLI-layer offline tests for `jlc add`** (flag validation, exit-code mapping, `--place`
  op-list emission) against the captured EasyEDA fixtures.
- **Altium fixture invariant sweep:** auto-discovering tests over every `.SchDoc` fixture —
  net members must reference real component pins, membership sorted/duplicate-free, reads
  deterministic, CSV/Protel exports agree with the inferred netlist, and the malformed corpus
  fails loudly. New fixtures are swept automatically. (The independent cross-check against
  Altium's own netlist export still requires a real AD install; KiCad's Altium importer is
  GUI-only and cannot be driven headless.)

## [0.3.0] - 2026-07-06

### Added
- **`tools/live-view/`:** a localhost dashboard that watches a `.kicad_sch` while akcli draws
  it — per-step SVG (inline, auto-cropped), KiCad ERC badges, part/net counts, notes, zoom/pan,
  follow-live; optional macOS auto-revert of an open KiCad editor. See its README.
- **New `jlcpcb-capabilities` skill:** manufacturing limits to design against, with
  **嘉立創 (jlc.com) as the primary source** — 1–64 layers, HDI blind/buried vias, 0.1 mm
  microvias, up to 6 oz copper, FPC and 經濟/標準 SMT gates — plus a difference table against
  JLCPCB-international (32 layers, no blind/buried, BGA ≥0.35 mm, ...) and the intl stencil
  specs. Sources + snapshot date stated in the skill; includes apply-while-drawing guidance
  (comfortable defaults vs 極限值, schematic-time package gating, AD/PADS export gotchas).
- **Converted libraries import into Altium Designer natively:** the footprint writer now
  emits the **KiCad 6 dialect** (`(layer)(width)` graphics, version `20211014`) — readable by
  every KiCad from 6 to 10 *and* by Altium Designer's built-in **Import Wizard » KiCad Design
  Files** (whose KiCad support is pinned to 6.0x), which converts the produced
  `.kicad_sym`/`.kicad_mod` to a native `.SchLib`/`.PcbLib`. This replaces the dead
  npnp `--to altium` path with a vendor-supported one.
- **`akcli jlc add` is back — in-process, zero-install:** LCSC → KiCad symbol/footprint/3D
  conversion now runs inside akcli via the vendored MIT conversion core of
  **JLC2KiCadLib** (TousstNicolas; license + provenance in
  `src/akcli/_vendor/jlc2kicadlib/` and `THIRD_PARTY_NOTICES.md`). Upstream's two
  dependencies are deliberately not vendored: `requests` is replaced by a stdlib shim and the
  GPLv3 `KicadModTree` by a clean-room `.kicad_mod` writer that emits the modern
  `(footprint ...)` dialect. `--place` emits a `place_component` op-list as before; no external
  binary and no pip dependency required.

### Removed
- **`akcli jlc add` (external library conversion):** the upstream `nlbn`/`npnp` converter
  repositories are no longer available, so the delegation, the pinned auto-downloader, and the
  `--place` op-list emission were removed. `jlc search`/`jlc show` (and `--easyeda` metadata)
  are unchanged. Symbols/footprints now come from the official KiCad libraries or project
  `.kicad_sym` files.

## [0.2.0] - 2026-07-06

Not yet published to PyPI; install from source (see `INSTALL.md`).

### Added
- **Readers:** Altium `.SchDoc` / `.SchLib` and ASCII `.PcbDoc`; KiCad `.kicad_sch` (v7/v8) via a
  bounded, non-recursive S-expression parser with pin-type resolution from `lib_symbols`.
- **Net inference** (`netbuild`) shared across both formats: same-name merge, junctions/T-junctions,
  No-ERC handling.
- **CLI:** `read`, `net`, `component`, `check` (ERC/power/BOM), `diff` (net-membership), `pinmap`,
  `export`.
- **KiCad write/draw:** `plan` / `draw` from a versioned JSON op-list — atomic, idempotent (UUIDv5),
  connectivity-verified.
- **JLCPCB/LCSC parts:** `jlc search` / `show` / `add` (conversion via external `nlbn` / `npnp`).
- **Claude Code plugin:** circuit-design skill + `circuit-review` / `circuit-pinmap` / `circuit-draw` /
  `circuit-diff` commands; DTS / pinout adapters.
- **Altium live driver (preview):** Python file-based JSON bridge; the Windows DelphiScript half is a
  scaffold pending validation.
- Documentation (`README.md`, `INSTALL.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, `docs/SPEC.md`,
  `docs/cli-reference.md`), reference config, and CI matrix.

- **`BOM_CORRUPT_TEXT` check (NOTE):** components whose value/parameters contain the U+FFFD
  replacement character are surfaced with an aggregated finding instead of silently printing `�`.
  Root-cause analysis on real-world files showed the corruption is baked into the `.SchDoc` at
  export time (a legacy-code-page value pushed through a lossy UTF-8 decode by the authoring tool --
  both the ANSI field and its `%UTF8%` twin carry the damage), so no decoder can recover it; the
  finding says so and points at re-export.

### Added
- **Hierarchical sheets (KiCad reader):** `read`/`net`/`check`/`diff`/`pinmap` on a root
  `.kicad_sch` now recurse into `(sheet ...)` children (paths relative to the parent file,
  cycle- and depth-guarded). Every sheet INSTANCE is its own geometric namespace — a file
  instantiated twice contributes its components once per instance with designators resolved
  from the matching `(instances (path ...))` entry — and connectivity crosses sheets only
  through sheet-pin↔hierarchical-label pairs (strictly parent↔child, never global), global
  labels, and power ports. The writer stays flat-only v1.
- **`delete_component` / `delete_object` / `move_component` ops:** delete removes all placed
  instances of a designator (or one object by uuid) — attached wires are left for the
  connectivity gate to flag, so stale wiring is cleaned up explicitly, and deleting an
  already-absent target is a replay-safe no-op; move repositions one instance (designator +
  optional unit) with its properties travelling along, wires intentionally not stretched.
- **Property autoplace:** placed symbols now get eeschema-style field layout — Reference/Value
  beside a tall (vertical-pin) body or above/below a wide one, `Footprint`/`Datasheet`/
  `Description` created hidden, power symbols with hidden `#PWR` references and the value
  below the anchor. Previously every field rendered at the component origin (the synthesized
  Reference even at absolute 0,0), stacking raw text over the body.
- **Multi-unit placement:** `place_component` takes an optional `"unit": N` — each unit is
  its own placed instance sharing the designator (74xx gate A/B/...). `"REF.PIN"` endpoints
  resolve against the instance whose unit owns the pin; wiring a pin on an **unplaced** unit
  fails loudly with the unit to place, instead of silently snapping to another unit's body.

### Fixed
- **Placed instances expose only their own unit's pins** (reader, writer, verifier): every
  unit of a multi-unit symbol shares local pin geometry, so treating all units' pins as
  present at one instance mapped all four 74xx gates onto one body — `akcli net` merged
  unrelated gate pins into one net while eeschema saw two, and phantom pin points masked
  real dangling wires in the connectivity gate. Instances of one designator now merge into
  a single component on read (no false `BOM_DUPLICATE_DESIGNATOR`).
- **Multi-line/control text is escaped KiCad-style:** `_q` escaped only `\` and `"`, so an
  `add_text` with a newline wrote a file KiCad refused to parse while every akcli gate
  passed (akcli's lexer tolerates a raw newline in a quoted atom; eeschema does not).
  `\n`/`\r`/`\t` are now escaped in all writer quoting helpers.
- **Pin taps now follow eeschema connectivity:** a pin tip touching a wire's **mid-span**
  connects only when a junction marks that point (or at a segment endpoint) — both in net
  inference (`netbuild`) and, constructively, in the writer: `auto_junctions` now also
  considers pins lying on a segment interior, so a placed part tapping a rail gets its
  junction automatically (previously the mid-span-pin rule never fired because candidates
  were wire endpoints only, and `akcli net` claimed connectivity KiCad rejected).
- **Replaying an op-list is byte-identical after ONE apply:** idempotent replay now replaces
  same-uuid nodes **in place** instead of remove-then-append, which migrated every op node to
  the end of the file while auto-junctions stayed put — the first re-apply reordered the
  document and byte-idempotency only converged on the second apply.
- **Large op-lists are no longer quadratic:** each placement re-parsed the whole (growing)
  inline `lib_symbols` cache to resolve its symbol; symbols now resolve once, from just their
  own cached body, memoized per apply run. A 478-placement sheet went from >120 s (timeout)
  to 1.7 s.
- **Duplicate pin numbers across units no longer collide:** multi-unit parts with shared pads
  (e.g. dual DirectFETs — unit A pins 1,2,3 / unit B pins 1,4,5) legitimately repeat a pin
  number, but the writer seeded every per-pin UUID with just `designator.pin<N>`, so the two
  `(pin "1" ...)` nodes got the same UUID and the connectivity gate refused the write
  (`DUPLICATE_UUID`). Later occurrences now carry a `#k` suffix in the seed; first occurrences
  keep the historical seed, so existing schematics replay byte-identically. Found by the
  library-wide sweep (`Transistor_FET:IRL6297SD` was the one failure in 478).
- **Alternate (DeMorgan) body styles no longer duplicate every pin:** the KiCad library
  reader collected pins from every `Name_<unit>_<style>` sub-symbol, including the `_<unit>_2`
  DeMorgan re-drawing of the same physical unit — so a 74xx-style symbol resolved with each
  gate pin twice, the writer emitted colliding per-pin UUIDs, and the connectivity gate refused
  the placement (`DUPLICATE_UUID`, exit 6). Only body style 1 is collected now, and each pin
  records its owning unit in `owner_part_id` (`_0_*` common sub-symbols map to unit 1). Found by
  a library-wide sweep placing every derived symbol in KiCad's official 74xx library.
- **`(extends)`-derived symbols are now FLATTENED into the written `lib_symbols` cache**
  (KiCad-save style): the base's units/pins/graphics are inlined under the derived name (unit
  sub-symbols renamed `Base_u_s` → `Derived_u_s`), the derived symbol's own properties/settings
  overlaid, and the `extends` clause dropped — no base is cached separately. Previously the cache
  kept a bare `(extends "Base")` next to a library-qualified `Nick:Base` entry, which KiCad's
  loader does **not** resolve: eeschema reported `lib_symbol_mismatch`, the derived part lost all
  its pins, every wire to it dangled (`unconnected_wire_endpoint`), and KiCad's netlist omitted
  the part entirely — while akcli's own verifier and netlist looked clean. Found by running a
  drawn AMS1117-3.3 LDO block through KiCad 10's own ERC; regression-tested in
  `test_e2e_draw.py` (cache shape everywhere; pins-on-net via real `kicad-cli` in the KiCad CI job).
- **`kicad-cli` advisory runs work on KiCad 10 again:** KiCad 10's argument parser rejects the
  `--` end-of-options separator (`Unknown argument: --`), so every advisory ERC/netlist run was
  silently degrading to `report: null` (exit 1). Paths are now passed absolute instead of behind
  `--`, keeping the option-injection guard (an absolute path cannot start with `-`).
- **CFBF DIFAT spillover (> 109 FAT sectors) is now walked**, not refused: the spillover chain is
  read under the header-declared count, a cycle set, and the global sector cap (hostile input still
  fails with `ALTIUM_FAT_CYCLE` / `ALTIUM_ALLOC_GUARD` / `ALTIUM_MALFORMED`). Large real-world
  `.PcbDoc` containers now open.
- **`BOM_MISSING_VALUE` no longer fires on vendor-library parts** whose value lives in the part
  identity: a part-number parameter (`Manufacturer Part`, `LCSC Part Name`, ...) or a digit-bearing
  `library_ref` (e.g. `AO2301`) now substitutes for a blank `Comment`/`Value`. Generic symbols with
  no identity still report.
- **`ERC_NO_POWER` / `ERC_NO_GROUND` skip `U`-prefixed parts with fewer than 3 pins** (2-pin
  headers/jumper stubs designated `U*` are not ICs).
- **`pinmap` without a configured MCU** now says how to fix it (`--mcu <REF>` or
  `[project].mcu_designator`) instead of a bare warning.
- **Footprints** now resolve via the model-link chain (RECORD-45 model → RECORD-44 implementation →
  RECORD-1 component): the owner keying was wrong, so the model-link footprint was never found; the
  RECORD-41 `Footprint` / `Supplier Footprint` parameter is the fallback. Removes false
  `BOM_MISSING_FOOTPRINT` (80/80 components resolved on the reference board).
- **Rail voltage inference** no longer mis-fires on underscore-suffixed rails (`V3V3_AUX`, `V3V3_IO`):
  the trailing word-boundary that `_` defeated is replaced; logic is now shared in `checks/_rails.py`,
  and configured `[[rail]]` names match `<rail>_suffix` too. Fixes false `ERC_NO_POWER`.
- **`export --json`** now errors (exit 2) with guidance instead of emitting non-JSON at exit 0.
- **`.SchLib` / `.PcbDoc`** binary records now surface as `ALTIUM_UNSUPPORTED` (exit 5, *unsupported*)
  instead of `ALTIUM_MALFORMED` (exit 3, *parse error*).
- **`pinmap --expected`** unmatched pins are now `WARNING` (non-zero exit) instead of a silent NOTE.
- **`-C/--config`** (and other global flags) are accepted before *or* after the subcommand.
- `schema_version` now stamped on every machine-readable command: `check` / `diff` / `pinmap` reports,
  plus `read` and `component --json` (`net` stays a bare array, as documented).
- **`draw --apply`** writes a `<target>.bak` next to the file (the doc'd backup was never wired up).
- `tarfile` extraction uses `filter="data"` (Python 3.14-ready, hardened).

### Known limitations
- A value whose Ω/µ/± was already written as the U+FFFD replacement bytes (`EF BF BD`) by an upstream
  tool on a non-UTF-8 locale is corrupted **at export** and cannot be recovered on read by any codec.
- `draw` snaps off-grid / non-orthogonal geometry rather than rejecting it.
- The Windows Altium *live driver* (DelphiScript half) needs a Windows + Altium 22+ box to validate.

### Notes
- Baselines at the first tagged release: package `0.1.0`, `schema_version = "1.0"`,
  `protocol_version = 1`.

[0.7.0]: https://github.com/tipoLi5890/akcli/commits/main
