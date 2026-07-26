"""Review rule tables: thresholds, keyword sets, assumptions.

Every constant cites the engineering source it rests on; role-classification
keyword sets follow common industry designator/library conventions.
"""

from __future__ import annotations

# IC pin names that mark a regulator feedback / sense input (vendor datasheet
# conventions: FB/ADJ on LDOs and switchers, SENSE on remote-sense parts).
FB_PIN_NAMES: frozenset[str] = frozenset({
    "FB", "NFB", "VFB", "ADJ", "ADJUST", "SENSE", "VSENSE", "VSNS",
    "FB1", "FB2", "SNS",
})

# Plausible bandgap/reference window for an implied regulator Vref (volts).
# Real references cluster in 0.5–1.25 V (buck/boost) up to 2.5 V (shunt
# references); the band is deliberately conservative — outside it the divider
# is likely swapped or mis-valued.
VREF_PLAUSIBLE_V: tuple[float, float] = (0.2, 3.0)

# Divider tap: implied-by-name voltage vs computed mismatch tolerance.
DIVIDER_TAP_TOL: float = 0.05

# Crystal load: stray/board capacitance assumption (pF) added to the series
# combination of the two load caps — CL = C1·C2/(C1+C2) + C_stray.
# (Standard crystal application figure, e.g. ST AN2867 §3: 2–5 pF.)
XTAL_CSTRAY_PF: float = 4.0

# Load caps are expected symmetric; beyond this relative difference the pair
# is flagged.
XTAL_LOAD_TOL: float = 0.05

# Part-ROLE classification (TVS/ESD keywords, connector and crystal prefixes)
# is NOT here: it lives in :mod:`akcli.classify`, shared with the check layer
# so `check` and `review` cannot disagree about what a part is. Only
# review-specific thresholds and keyword sets stay in this module.

# Fuse / resettable-PTC classification: designator prefix + lib/value keywords
# (IEC 60617 reference designator "F"; "polyfuse"/"ptc" per resettable-fuse
# vendor naming). "FB" (ferrite bead) does NOT match — the prefix is the full
# leading letter run.
FUSE_PREFIXES: frozenset[str] = frozenset({"F"})
FUSE_KEYWORDS: tuple[str, ...] = ("fuse", "polyfuse", "ptc")

# Rectifier/Schottky diode classification (reverse-polarity protection).
# TVS parts and LEDs also carry the D prefix and are excluded by keyword.
DIODE_PREFIXES: frozenset[str] = frozenset({"D", "CR"})
DIODE_KEYWORDS: tuple[str, ...] = ("diode", "schottky", "d_schottky")

# Power-entry net-name tokens: rails that arrive from OUTSIDE the board
# (battery, DC jack, USB VBUS) and therefore deserve fuse + reverse-polarity
# protection. Matched as delimited tokens inside the net name; deliberately
# excludes post-protection names (VSYS, VDD, +3V3 …).
POWER_ENTRY_RX_TOKENS: str = (
    r"(V?BATT?|VIN|VBUS|VDC|DCIN|PWR_?IN|VSUPPLY|VEXT)")

# Series elements the power-entry chain walk may cross (fuse / diode /
# inductor-ferrite); the walk never crosses capacitors or resistors, so it
# cannot wander into dividers or decoupling. Chain length is bounded.
POWER_CHAIN_MAX_NETS: int = 6

# Resistor/capacitor classification: designator prefixes. (Crystal prefixes
# live in :mod:`akcli.classify` — see the note above.)
RESISTOR_PREFIXES: frozenset[str] = frozenset({"R"})
CAPACITOR_PREFIXES: frozenset[str] = frozenset({"C"})

# --------------------------------------------------------------------------- #
# PCB review thresholds (M5)
# --------------------------------------------------------------------------- #
# Decoupling cap → IC power-pad distance ceiling. Standard high-speed layout
# guidance places the 100 nF within a few mm of the pin it decouples.
DECAP_MAX_MM: float = 4.0

# Thermal pad recognition + via floor: an exposed pad this large on an active
# part should carry at least this many vias (typical package thermal
# application guidance: 4–9 vias under the EP).
THERMAL_PAD_MIN_MM2: float = 4.0
THERMAL_VIA_MIN: int = 4

# IPC-2221 trace-ampacity assumptions (stated on every finding).
TRACE_DTEMP_C: float = 10.0
TRACE_COPPER_OZ: float = 1.0

# Typical package θ_JA (K/W, JEDEC 2s2p board) — FALLBACK ONLY when a facts
# file has no theta_ja; matched against the footprint name, judgement stays
# heuristic. Values are representative mid-range figures.
PACKAGE_THETA_JA: tuple[tuple[str, float], ...] = (
    ("SOT-23", 250.0),
    ("SOT-223", 60.0),
    ("SOT-89", 120.0),
    ("SOIC-8", 120.0),
    ("SO-8", 120.0),
    ("TSSOP", 110.0),
    ("QFN", 45.0),
    ("DFN", 50.0),
    ("TO-252", 50.0),   # DPAK
    ("TO-263", 40.0),   # D2PAK
    ("TQFP", 55.0),
)

# Junction-temperature judgement defaults (stated as assumptions).
T_AMBIENT_C: float = 25.0
T_JUNCTION_MAX_DEFAULT_C: float = 125.0

# --------------------------------------------------------------------------- #
# EMC review (M6) — every numeric threshold here is an ASSUMPTION and is
# stated verbatim on the finding that uses it. Positioning: risk analyzer,
# not a compliance predictor.
# --------------------------------------------------------------------------- #
EMC_DISCLAIMER: str = (
    "pre-compliance risk analysis — only a calibrated measurement in an "
    "accredited lab can establish regulatory compliance")

# Ground-via stitching: spacing floor λ/20 at the assumed highest harmonic
# content (1 GHz), ε_eff 4.3 (FR4). λ/20 ≈ 7.2 mm.
EMC_FMAX_HZ: float = 1e9
EMC_ER_EFF: float = 4.3
EMC_STITCH_FRACTION: int = 20

# Board-edge proximity margin for routed signals (fringing fields radiate
# past the plane edge); rectangular-outline approximation.
EMC_EDGE_MARGIN_MM: float = 0.5

# Differential-pair intra-pair skew ceiling and the FR4 propagation figure
# used to convert length to time (≈167 ps/inch microstrip).
EMC_DIFF_SKEW_PS: float = 25.0
EMC_PS_PER_MM: float = 6.6

# TVS clamp must sit close to the connector it protects (ESD current path).
EMC_TVS_CONN_MAX_MM: float = 10.0

# Clock-net name tokens (matched as tokens inside the net name).
CLOCK_NET_TOKENS: tuple[str, ...] = (
    "CLK", "SCK", "SCLK", "MCLK", "BCLK", "LRCLK", "XTAL", "OSC",
)

# Risk-score weights per severity (score capped at 100).
EMC_RISK_WEIGHTS: dict[str, int] = {
    "critical": 25, "error": 15, "warning": 8, "note": 3, "info": 1,
}
