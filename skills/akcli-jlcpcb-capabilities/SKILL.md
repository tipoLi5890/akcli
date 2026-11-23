---
name: akcli-jlcpcb-capabilities
description: >-
  Consult 嘉立創/JLC (jlc.com) manufacturing capabilities — the PRIMARY reference —
  while designing a board: rigid PCB (1-64 layers, HDI blind/buried vias, 0.1 mm
  microvias, up to 6 oz copper), FPC, and SMT assembly limits with exact numbers,
  plus a JLCPCB-international (jlcpcb.com) difference table and stencil specs. Use
  this skill whenever the task involves: choosing trace width/spacing, via or drill
  sizes, board size/thickness, copper weight, solder mask or silkscreen limits;
  checking whether a package (0201/0402, BGA pitch, fine-pitch IC) is assemblable;
  Economic vs Standard SMT; panelization (V-cut, 郵票孔, process borders, Mark
  points); flex stackups, coverlay, stiffeners; stencil ordering; preparing /
  exporting Gerber+drill files for a JLC order (出圖, RS-274-X, layer mapping,
  plated vs non-plated holes, silkscreen-on-pads, the common DFM mistakes JLC
  flags); or the manufacturing handoff itself — which files a KiCad export
  (headless `kicad-cli` or the Fabrication Toolkit plugin) actually produces,
  which of them a bare-PCB order needs vs. an SMT/PCBA order, the jlc.com vs
  jlcpcb.com order-sequencing difference, and how LCSC part-number matching
  behaves on upload. Triggers on:
  嘉立創, JLC, JLCPCB, 工藝能力, 製程能力, DFM, design rules, min trace, 線寬線距,
  annular ring, 孔環, via, 過孔, 盲埋孔, HDI, 微孔, drill, 鑽孔, board thickness,
  板厚, copper weight, 銅厚, solder mask, 阻焊, silkscreen, 字符, castellated, 半孔,
  panelization, 拼板, V割, SMT, PCBA, BGA pitch, 0402, 0201, stencil, 鋼網, coverlay,
  補強, flex, FPC, 出圖, Gerber, RS-274-X, plated, NPTH, thermal relief, DFM mistakes,
  Fabrication Toolkit, gerber.zip, bom.csv, positions.csv, designators.csv,
  netlist.ipc, 貼片, 貼片下單, 下單流程, BOM上傳, CPL, centroid, pick and place,
  自助匹配選型, 嘉立創幫我選型, PCB Assembly, 出貨, order flow.
---

# akcli-jlcpcb-capabilities — 嘉立創/JLC manufacturer limits to design against

嘉立創/JLC is the **primary manufacturing reference** while designing a board.
This file holds the **judgment** (what to decide at schematic/layout time) and
the **handoff procedure**; the exhaustive capability numbers live in
`references/`, loaded on demand (see the index at the bottom).

**Sources**: 工藝能力頁 <https://www.jlc.com/portal/vtechnology.html> +
出圖/DFM 指導 <https://www.jlc.com/portal/server_guide_112.html> — extracted
2026-07-06, re-verified 2026-07-15. All numbers are a **point-in-time
snapshot**: for anything order-critical or borderline, confirm against the live
page and the order-time DFM/audit result.

## First decision: which service line?

**嘉立創 (jlc.com, domestic CN) and JLCPCB (jlcpcb.com, international) are the
same company but DIFFERENT service lines with different limits** — pick the one
the user will actually order from. The differences that change a design:

| Item | 嘉立創 jlc.com | JLCPCB intl |
|---|---|---|
| Max layers | **64** | 32 |
| Blind/buried vias (HDI) | **supported (4–32 L)** | not supported |
| Microvia | **0.1 mm** | 0.15 mm min |
| Outer copper (2-layer) | up to **6 oz** | up to 4.5 oz |
| Thickness | 0.4–**4.8 mm** | 0.4–4.5 mm |
| Standard-SMT min BGA pitch | **0.3 mm** | 0.35 mm |

Full delta in `references/jlcpcb-international.md`. **HDI / blind-buried vias
exist ONLY on 嘉立創 — if the design needs them and the user might order
internationally, that is a blocker to surface early.**

## How to apply this while drawing

