---
name: jlcpcb-capabilities
description: >-
  Consult 嘉立創/JLC (jlc.com) manufacturing capabilities — the PRIMARY reference —
  while designing a board: rigid PCB (1-64 layers, HDI blind/buried vias, 0.1 mm
  microvias, up to 6 oz copper), FPC, and SMT assembly limits with exact numbers,
  plus a JLCPCB-international (jlcpcb.com) difference table and stencil specs. Use
  this skill whenever the task involves: choosing trace width/spacing, via or drill
  sizes, board size/thickness, copper weight, solder mask or silkscreen limits;
  checking whether a package (0201/0402, BGA pitch, fine-pitch IC) is assemblable;
  Economic vs Standard SMT; panelization (V-cut, 郵票孔, process borders, Mark
  points); flex stackups, coverlay, stiffeners; or stencil ordering. Triggers on:
  嘉立創, JLC, JLCPCB, 工藝能力, 製程能力, DFM, design rules, min trace, 線寬線距,
  annular ring, 孔環, via, 過孔, 盲埋孔, HDI, 微孔, drill, 鑽孔, board thickness,
  板厚, copper weight, 銅厚, solder mask, 阻焊, silkscreen, 字符, castellated, 半孔,
  panelization, 拼板, V割, SMT, PCBA, BGA pitch, 0402, 0201, stencil, 鋼網, coverlay,
  補強, flex, FPC.
---

# jlcpcb-capabilities — 嘉立創/JLC manufacturer limits to design against

**Primary source (以此為主)**: 嘉立創工藝能力頁
<https://www.jlc.com/portal/vtechnology.html> — extracted **2026-07-06**.
Secondary: JLCPCB international pages (differences + stencil, same date; URLs in
that section). All numbers are a **point-in-time snapshot**: for anything
order-critical or borderline, confirm against the live page and the order-time
DFM/audit result.

**嘉立創 (jlc.com, domestic CN) vs JLCPCB (jlcpcb.com, international) are the
same company but DIFFERENT service lines with different limits** — pick the
table matching where the user will actually order. Headline differences:

| Item | 嘉立創 jlc.com | JLCPCB intl |
|---|---|---|
| Max layers | **64** | 32 |
| Blind/buried vias (HDI) | **supported (4–32 layer HDI)** | not supported |
| Microvia | **0.1 mm** (2–12 L, board ≤1 mm, 沉金 only) | 0.15 mm min |
| Outer copper (2-layer) | up to **6 oz** | up to 4.5 oz |
| Thickness | 0.4–**4.8** mm | 0.4–4.5 mm |
| Multilayer PTH annular | 0.20 rec / **0.15 limit** | 0.25 rec / 0.18 limit |
| Standard-SMT min BGA pitch | **0.3 mm** | 0.35 mm |
| Press-fit holes | 0.55–**2.0** mm | 0.55–1.025 mm |

## How to apply this while drawing

