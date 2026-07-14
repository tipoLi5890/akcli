"""Fab profiles: versioned manufacturing policy checked against a ``.kicad_pcb``.

A profile is TOML (stdlib ``tomllib``) capturing ONE vendor capability revision
with its sources — never a hardcoded global constant:

.. code-block:: toml

    id = "jlc-4l-1oz-free-vias-2026-07"
    vendor = "JLCPCB"

    [source]
    urls = ["https://jlcpcb.com/hk/help/article/pcb-via-covering"]
    retrieved_at = "2026-07-14"

    [stackup]
    layers = 4
    thickness_mm = 1.0

    [via]
    min_drill_mm = 0.30            # below this: paid small-via process
    min_pad_mm = 0.40
    preferred_annular_mm = 0.075
    max_tented_drill_mm = 0.40     # tented covering caps the drill
    forbid_via_in_pad = true
    forbid_blind_buried = true

    [cost.warn_if]
    board_length_mm_gte = 600
    board_area_cm2_gt = 650
    drill_density_per_m2_gt = 150000
    trace_width_mm_lte = 0.0889

    [[exception]]
    type = "thermal_via"
    component = "U1"               # vias inside U1's pads are approved
    owner = "hw-lead"
    reason = "QFN exposed-pad thermal vias"
    expires = "2027-01-01"

Severity policy (mirrors the enhancement plan): direct profile violations
(paid via geometry, oversize tented via, via-in-pad, forbidden via types,
stackup drift) are ERRORs; cost-threshold crossings are WARNINGs with the
actual value vs the threshold; boundary-exact geometry passes with a NOTE
("minimum margin"); an approved exception passes as an explicit NOTE, never
silently. Missing evidence (no board outline) is itself a WARNING — the check
never pretends it measured something it could not.

The **order manifest** (:func:`check_order`) is the user's declared purchase
intent — delivery format, surface finish, via covering… — validated for
completeness; it is never guessed from the PCB.
"""

from __future__ import annotations

import datetime as _dt
import math
import os

from .errors import fail
from .model import Pcb
from .report import Finding, Severity, anchor

__all__ = ["load_profile", "check", "load_order", "check_order", "explain"]

_EPS = 1e-6


def load_profile(path: os.PathLike | str) -> dict:
    """Load + structurally validate a fab-profile TOML file."""
    import tomllib
    from pathlib import Path

    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail("BAD_CONFIG", f"fab profile {path}: {exc}")
    if not doc.get("id"):
        fail("BAD_CONFIG", f"fab profile {path}: missing 'id'")
    if not (doc.get("source") or {}).get("urls"):
        fail("BAD_CONFIG",
             f"fab profile {path}: missing [source] urls — a profile must "
             "carry its evidence (vendor page + retrieved_at)")
    for i, exc_rule in enumerate(doc.get("exception") or []):
        if not exc_rule.get("owner") or not exc_rule.get("reason"):
            fail("BAD_CONFIG",
                 f"fab profile {path}: exception #{i} needs owner AND reason")
    return doc


def _exception_for(profile: dict, *, etype: str, component: str | None,
                   today: _dt.date) -> tuple[dict | None, bool]:
    """Find a matching approved exception; returns (rule, expired)."""
    for rule in profile.get("exception") or []:
        if rule.get("type") != etype:
            continue
        want = rule.get("component")
        if want and component and want != component:
            continue
        expires = rule.get("expires")
        if expires:
            try:
                if _dt.date.fromisoformat(str(expires)) < today:
                    return rule, True
            except ValueError:
                return rule, True
        return rule, False
    return None, False


def _point_in_pad(px: float, py: float, pad: dict) -> bool:
    """Is board point (px,py) inside the pad's (rotated) rectangle?"""
    ax, ay = pad["at"]
    rot = float(pad.get("rotation") or 0.0)
    dx, dy = px - ax, py - ay
    if rot:
        r = math.radians(rot)
        c, s = math.cos(r), math.sin(r)
        # inverse of the reader's CCW-on-screen rotation
        dx, dy = dx * c - dy * s, dx * s + dy * c
    sx, sy = pad["size"]
    return abs(dx) <= sx / 2 + _EPS and abs(dy) <= sy / 2 + _EPS