- **At schematic time (akcli's territory), assembly limits bite first**: pick
  packages the SMT line can place — Economic 經濟型 ≥0402 / IC pitch ≥0.4 mm /
  BGA ≥0.5 mm; Standard 標準型 ≥0201 / 0.35 mm / 0.3 mm (adds a required 5 mm
  process border). Prefer Basic/Preferred parts (`akcli jlc search`,
  akcli-parts-sourcing skill). Full table: `references/assembly-and-stencil.md`.
- **Comfortable defaults vs absolute limits**: an ordinary 1 oz 2–4 layer board
  at ≥0.127 mm (5 mil) trace/space and 0.3/0.6 mm vias sits far inside every
  limit at zero cost. Quote the limit values (0.09 mm trace, 0.15/0.25 mm via,
  0.1 mm microvia) only for dense BGA fan-out, and label them as limits.
- **Copper weight rewrites the rules**: 2 oz → 0.16/0.16 mm trace/space and
  0.20 mm mask dam; 6 oz → 0.45/0.45 mm. Re-check every clearance for heavy
  copper. Full trace/space + via tables: `references/rigid-pcb.md`.
- **Black/white mask is coarser** (0.13 vs 0.10 mm dam @1 oz) — prefer green for
  dense fine-pitch.

## Manufacturing handoff — the four order artifacts from KiCad

A JLCPCB PCBA order needs Gerbers + drill + BOM + CPL. The BOM comes straight
from akcli; the rest from `kicad-cli` (headless, agent-runnable — flags
verified on KiCad 10; DRC gate first, always):

> **Refill zones before DRC — every time.** Zone fills are stored in the
> `.kicad_pcb` as saved copper, and pcbnew does **not** refill them
> automatically after routing edits. Running DRC against a stale fill
> produces dozens of ghost clearance/unconnected violations (a real board hit
> 76) — or worse, hides real ones. In the GUI press `B` (Edit -> Fill All
> Zones) and save before exporting; headless `kicad-cli pcb drc` refills
> zones itself by default (do NOT pass a refill-skipping option before a
> gate), but the *saved* file still needs a GUI refill+save before Gerber
> export, or the copper in the Gerbers is the stale fill.

```bash
kicad-cli pcb drc board.kicad_pcb --exit-code-violations
kicad-cli pcb export gerbers board.kicad_pcb -o fab/ \
  --layers F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts \
  --subtract-soldermask                # multilayer: add In1.Cu,In2.Cu,...
kicad-cli pcb export drill board.kicad_pcb -o fab/ --format excellon \
  --drill-origin absolute --excellon-units mm --excellon-zeros-format decimal --generate-map
kicad-cli pcb export pos board.kicad_pcb -o fab/cpl.csv --format csv --units mm \
  --side both --exclude-dnp
akcli jlc bom board.kicad_sch --qty 10 --csv fab/bom.csv   # JLCPCB header, LCSC ids
akcli fab profiles                                         # bundled profiles: id, sources, retrieval date
akcli fab check board.kicad_pcb --profile jlc-4l-1oz-free-vias-2026-07 --order order.toml
akcli fab explain FAB_VIA_IN_PAD --profile jlc-4l-1oz-free-vias-2026-07
akcli release preflight --sch board.kicad_sch --pcb board.kicad_pcb --fab-profile jlc-4l-1oz-free-vias-2026-07 --drc --gerbers fab/ --out release.manifest.json
```

`--profile` takes a profile **file** or the **id of a profile bundled with akcli**
(`akcli fab profiles`; a file at that path always wins). The JLC 4-layer 1 oz
free-via baseline ships bundled as `jlc-4l-1oz-free-vias-2026-07`; revision
`jlc-4l-1oz-free-vias-2026-07-r2` is the same snapshot plus the slot-width floor. A bundled profile
is a dated snapshot, not a live feed: once its `retrieved_at` is more than 12 months
old `fab check` adds a `FAB_PROFILE_STALE` note — re-verify the numbers against the
live JLCPCB pages (this skill's reference files say where), or cut your own profile
file from `examples/fab/jlc-4l-1oz.toml` and pass that.

A fab profile (`--profile`) is a versioned vendor-capability snapshot — `[source]`
URLs plus a `retrieved_at` date, see `examples/fab/jlc-4l-1oz.toml` — that turns this
skill's capability numbers into a machine-checkable gate: `fab check` runs the
board against it (and, with `--order`, against the declared purchase intent) and
classifies findings by severity — direct rule violations are errors, cost-driving
thresholds are warnings, boundary-exact values are notes. `fab explain <CODE>` prints
the rule, the fix direction, and the evidence behind a specific finding code. Board
area is the true `Edge.Cuts` outline (arcs exact, cutouts subtracted; a bounding-box
fallback is always labelled as one), the tenting cap binds only vias the board marks
tented — or all of them when `--order` declares `via_covering = "tented"` — and the
project's own DRC minimums (`.kicad_pro`) are compared with the profile's. For the
free-vs-paid **via covering** model, the surcharge triggers, the capability→profile-field
mapping, and **exactly what `fab check` gates vs what stays advisory** (it is a
fab-policy/cost gate, NOT a geometric DRC — pair it with KiCad's DRC: `kicad-cli pcb
drc` by hand, or `akcli release preflight --drc` to make it a release gate recorded
in the manifest), see `references/cost-and-fab-profile.md`.

CPL header rename before upload: `Ref→Designator, PosX→Mid X, PosY→Mid Y,
Rot→Rotation, Side→Layer` (or use JLCPCB's Fabrication Toolkit KiCad plugin /
the order page's column mapper). **Always check the placement preview for
polarized-part rotation** — it is the classic PCBA defect. GUI walkthroughs:
JLCPCB's own KiCad 8 guides
([gerber/drill](https://jlcpcb.com/hk/help/article/generate-gerber-and-drill-files-in-kicad-8),
[BOM/centroid](https://jlcpcb.com/hk/help/article/generate-bom-and-centroid-files-from-kicad-8));
full detail in `docs/jlc.md`.

**Fabrication Toolkit's five default files**: `gerber.zip`, `bom.csv`,
`positions.csv` (already JLCPCB-shaped, no header rename), plus
`designators.csv`/`netlist.ipc` (traceability only — not uploaded). A bare-PCB
order needs `gerber.zip` alone. **Order sequencing differs by line**: on
嘉立創 (jlc.com) SMT is a *second* order placed against an already-submitted
PCB order; on JLCPCB.com international, PCB + Assembly are *one* combined
order (toggle Assembly on before paying for the PCB). Either way, a component
with a resolved `LCSC` C-number auto-matches in the SMT BOM step; an
unmatched line falls to manual picking (jlc.com also offers a paid ¥50
JLC-assisted match). Full detail in `docs/jlc.md`.

## DFM & 出圖 — the mistakes that scrap a first batch

Top rules an agent must get right (full file-prep guide + the nine 常見錯誤:
`references/dfm-and-file-prep.md`):

- **RS-274-X Gerber only** (JLC 域内 rejects Gerber X2 / RS-274-D); KiCad's
  default output is RS-274-X.
- **Outline + slots on `Edge.Cuts` as real geometry**; **mask windows come from
  `*.Mask`, not `*.Paste`** (paste = stencil only).
- **No silkscreen on a pad** (JLC deletes it); never hand-mirror bottom text —
  KiCad already mirrors it.
- **Plated vs non-plated is load-bearing** — a mis-marked hole is a silent open /
  an unsolderable pin. Verify PTH/NPTH before export.
- **Declare special intent** — controlled impedance and a multi-design panel's
  design count are NOT in the files; put them in `order.toml` and gate with
  `akcli fab check --order`.
- **First batch is customer-verified — no refund on artwork errors** — run
  `kicad-cli pcb drc` + `akcli fab check` (both at once, with a manifest:
  `akcli release preflight --drc --fab-profile …`) and review the
  placement/mask preview before ordering.

## Reference files (load only the one you need)

| File | Read it for |
|---|---|
| `references/rigid-pcb.md` | 嘉立創 rigid FR-4/HDI full numbers: trace/space by copper, vias/drills/HDI/backdrill/via-fill, annular ring, slots, mask/silk, edges/panel, finishes |
| `references/flex-fpc.md` | 嘉立創 FPC: stackups, trace/space, vias, coverlay, stiffeners, panelization |
| `references/assembly-and-stencil.md` | SMT Economic vs Standard placement limits, **design-for-assembly (edge clearance, polarity marking, tombstoning, DNP)**, laser-stencil specs |
| `references/cost-and-fab-profile.md` | **via covering (free/paid) & the surcharge triggers; how to encode the numbers as an `akcli fab` profile; what `fab check` gates vs advisory** |
| `references/dfm-and-file-prep.md` | out-of-file rules: Gerber/drill format, outline/slot/mask/silk layer mapping, plated/non-plated, the nine common mistakes, EDA-tool export notes |
| `references/jlcpcb-international.md` | jlcpcb.com international-line differences (tighter limits) + international stencil |

## Caveats (repeat when advising)

- Snapshot **2026-07-06** (capabilities) / **2026-07-15** (DFM + re-verify); the
  live 工藝能力 page and order-time audit are authoritative. 嘉立創 revises
  frequently (e.g. the 2025-06 LDI mask-1:1 change).
- Tables mix **建議值 (recommended)** and **極限值 (absolute limits)** — when
  quoting a limit, say it is a limit, not a target.
- Special processes (HDI, microvia, heavy copper, backdrill, via fill, 6+
  layers, high-frequency, impedance) change price class; 沉金 (ENIG) is often
  mandatory for the extreme values.
- These are layout/fab constraints; akcli's checks are schematic-level. Gate
  component choice at schematic time; for hard, CI-grade gating on an actual
  board file use `akcli fab check --profile` (versioned and sourced) rather than
  eyeballing the tables.
