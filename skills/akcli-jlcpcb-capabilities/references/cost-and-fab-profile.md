# JLC cost model, via covering, and the `akcli fab check` bridge

The bridge from "capability numbers" to an **enforceable gate**. Covers the
free-vs-paid **via covering** model, the **surcharge triggers**, how to encode
them as an `akcli` **fab profile**, and — crucially — **which limits
`fab check` actually gates vs which stay advisory**. Sources: JLCPCB
「PCB Via Covering」<https://jlcpcb.com/hk/help/article/pcb-via-covering> and
「在什麼情況下會被收取額外費用」
<https://jlcpcb.com/hk/help/article/in-what-cases-will-there-be-charged-extra>
(2026-07-08 / re-verified 2026-07-15). Pricing **classes** (free vs paid) are
stable; exact prices are not — the online quote is authoritative.

---

## 1. Via covering (蓋孔) — free vs paid, and NOT the same as via fill

Two independent via decisions; do not conflate them:

- **Via fill (塞孔)** — what goes *inside* the barrel (ink / resin+cap /
  copper-paste+cap). A fabrication process, listed in `rigid-pcb.md` §4.
- **Via covering (蓋油/tenting)** — whether the **solder mask** covers the via
  opening on the *surface*. This is an **order option with cost consequences**,
  and it is what `fab check` reasons about.

| Covering option | Cost class | Notes |
|---|---|---|
| **Tented (蓋油, default)** | **free** | solder mask bridges over the via opening; works only up to a drill cap (~0.4 mm — larger holes can't be tented) |
| **Untented (open)** | **free** | via opening left exposed |
| Solder-mask plugged | usually free | mask plugs a small hole; limited to small drills |
| **Resin / epoxy filled + capped** | **paid** | IPC-4761 Type VII; enables via-in-pad; a price-class change |
| **Copper-paste filled + capped** | **paid** | thermal via-in-pad; paid |

Consequences an agent must know:
- **Via-in-pad requires a filled+capped (paid) process** — an SMD pad with a bare
  via wicks solder away and starves the joint. So via-in-pad is **forbidden by
  default** unless the design opts into (and pays for) fill+cap.
- **Tenting has a drill cap** (~0.4 mm): a via drill larger than the cap cannot be
  tented and must be untented or filled.
- **A via drill/pad below the vendor's free minimum** (≈0.30 mm drill / 0.40 mm
  pad on the JLC 4-layer baseline) falls into the **paid small-via process**.

The comfortable, all-free via is **0.3 mm drill / 0.6 mm pad, tented, not in a
pad** — see `rigid-pcb.md` §4.

---

## 2. What triggers a surcharge (cost drivers)

Group by **who can verify it**, because that decides whether it is a `fab check`
error, a warning, a review flag, or an order-manifest fact:

**A. Directly checkable from the PCB → hard rule (error if it violates the profile):**
- small via drill/pad below the free minimum (paid small-via process)
- via annular / via type (blind/buried) outside the profile
- via-in-pad without a registered exception
- a tented via whose drill exceeds the tenting cap (tented per the board file,
  or per the order manifest's `via_covering`; unknown → one note, not an error)
- an oval pad drill (slot) narrower than the profile's `[slot]` minimum

**B. Checkable from the PCB → cost warning (escalate with `[cost.error_if]`):**
- multilayer trace/space in the 3.0–3.5 mil band (≈0.076–0.089 mm) → fine-line surcharge
- board length ≥ ~600 mm
- single-board area > ~650 cm²
- drill density > ~150,000 holes/m²

**C. Estimable from Gerber, needs review (recompute post-export, not from schematic):**
- ENIG exposed-copper area > ~30 %
- V-Cut with a single edge < ~15 mm
- excessive routed-slot width / total slot-path density

**D. Order-manifest facts (the files don't carry them — declare, or it's undefined):**
- multi-design shared Gerber / panel vs single delivery format
- rush / expedited build
- special board material, non-standard thickness, heavy copper, or a surface
  finish other than the default
- the chosen **via covering** option

**E. External (quote-only, do not model offline):** stencil, PCBA labor, batch
lead time, live promotions — the online calculator is the source of truth.

---

## 3. Encode it as an `akcli` fab profile (the enforceable loop)

A **fab profile** is a versioned TOML that turns the numbers above into a gate.
`akcli fab check board.kicad_pcb --profile p.toml --order order.toml` runs the
board and the declared order against it. Reference file:
`examples/fab/jlc-4l-1oz.toml` — also shipped inside akcli, so `--profile
jlc-4l-1oz-free-vias-2026-07` works with no file at all (`akcli fab profiles`
lists the bundled ids; a file path always wins over an id). **Capability number →
profile field:**

| Capability (from the reference tables) | Profile field |
|---|---|
| layer count | `[stackup] layers` |
| board thickness | `[stackup] thickness_mm` |
| free via covering | `[via] covering = "tented"` |
| free-via drill / pad minimum | `[via] min_drill_mm`, `min_pad_mm` |
| recommended annular margin | `[via] preferred_annular_mm` |
| tenting drill cap | `[via] max_tented_drill_mm` |
| via-in-pad forbidden | `[via] forbid_via_in_pad = true` |
| blind/buried not on this line | `[via] forbid_blind_buried = true` |
| slot width (plated / non-plated) | `[slot] min_plated_width_mm`, `min_non_plated_width_mm` (`min_width_mm` for either) |
| board length surcharge | `[cost.warn_if] board_length_mm_gte` |
| board area surcharge | `[cost.warn_if] board_area_cm2_gt` |
| drill-density surcharge | `[cost.warn_if] drill_density_per_m2_gt` |
| fine multilayer trace surcharge | `[cost.warn_if] trace_width_mm_lte` |
| a threshold that must fail, not warn | the same key under `[cost.error_if]` |
| floors for the project's own DRC constraints | `[rules] min_clearance_mm`, `min_track_width_mm`, `min_via_diameter_mm`, `min_through_hole_mm`, `min_via_annular_mm`, `min_hole_to_hole_mm`, `min_hole_clearance_mm`, `min_copper_edge_clearance_mm` |
| source URLs + date | `[source] urls`, `retrieved_at` (mandatory) |
| an approved exception (thermal via) | `[[exception]] type/component/owner/reason/expires` |

**Order manifest** (`order.toml`, the D-list facts) required keys:
`delivery_format`, `design_count`, `rush`, `surface_finish`, `via_covering`,
`board_material`, `copper_weight_oz` (+ optional `thickness_mm` for a
profile-consistency check). A missing key is `ORDER_INCOMPLETE`; ENIG / panel /
multi-design raise `ORDER_REVIEW_REQUIRED`; a covering/thickness that disagrees
with the profile is `ORDER_PROFILE_CONFLICT`.

**Versioning discipline:** a profile is ONE vendor-capability revision. When the
vendor page changes, cut a NEW file with a new `id` — never mutate an old
revision (a released board must stay reproducible against the profile it passed).
`jlc-4l-1oz-free-vias-2026-07-r2` (`examples/fab/jlc-4l-1oz-r2.toml`) is such a new
file: the 2026-07 numbers unchanged, plus `[slot]` from `rigid-pcb.md` §half-hole &
slots (multilayer plated ≥0.35 mm, non-plated ≥1.0 mm).
The bundled profiles follow the same rule, and age the same way: a bundled profile
whose `retrieved_at` is over 12 months old raises a `FAB_PROFILE_STALE` note on every
`fab check` — a prompt to re-read the vendor pages, not a failure.

---

## 4. What `fab check` GATES vs what stays advisory

`akcli fab check` is a **fab-policy + cost gate, NOT a geometric DRC.** Know the
boundary so you neither over-trust it nor duplicate KiCad's DRC.

**`fab check` enforces (finding codes):**

| Area | Findings |
|---|---|
| Via geometry / covering | `FAB_VIA_PAID_PROCESS`, `FAB_VIA_TENTED_TOO_BIG`, `FAB_VIA_MIN_MARGIN` (note), `FAB_VIA_ANNULAR_BELOW_PREFERRED` (note) |
| Via type / via-in-pad | `FAB_VIA_TYPE_FORBIDDEN` (blind, buried, micro), `FAB_VIA_IN_PAD`, `FAB_VIA_IN_PAD_EXCEPTION` (allowed), `FAB_EXCEPTION_EXPIRED` |
| Slots (oval pad drills) | `FAB_SLOT_BELOW_MIN` |
| Stackup drift | `FAB_STACKUP_MISMATCH` |
| Cost thresholds | `FAB_COST_BOARD_LENGTH`, `FAB_COST_BOARD_AREA`, `FAB_COST_DRILL_DENSITY`, `FAB_COST_TRACE_WIDTH` — warning under `[cost.warn_if]`, error under `[cost.error_if]` |
| Project DRC drift | `FAB_RULES_LOOSER_THAN_PROFILE` (the `.kicad_pro` allows less than the profile's minimums) |
| Order manifest | `ORDER_INCOMPLETE`, `ORDER_REVIEW_REQUIRED`, `ORDER_PROFILE_CONFLICT` |
| Meta / honesty | `FAB_NO_OUTLINE` (nothing on Edge.Cuts — didn't pretend to measure), `FAB_TENTING_UNKNOWN` (note: tenting cap not applied), `FAB_UNSUPPORTED_SOURCE` |

Area and drill density use the true `Edge.Cuts` outline — arcs exact, cutouts
subtracted, footprint-drawn edges included. If the shapes do not close, the
bounding box is used and the finding says so.

Severity policy: direct violations = **error**; cost-threshold crossings =
**warning** (with actual value vs threshold); boundary-exact geometry = **note**;
a registered exception passes as an explicit note (an expired one is an error).

**`fab check` does NOT gate — use another tool:**

| Not gated by fab check | Who checks it |
|---|---|
| minimum trace width / spacing (fab check only flags the fine-line *cost* band) | **`kicad-cli pcb drc`** (net-class rules) |
| minimum annular ring (only a "below preferred" note) | `kicad-cli pcb drc` |
| solder mask dam / mask-to-trace, silkscreen height/on-pad | `kicad-cli pcb drc` + `dfm-and-file-prep.md` |
| width of a slot routed on `Edge.Cuts` (only oval pad drills are measured), board-edge clearance, min drilled hole | `kicad-cli pcb drc` |
| ENIG exposed-copper %, V-Cut short edge, panel features, slot density | order-time review (`ORDER_REVIEW_REQUIRED` flags intent and says "NOT computed"; V-cut/panel data is not in the board file) |
| component-to-edge, polarity, tombstoning (assembly DFA) | human review + `assembly-and-stencil.md` |

**Bottom line:** gate with **both** — `kicad-cli pcb drc` for geometric design
rules, `akcli fab check --profile --order` for vendor fab policy + cost + order
completeness. `akcli release preflight --pcb … --drc --fab-profile … --order …` runs
the two as gates of one release manifest (KiCad's DRC on a staged copy, zone fills
checked as saved). Neither replaces the vendor's own order-time DFM/quote.