def check(pcb: Pcb, profile: dict, *, today: _dt.date | None = None) -> list[Finding]:
    """Check a deep-read KiCad board against a fab profile."""
    findings: list[Finding] = []
    out = findings.append
    today = today or _dt.date.today()
    pid = profile.get("id", "?")

    if pcb.source_format != "kicad":
        out(Finding("FAB_UNSUPPORTED_SOURCE", Severity.WARNING,
                    "fab check currently understands KiCad boards only "
                    f"(got {pcb.source_format})"))
        return findings

    # --- stackup drift ------------------------------------------------------ #
    stackup = profile.get("stackup") or {}
    want_layers = stackup.get("layers")
    have_layers = len(pcb.board.get("copper_layers") or [])
    if want_layers is not None and have_layers and have_layers != want_layers:
        out(Finding("FAB_STACKUP_MISMATCH", Severity.ERROR,
                    f"profile {pid} is a {want_layers}-layer profile; board has "
                    f"{have_layers} copper layers"))
    want_thick = stackup.get("thickness_mm")
    have_thick = pcb.board.get("thickness")
    if (want_thick is not None and have_thick
            and abs(float(have_thick) - float(want_thick)) > 0.011):
        out(Finding("FAB_STACKUP_MISMATCH", Severity.ERROR,
                    f"profile thickness {want_thick}mm, board setup says "
                    f"{have_thick}mm"))

    # --- vias ---------------------------------------------------------------- #
    via_rules = profile.get("via") or {}
    min_drill = via_rules.get("min_drill_mm")
    min_pad = via_rules.get("min_pad_mm")
    pref_annular = via_rules.get("preferred_annular_mm")
    max_tented = via_rules.get("max_tented_drill_mm")
    smd_pads = [p for p in pcb.pads if p.get("pad_type") == "smd"]

    for i, via in enumerate(pcb.vias):
        at = via.get("at") or (0.0, 0.0)
        where = f"via@({at[0]:g},{at[1]:g})"
        drill = float(via.get("drill") or 0.0)
        size = float(via.get("size") or 0.0)

        if via.get("type") in ("blind", "micro") and via_rules.get("forbid_blind_buried"):
            out(Finding("FAB_VIA_TYPE_FORBIDDEN", Severity.ERROR,
                        f"{where}: {via['type']} via — profile {pid} forbids "
                        "blind/buried/micro vias",
                        pos=at, anchors=[anchor("net", via.get("net") or "?", at)]))

        if min_drill is not None and drill and drill < float(min_drill) - _EPS:
            out(Finding("FAB_VIA_PAID_PROCESS", Severity.ERROR,
                        f"{where}: drill {drill:g}mm < free minimum "
                        f"{min_drill}mm — paid small-via process",
                        pos=at))
        elif min_drill is not None and drill and abs(drill - float(min_drill)) <= _EPS:
            out(Finding("FAB_VIA_MIN_MARGIN", Severity.NOTE,
                        f"{where}: drill {drill:g}mm sits exactly on the free "
                        f"minimum ({min_drill}mm) — zero manufacturing margin",
                        pos=at))
        if min_pad is not None and size and size < float(min_pad) - _EPS:
            out(Finding("FAB_VIA_PAID_PROCESS", Severity.ERROR,
                        f"{where}: pad {size:g}mm < free minimum {min_pad}mm",
                        pos=at))
        if (pref_annular is not None and drill and size
                and (size - drill) / 2 < float(pref_annular) - _EPS):
            out(Finding("FAB_VIA_ANNULAR_BELOW_PREFERRED", Severity.NOTE,
                        f"{where}: annular {(size - drill) / 2:.3f}mm < preferred "
                        f"{pref_annular}mm — passes, but below the recommended "
                        "margin",
                        pos=at))
        if max_tented is not None and drill and drill > float(max_tented) + _EPS:
            out(Finding("FAB_VIA_TENTED_TOO_BIG", Severity.ERROR,
                        f"{where}: drill {drill:g}mm > tenting cap "
                        f"{max_tented}mm — cannot be tented under {pid}",
                        pos=at))

        if via_rules.get("forbid_via_in_pad"):
            host = next((p for p in smd_pads
                         if _point_in_pad(at[0], at[1], p)), None)
            if host is not None:
                comp = host.get("component") or "?"
                rule, expired = _exception_for(
                    profile, etype="thermal_via", component=comp, today=today)
                label = f"{comp}.{host.get('number')}"
                if rule is not None and not expired:
                    out(Finding("FAB_VIA_IN_PAD_EXCEPTION", Severity.NOTE,
                                f"{where}: inside pad {label} — allowed by "
                                f"exception ({rule.get('reason')}, owner "
                                f"{rule.get('owner')})",
                                pos=at, anchors=[anchor("pin", label, at)]))
                elif rule is not None and expired:
                    out(Finding("FAB_EXCEPTION_EXPIRED", Severity.ERROR,
                                f"{where}: inside pad {label} — matching "
                                f"exception EXPIRED ({rule.get('expires')}); "
                                f"owner {rule.get('owner')} must re-review",
                                pos=at, anchors=[anchor("pin", label, at)]))
                else:
                    out(Finding("FAB_VIA_IN_PAD", Severity.ERROR,
                                f"{where}: via inside SMD pad {label} — "
                                f"forbidden by {pid} (register a thermal_via "
                                "exception if intentional)",
                                pos=at, anchors=[anchor("pin", label, at)]))

    # --- cost thresholds ------------------------------------------------------ #
    warn_if = ((profile.get("cost") or {}).get("warn_if")) or {}
    bbox = pcb.board.get("outline_bbox")
    if bbox is None and (warn_if.get("board_length_mm_gte")
                         or warn_if.get("board_area_cm2_gt")
                         or warn_if.get("drill_density_per_m2_gt")):
        out(Finding("FAB_NO_OUTLINE", Severity.WARNING,
                    "board has no Edge.Cuts outline — size/area/drill-density "
                    "cost rules were NOT evaluated"))
    elif bbox is not None:
        (x0, y0), (x1, y1) = bbox
        length = max(x1 - x0, y1 - y0)
        area_cm2 = (x1 - x0) * (y1 - y0) / 100.0
        thr = warn_if.get("board_length_mm_gte")
        if thr is not None and length >= float(thr):
            out(Finding("FAB_COST_BOARD_LENGTH", Severity.WARNING,
                        f"board length {length:g}mm >= {thr}mm — vendor "
                        "surcharge threshold"))
        thr = warn_if.get("board_area_cm2_gt")
        if thr is not None and area_cm2 > float(thr):
            out(Finding("FAB_COST_BOARD_AREA", Severity.WARNING,
                        f"board bbox area {area_cm2:.1f}cm² > {thr}cm² — "
                        "vendor surcharge threshold (bbox approximation)"))
        thr = warn_if.get("drill_density_per_m2_gt")
        if thr is not None and area_cm2 > 0:
            holes = len(pcb.vias) + sum(
                1 for p in pcb.pads if p.get("drill"))
            density = holes / (area_cm2 / 1e4)
            if density > float(thr):
                out(Finding("FAB_COST_DRILL_DENSITY", Severity.WARNING,
                            f"drill density {density:,.0f}/m² > {thr:,}/m² "
                            f"({holes} holes over {area_cm2:.1f}cm² bbox)"))
    thr = warn_if.get("trace_width_mm_lte")
    if thr is not None:
        thin = [t for t in pcb.tracks
                if t.get("width") and float(t["width"]) <= float(thr) + _EPS]
        if thin:
            wmin = min(float(t["width"]) for t in thin)
            out(Finding("FAB_COST_TRACE_WIDTH", Severity.WARNING,
                        f"{len(thin)} track segment(s) at width <= {thr}mm "
                        f"(min {wmin:g}mm) — multilayer fine-line surcharge"))
    return findings