- **At schematic time (akcli's territory), assembly limits bite first**: pick
  packages the SMT line can place — Economic 經濟型: ≥0402, IC pitch ≥0.4 mm,
  BGA ≥0.5 mm; Standard 標準型: ≥0201, IC pitch ≥0.35 mm, BGA ≥0.3 mm. Prefer
  Basic/Preferred parts (`akcli jlc search`, parts-sourcing skill).
- **Comfortable defaults vs absolute limits**: for an ordinary 1 oz 2–4 layer
  board design at ≥0.127 mm (5 mil) trace/space and via 0.3/0.6 mm — far from
  every limit at zero cost. Quote limit values (0.09 mm trace, 0.15/0.25 mm
  via, 0.1 mm microvia) only for dense BGA fanout, and label them as limits.
- **Copper weight rewrites the rules**: 2 oz → 0.16/0.16 mm trace/space and
  0.20 mm mask dam; 6 oz → 0.45/0.45 mm. Re-check every clearance when the
  user asks for heavy copper.
- **HDI/盲埋孔 exists only on 嘉立創**: if the design needs blind/buried vias
  and the user orders internationally, that is a blocker to surface early.
- **Black/white mask is coarser** (0.13 vs 0.10 mm dam @1 oz).
- **Exporting from this repo's world**: the page's own tool notes — Altium:
  slot/keepout layers must match the outline (uncheck Keepout on export);
  PADS: use Hatch copper mode, outline for non-plated slots.

## 嘉立創 rigid PCB (FR-4 unless noted)

### Layers, size, thickness

| Item | Capability |
|---|---|
| Layers | **1–64** |
| Max size | 1L 606×510; 2L 670×600; 4L 663×593; 6–64L 656×586 mm |
| Min size | 3×3 mm (FR4/high-freq); 5×5 mm (Al/Cu core) |
| Thickness | 0.4–4.8 mm; tol ±10 % (≥1.0 mm) / ±0.1 mm (<1.0 mm) |
| Materials | FR-4 Grade A, HDI, high-frequency, aluminum, copper core |

### Trace / space (min, by finished copper)

| Copper | 單/雙面 | 多層 |
|---|---|---|
| 1 oz | 0.10/0.10 mm (4 mil) | 0.09/0.09 mm (3.5 mil) |
| 2 oz | 0.16/0.16 | 0.15/0.15 |
| 2.5 oz | 0.20/0.20 | — |
| 3.5 oz | 0.25/0.25 | — |
| 4.5 oz | 0.30/0.30 | — |
| 5 oz | 0.35/0.35 | — |
| 6 oz | 0.45/0.45 | — |

Outer 1–6 oz (double) / 1–2 oz (multi); inner 0.5/1/2 oz. Width tol ±20 %.

### Drills, vias, HDI

| Item | Capability |
|---|---|
| Drill | 0.15–6.3 mm (2+ L); 0.3 mm 1L; Al ≥0.65; Cu core ≥1.0 |
| **Microvia 微孔** | **0.1 mm** (2–12 L, board ≤1 mm, 沉金 only; pad ≤0.2 mm) |
| Via (min) | 0.15 mm hole / 0.25 mm pad (pad ≥ hole+0.1, rec +0.15); via↔via edge 0.2 mm |
| **Blind/buried 盲埋孔** | **HDI 4–32 layers supported**; buried via resin/copper-paste fill + cap |
| Annular PTH | 雙面 ≥0.25 rec (0.18 limit); **多層 ≥0.20 rec (0.15 limit)**; NPTH ≥0.45 |
| Hole tol | 插件孔 +0.13/−0.08 mm; 壓接孔 ±0.05 (0.55–2.0 mm, 多層沉金) |
| Plated slot | 雙面 ≥0.5 / 多層 ≥0.35 mm wide; length ≥2× width (min 1.0/0.7 mm) |
| Non-plated slot | ≥1.0 mm |
| Half-hole 半孔 | ≥0.5 mm dia, ≥1 mm to edge, ≥0.5 mm pitch; board ≥10×10, ≥0.6 mm thick |
| Backdrill 背鑽 | 4–64 L, board ≥0.8 mm; hole 0.2–0.5 mm (resin filled), backdrill = hole+0.2; dielectric ≥0.15 |
| Blind slot 盲槽 | width ≥1.0, depth ≥0.2, ring ≥0.3 (PTH)/0.2 (NPTH); board ≥0.8 mm |
| Via fill | 油墨塞孔 0.15–0.5; 樹脂塞孔+電鍍蓋帽 0.15–0.55 (6+ L default); 銅漿塞孔+蓋帽 0.15–0.55 (thermal) |
| Pad↔track | ≥0.1 mm @1 oz (BGA 0.09); pad-hole↔pad-hole ≥0.45 mm |
| BGA pad | ≥0.2 mm; ↔track ≥0.1 (0.09 multi) |

### Solder mask 阻焊 / silkscreen 字符

| Item | Capability |
|---|---|
| Colors | 綠/紫/紅/黃/藍/白/黑 (感光油墨), thickness ≥10 µm |
| Opening | **1:1 with pad since 2025-06** (design ≥0.02 mm, engineering adjusts) |
| Mask dam 阻焊橋 | 1 oz: ≥0.10 mm (綠紅黃藍紫) / **≥0.13 mm (黑白)**; 2 oz: ≥0.20 mm |
| Mask↔trace | ≥0.09 mm |
| Silkscreen | height ≥1 mm (CJK may need more), stroke ≥0.15 mm, ≥0.15 mm off copper |

### Edges & panelization 成型/拼板

| Item | Capability |
|---|---|
| Routed 鑼邊 | copper↔edge ≥0.2 mm; tol ±0.2 (precision ±0.1, board ≥50×50 + 3 tooling holes ≥1.5 mm) |
| Al/Cu-core slot | ≥1.6 mm wide |
| V割 | copper↔edge ≥0.4 mm; tol ±0.4 (board ≥0.6 mm); panel 70–475 mm; cut pitch ≥3 mm (extreme 2); default 0 mm gap |
| 郵票孔 | gap 1.6–2 mm; process border ≥3 mm (**JLC SMT: 5 mm + 1 mm Mark 光點**) |
| Impedance | multilayer, ±10 % (fee); calculator: 阻抗計算神器 |
| Finishes | 有鉛/無鉛噴錫, 沉金, OSP (limits: Al = 噴錫 only; ≤0.4 mm/高頻/銅基/FPC no 噴錫; Al/Cu/FPC no 有鉛) |

## 嘉立創 FPC

| Item | Capability |
|---|---|
| Layers | 1/2/4 (no rigid-flex yet); electrolytic or rolled copper |
| Stackups | PI 25 µm std; PI 50 µm 超厚; PET 36 µm 透明 |
| Size | ≤234×490 mm (extreme 250×600 with border); panelize <20×20 |
| Thickness | 1L 0.07/0.11-0.12; 2L 0.11–0.24; 4L per stackup; 補強 tol ±0.05 (≤0.3)/±0.1 (0.3–1.0)/±10 %; 金手指區 ±0.03 |
| Copper | 1L 0.5/1 oz; 2–4L 0.33/0.5/1 oz |
| Trace/space | 0.33 oz 3/3 mil (extreme 2/2); 0.5 oz 3.5/3.5; 1 oz 4/4 (±20 %) |
| Drill | 0.1–6.5 mm (PTH ≤5 rec), tol ±0.08 |
| Via | std 0.3/0.55; extreme 2L 0.10/0.30, 4L 0.15/0.35 (fee); pad ≥ hole+0.2 (rec +0.25) |
| Annular | ≥0.25 rec (0.18 limit); NPTH↔copper ≥0.2; plated slot ≥0.5 |
| Half-hole | ≥0.3 mm dia, ≥0.5 to edge, ≥0.4 pitch |
| Coverlay | 黃/黑/白/透明; opening ≥pad+0.1/side, ↔trace ≥0.15; bridge ≥0.5 mm (else auto open); white +13–18 µm/side |
| 補強 | PI 0.1–0.25; FR4 0.1–1.6; 鋼片 0.1–0.3 (not near Hall sensors); 背膠 3M9077 0.05 / 3M468 0.13 / Tesa8854 0.1 (rec) |
| EMI film | 18 µm black, ground-window option |
| Outline | laser/punch; copper↔edge ≥0.3; tol ±0.1 (±0.05 special); 金手指↔edge 0.2 |
| Panel | gap 2 mm (3 with steel stiffener); border 5 mm; 4× Mark 光點 1 mm (one offset ≥5 mm anti-reverse); 4× tooling 2 mm; tabs 0.7–1.0 mm |

## 嘉立創 SMT assembly

| Item | 經濟型 Economic | 標準型 Standard |
|---|---|---|
| Sides | single (SMD+THT) | single/double |
| Layers / thickness | 2/4/6, 0.8–1.6 mm | unlimited |
| Board size | 10×10 – 470×570 mm | 70×70 – 460×510 mm |
| **Min package** | **0402** | **0201** |
| Min IC pitch | 0.4 mm | 0.35 mm |
| **Min BGA pitch** | 0.5 mm | **0.3 mm** |
| Reflow | 255±5 °C fixed | 240±5 °C adjustable |
| SPI / enhanced AOI | no | yes |
| Process border + Mark | not required | **required** (5 mm border, 1 mm Mark) |

## JLCPCB international (jlcpcb.com) — when ordering there instead

Sources: <https://jlcpcb.com/capabilities/pcb-capabilities>,
`flex-pcb-capabilities`, `pcb-assembly-capabilities`,
`pcb-stencil-manufacturing` (2026-07-06).

Mostly matches 嘉立創 at 1–2 oz mainstream specs (0.10/0.09 trace, 0.15/0.25
via, mask dams, silkscreen, V-cut/mouse-bite numbers), with these tighter
limits: **32 layers max, NO blind/buried vias, no 0.1 mm microvia, copper ≤4.5
oz (2L), thickness ≤4.5 mm, multilayer annular 0.18 limit, Standard-assembly
BGA ≥0.35 mm, press-fit ≤1.025 mm**, castellated ≥0.5 mm, board↔board panel
gap ≥2 mm, circular boards ≥20×20 mm, Economic assembly 2–50 pcs / Standard
2–80,000 pcs.

## 嘉立創 SMT 激光鋼網

Source: <https://www.jlc.com/portal/smtLaserSteelNet.html> (2026-07-06).

| Item | Capability |
|---|---|
| Material / cutting | 304TA 鋼片, LPKF laser, tol ±0.003 mm, 最小開孔 >0.08 mm |
| 鋼片厚度 | 0.06/0.08/0.10/0.12/0.13/0.15/0.18/0.20/0.30 mm (常規 0.10–0.20; 特殊 0.03–0.06, 0.25–0.5) |
| 規格 | 20+ 標準尺寸 (37×47, 42×52, 55×65, 73.6×73.6 cm ...) |
| 拋光 | 電解拋光 / 打磨拋光 (電解建議用於 pitch ≤0.5 mm 與 BGA) |
| 交期 / 格式 | 6 小時–2 天; Gerber / Protel / PowerPCB |

JLCPCB international stencil (<https://jlcpcb.com/capabilities/pcb-stencil-manufacturing>):
same 304 steel / ±0.003 mm / >0.08 mm aperture family; frameless 280×380–700×600 mm,
framework 400×300 (valid 240×140) – 736×736 (valid 500×500), rect ≤1500×500; foil std
0.10–0.20 mm (fee 0.03–0.08, 0.25–0.5); fiducials none/through/half-etched; step
stencils (framework only); top/bottom/combined.

Rule of thumb: 0.12 mm foil for mixed 0402 + fine-pitch; electropolish
whenever pitch ≤0.5 mm.

## Caveats (repeat when advising)

- Snapshot **2026-07-06**; the live 工藝能力 page and order-time audit are
  authoritative. 嘉立創 revises frequently (e.g. the 2025-06 mask 1:1 change).
- Tables mix **建議值 (recommended)** and **極限值 (absolute limits)** — when
  quoting a limit, say it is a limit, not a target.
- Special processes (HDI, microvia, heavy copper, backdrill, via fill, 6+
  layers, high-frequency) change price class; 沉金 is often mandatory for the
  extreme values.
- These are layout/fab constraints; akcli's checks are schematic-level. Use
  the SMT table to gate component choice at schematic time, and put fab
  constraints into design notes handed to layout.
