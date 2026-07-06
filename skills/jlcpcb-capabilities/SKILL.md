---
name: jlcpcb-capabilities
description: >-
  Consult JLCPCB's manufacturing capabilities while designing a board — rigid PCB,
  flex PCB, PCB assembly (PCBA/SMT), and SMT stencil limits, extracted from JLCPCB's
  official capability pages with exact numbers. Use this skill whenever the task
  involves: choosing trace width/spacing, via or drill sizes, board size/thickness,
  copper weight, solder mask or silkscreen limits; checking whether a component
  package (0201/0402, BGA pitch, fine-pitch IC) is assemblable; deciding Economic vs
  Standard PCBA; panelization (V-cut, mouse bites, edge rails, fiducials); flex/rigid
  choices, stiffeners, coverlay; or stencil ordering. Triggers on keywords: JLCPCB,
  DFM, design rules, manufacturing capability, 製程能力, min trace, min spacing,
  annular ring, via size, drill, board thickness, copper weight, solder mask dam,
  silkscreen, castellated, panelization, V-cut, mouse bites, PCBA, SMT assembly,
  BGA pitch, 0402, 0201, stencil, coverlay, stiffener, flex PCB.
---

# jlcpcb-capabilities — manufacturer limits to design against

Data below was extracted from JLCPCB's official capability pages on
**2026-07-06**. It is a **point-in-time snapshot**: quote the numbers with that
date, and for anything order-critical (or borderline) tell the user to confirm
against the live page and the order-time DFM check.

Sources:

- Rigid PCB: <https://jlcpcb.com/capabilities/pcb-capabilities>
- Flex PCB: <https://jlcpcb.com/capabilities/flex-pcb-capabilities>
- Assembly (PCBA): <https://jlcpcb.com/capabilities/pcb-assembly-capabilities>
- Stencil: <https://jlcpcb.com/capabilities/pcb-stencil-manufacturing>

## How to apply this while drawing