# --------------------------------------------------------------------------- #
# order manifest — declared purchase intent, never guessed from the PCB
# --------------------------------------------------------------------------- #
_ORDER_REQUIRED = (
    "delivery_format",     # single | panel
    "design_count",
    "rush",
    "surface_finish",      # HASL_LF | ENIG | ...
    "via_covering",        # tented | untented | plugged | ...
    "board_material",
    "copper_weight_oz",
)


def load_order(path: os.PathLike | str) -> dict:
    import tomllib
    from pathlib import Path

    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail("BAD_CONFIG", f"order manifest {path}: {exc}")


def check_order(order: dict, profile: dict | None = None) -> list[Finding]:
    """Validate an order manifest's completeness + profile consistency."""
    findings: list[Finding] = []
    out = findings.append
    missing = [k for k in _ORDER_REQUIRED if k not in order]
    if missing:
        out(Finding("ORDER_INCOMPLETE", Severity.ERROR,
                    "order manifest is missing required field(s): "
                    + ", ".join(missing) + " — these are pricing inputs the "
                    "tool must not guess"))
    if order.get("surface_finish") == "ENIG":
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    "surface_finish ENIG: exposed-copper-area surcharge rules "
                    "apply — review against the vendor calculator (akcli does "
                    "not compute mask-open copper area yet)"))
    if order.get("delivery_format") == "panel":
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    "delivery_format panel: V-Cut/mouse-bite and per-design "
                    "surcharge rules apply — review panel drawing against the "
                    "vendor calculator"))
    if int(order.get("design_count") or 1) > 1:
        out(Finding("ORDER_REVIEW_REQUIRED", Severity.WARNING,
                    f"design_count {order.get('design_count')}: multi-design "
                    "gerbers are surcharged — declare per vendor rules"))
    if profile is not None:
        want = (profile.get("via") or {}).get("covering")
        got = order.get("via_covering")
        if want and got and want != got:
            out(Finding("ORDER_PROFILE_CONFLICT", Severity.ERROR,
                        f"order declares via_covering {got!r} but profile "
                        f"{profile.get('id')} is built for {want!r}"))
        want_thick = (profile.get("stackup") or {}).get("thickness_mm")
        got_thick = order.get("thickness_mm")
        if want_thick is not None and got_thick is not None \
                and abs(float(got_thick) - float(want_thick)) > 0.011:
            out(Finding("ORDER_PROFILE_CONFLICT", Severity.ERROR,
                        f"order thickness {got_thick}mm != profile "
                        f"{want_thick}mm"))
    return findings


# --------------------------------------------------------------------------- #
# explain — finding code -> profile rule + evidence
# --------------------------------------------------------------------------- #
_EXPLAIN = {
    "FAB_VIA_PAID_PROCESS": (
        "via", "Vias below the profile's min_drill_mm/min_pad_mm fall into the "
        "vendor's paid small-via process. Fix: enlarge to at least the free "
        "minimum (preferably min_drill + 2*preferred_annular)."),
    "FAB_VIA_TENTED_TOO_BIG": (
        "via", "The declared via covering is tented; the vendor only tents "
        "drills up to max_tented_drill_mm. Fix: shrink the drill or change "
        "the covering option (which changes pricing)."),
    "FAB_VIA_IN_PAD": (
        "via", "Via-in-pad requires plugged/capped vias (a paid process) or "
        "causes solder wicking. Fix: move the via out of the pad, or register "
        "a thermal_via exception with owner+reason for exposed pads."),
    "FAB_VIA_TYPE_FORBIDDEN": (
        "via", "Blind/buried/micro vias are excluded by this profile "
        "(forbid_blind_buried). They require a different, costlier stackup."),
    "FAB_STACKUP_MISMATCH": (
        "stackup", "The board's layer count/thickness drifted from the "
        "profile. Either the board or the chosen profile revision is wrong."),
    "FAB_COST_BOARD_LENGTH": ("cost.warn_if", "Long boards are surcharged."),
    "FAB_COST_BOARD_AREA": ("cost.warn_if", "Large boards are surcharged."),
    "FAB_COST_DRILL_DENSITY": ("cost.warn_if", "High drill density is surcharged."),
    "FAB_COST_TRACE_WIDTH": ("cost.warn_if", "Fine multilayer traces are surcharged."),
    "ORDER_INCOMPLETE": (
        "order", "The order manifest is purchase intent — akcli never derives "
        "delivery format, finish, or covering from the PCB. Declare them."),
}


def explain(code: str, profile: dict | None = None) -> str | None:
    entry = _EXPLAIN.get(code)
    if entry is None:
        return None
    section, text = entry
    lines = [f"{code}", f"  profile section: [{section}]", f"  {text}"]
    if profile is not None:
        src = profile.get("source") or {}
        for url in src.get("urls") or []:
            lines.append(f"  source: {url} (retrieved {src.get('retrieved_at', '?')})")
        lines.append(f"  profile: {profile.get('id')}")
    return "\n".join(lines)