- **At schematic time (akcli's territory), assembly limits bite first**: pick
  packages the PCBA line can place (≥0402 for Economic, ≥0201 for Standard;
  BGA pitch ≥0.5 mm Economic / ≥0.35 mm Standard; IC pin pitch ≥0.4 / ≥0.35 mm).
  Prefer JLCPCB **Basic/Preferred** parts (`akcli jlc search`, see the
  parts-sourcing skill) to avoid per-reel Extended fees.
- **Comfortable defaults vs absolute limits**: the tables give absolute minimums.
  For an ordinary 1–2 oz 2–4 layer board, design at ≥0.127 mm (5 mil)
  trace/space, via 0.3 mm hole / 0.6 mm diameter, and you sit far from every
  limit at no cost. Reserve limit values (0.09/0.10 mm traces, 0.15/0.25 mm
  vias) for dense BGA fanout, and say so explicitly in the design notes.
- **Copper weight changes the rules**: 2 oz outer copper roughly doubles the
  minimum trace/space (0.16 mm) and raises the solder-mask dam minimum to
  0.20 mm — re-check clearances when the user asks for heavy copper.
- **No blind/buried vias** on the standard rigid process — plan BGA escape
  routing with through-vias (via-in-pad is available, epoxy/copper filled,
  0.15–0.55 mm).
- **Black/white solder mask is coarser** (0.13 mm dam vs 0.10 mm) — mention it
  when aesthetics drive the color.
- When emitting design rules for a layout tool, map: min track/space,
  via hole/diameter, hole-to-hole, copper-to-edge, mask expansion (1:1 here),
  and silkscreen width/height from the tables below.

## Rigid PCB (FR-4 unless noted)

### Layers, size, thickness

| Item | Capability |
|---|---|
| Layer count | 1–32 |
| Max size (2-layer) | 670 × 600 mm (extended 1020 × 600 mm) |
| Max size (4-layer) | 663 × 593 mm (extended 1016 × 596 mm) |
| Max size (6+ layers) | 656 × 586 mm |
| Max size (Rogers/PTFE) | 590 × 438 mm; Aluminum 602 × 506 mm; Copper core 480 × 286 mm |
| Min size | 3 × 3 mm (FR4/Rogers); 5 × 5 mm (Al/Cu core); 10 × 10 mm (castellated/plated edge) |
| Thickness | 0.4–4.5 mm (standard: 0.4/0.6/0.8/1.0/1.2/1.6/2.0) |
| Thickness tolerance | ±10 % (≥1.0 mm), ±0.1 mm (<1.0 mm) |
| Outline tolerance | ±0.2 mm routed (±0.1 mm precision), ±0.4 mm V-score |

### Trace / space (min, by copper weight)

| Copper | Layers | Min track/space |
|---|---|---|
| 1 oz | 1–2 layer | 0.10 / 0.10 mm (4/4 mil) |
| 1 oz | multilayer | 0.09 / 0.09 mm (3.5/3.5 mil) |
| 2 oz | 2 layer | 0.16 / 0.16 mm |
| 2 oz | multilayer | 0.15 / 0.15 mm |
| 2.5 / 3.5 / 4.5 oz | 2 layer | 0.20 / 0.25 / 0.30 mm |

Outer copper 1–4.5 oz (2-layer) or 1–2 oz (multilayer); inner 0.5/1/2 oz
(default 0.5 oz). Track width tolerance ±20 %.

### Drills, vias, clearances

| Item | Capability |
|---|---|
| Drill diameter | 0.15–6.3 mm (2+ layers); 0.3 mm min 1-layer; 0.65 Al; 1.0 Cu core |
| Via (min) | 0.15 mm hole / 0.25 mm diameter; **through-hole vias only — no blind/buried** |
| Annular ring PTH | ≥0.25 mm recommended (abs 0.18 mm @1 oz; 0.254 mm @2 oz); NPTH ≥0.45 mm |
| Via↔via (hole edge) | 0.2 mm; pad-holes 0.45 mm |
| Via↔track | 0.2 mm; PTH↔track 0.28 mm (0.35 recommended); NPTH↔track 0.2 mm |
| Pad↔track | 0.1 mm (0.09 mm locally for BGA) |
| Hole tolerance | PTH +0.13/−0.08 mm; position ±0.05 mm; plating ~18 µm |
| Plated slot | ≥0.5 mm (2-layer) / ≥0.35 mm (multilayer); non-plated ≥1.0 mm |
| Via-in-pad | epoxy or copper filled+capped, 0.15–0.55 mm |
| Castellated holes | ≥0.5 mm dia, ≥1 mm from edge, ≥0.5 mm hole-to-hole |
| Backdrill | 4–32 layers, 0.2–0.5 mm, ≥0.15 mm dielectric to next layer |
| Copper→routed edge | ≥0.2 mm (V-cut: ≥0.4 mm) |

### Solder mask, silkscreen, pads

| Item | Capability |
|---|---|
| Mask colors | green/purple/red/yellow/blue/white/black (LPI), expansion 1:1, ink ≥10 µm |
| Mask dam (1 oz) | ≥0.10 mm (green/red/yellow/blue/purple); **≥0.13 mm black/white**; ≥0.20 mm @2 oz |
| Mask↔trace clearance | ≥0.09 mm |
| Silkscreen | line ≥0.15 mm, char height ≥1.0 mm (40 mil), 1:6 ratio, ≥0.15 mm off pads |
| Min BGA pad | 0.2–0.25 mm (ENIG required at that size) |
| Min SMD pad | 0.25 × 0.25 mm; pad↔pad (different nets) 0.15 mm |

### Materials, finishes, impedance

- FR-4 grade A (Nan Ya/KB/Shengyi); Dk ≈ 4.1–4.5 by prepreg (7628: 4.5, 3313: 4.4, 2116: 4.1).
- Aluminum core (1-layer), copper core (1-layer, direct heatsink), Rogers/PTFE (2-layer RF).
- Finishes: HASL (leaded/lead-free), ENIG (required for 6+ layers, fine BGA), OSP.
- Controlled impedance on 4–32 layers, ±10 %, calculator: <https://jlcpcb.com/pcb-impedance-calculator>.

### Panelization

| Item | Capability |
|---|---|
| V-cut | panel 70×70 to 475×475 mm, copper ≥0.4 mm from cut, angle 25°, cut spacing ≥2 mm (3 rec.) |
| Mouse bites | board spacing 1.6/2 mm, bite Ø 0.5–0.8 mm, tab ≥4 mm (5 with bites) |
| Rails / tooling | edge ≥3 mm (≥5 mm for SMT), tooling holes 2 mm, fiducial 3.85 mm from edge |
| General | board-to-board ≥2 mm; circular boards ≥20 × 20 mm |

## Flex PCB

| Item | Capability |
|---|---|
| Layers | 1, 2, 4 (polyimide 25/50 µm; transparent PET variants) |
| Max size | 234 × 490 mm regular (250 × 600 mm with edge rails); panelize below 20 × 20 mm |
| Finished thickness | 1L 0.07–0.12 mm; 2L 0.11–0.24 mm; 4L 0.20–0.45 mm (by stackup) |
| Copper | 0.33/0.5/1 oz (2–4 L), 0.5/1 oz (1 L) |
| Trace/space | 0.33 oz: 3/3 mil (abs 2/2); 0.5 oz: 3.5/3.5 mil; 1 oz: 4/4 mil (±20 %) |
| Vias | hole 0.3 mm regular (extreme 0.10 mm 2L / 0.15 mm 4L); via pad ≥ hole + 0.2 mm |
| Annular ring | ≥0.25 mm rec (abs 0.18 mm); plated slot ≥0.5 mm |
| Coverlay | yellow/black/white/transparent; opening↔trace ≥0.15 mm; expansion 0.1 mm; white ~13–18 µm thicker/side |
| Stiffeners | PI 0.1–0.25 mm; FR4 0.1–1.6 mm; stainless 0.1–0.3 mm; 3M tape 0.05–0.13 mm |
| EMI shielding film | 18 µm, black |
| Finish | ENIG 1u"/2u"; copper→edge ≥0.3 mm; outline ±0.1 mm (±0.05 on request) |
| Silkscreen | line ≥0.15 mm, height ≥1 mm; castellated ≥0.3 mm dia, ≥0.5 mm to edge |

(Bend-radius guidance is not published on the capability page — flag it as a
question for JLCPCB support when the design flexes dynamically.)

## PCB assembly (PCBA)

| Item | Economic | Standard |
|---|---|---|
| Sides | single (SMT/THT) | double |
| PCB layers | 2/4/6 | 1–32 |
| Board thickness | 0.8–1.6 mm | no limit |
| Single PCB size | 10×10 – 470×500 mm | 70×70 – 460×500 mm |
| Panel size | ≤250×250 mm | ≤250×250 mm |
| Order volume | 2–50 pcs | 2–80,000 pcs |
| **Min package** | **0402** | **0201** |
| Min IC pin pitch | 0.4 mm | 0.35 mm |
| Min BGA pitch | 0.5 mm | 0.35 mm |
| Edge rails / fiducials | not required | **required** |
| Gold fingers / castellated / edge plating | not supported | supported |
| Inspection | AOI + X-ray (BGA) + visual | + SPI |
| Reflow | 255±5 °C | 240±5 °C |

Design consequences: on Economic PCBA avoid 0201s, <0.4 mm-pitch ICs and
<0.5 mm BGAs entirely; for Standard, add rails + fiducials to the panel from
the start. Component availability/pricing (Basic vs Extended) is a separate
axis — check with `akcli jlc search`/`show` (parts-sourcing skill).

## SMT stencil

| Item | Capability |
|---|---|
| Material | 304 HTA stainless steel, laser cut, tolerance ±0.003 mm |
| Min aperture | >0.08 mm |
| Thickness (standard) | 0.10/0.12/0.15/0.18/0.20 mm |
| Thickness (extra cost) | 0.03–0.08 mm and 0.25–0.5 mm |
| Non-framework | 280×380 to 700×600 mm (custom ≤650×580) |
| Framework | 400×300 (valid 240×140) to 736×736 (valid 500×500); rect ≤1500×500 (valid 1300×320) |
| Electropolish | recommended for IC pitch ≤0.5 mm and BGA |
| Fiducials | none / etched-through / half-etched |
| Step (multi-level) | framework stencils only |
| Sides | top / bottom / both-on-one / separate |

Rule of thumb: 0.12 mm foil suits mixed 0402+fine-pitch boards; specify
electropolishing whenever pitch ≤0.5 mm.

## Caveats (repeat these when advising)

- Snapshot of **2026-07-06** — JLCPCB revises capabilities; the live page and
  the order-time DFM report are authoritative.
- Tables mix **recommended** and **absolute** values; when quoting an absolute
  minimum, say it is a limit, not a target.
- Special processes (heavy copper, Rogers, via-in-pad, backdrill, 6+ layers,
  extended sizes) change price class — surface that in recommendations.
- These are **layout/fab** constraints; akcli's own checks are schematic-level.
  Use this skill to gate *component choice* (assembly table) at schematic time
  and to state fab constraints in design notes handed to layout.
