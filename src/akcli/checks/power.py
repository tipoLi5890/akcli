"""Power-rail analysis check (SPEC §3.6).

``run(sch, cfg) -> list[Finding]`` enumerates power rails from power ports /
net names plus any configured ``[[rail]]`` entries, lists the consumers on each
rail, applies a decoupling-capacitor heuristic per IC power net, sums an optional
current budget when the BOM is annotated, and sanity-checks rail voltages against
the config.

Design notes
------------
* **Net-name based**, never electrical-type based — real boards are overwhelmingly
  Passive pins (see SPEC §3.6 / risk #3), so power/ground are detected from net
  names + names that contributed to a net (``source_names``/``aliases``), which is
  where power-port symbols leave their footprint.
* A *rail* is any named power (non-ground) net, union the config rails (which may
  name a custom rail that the heuristic would miss). Ground nets are reported
  separately and are the reference for the decoupling heuristic.
* ``POWER_RAIL_NO_SOURCE`` reports a rail that is *consumed* (≥2 consumers) but
  that nothing plausibly *feeds*. "Plausibly" is deliberately generous — a
  driving pin type, a ``PWR_FLAG``, a connector/battery/test-point designator,
  a configured ``[[rail]]``, an entry-style name (``VBUS``/``VBAT``/``VIN``…),
  or any of those on a net the rail reaches through series parts — because the
  rule's only job is catching the rail nobody wired at all.
* **DNP parts satisfy nothing**: a Do-Not-Populate bypass cap does not decouple
  a rail (the BOM checks are where DNP parts still count).
* All findings are advisory hints for a human/agent; nothing here mutates ``sch``.
"""

from __future__ import annotations

import re

from ..classify import (
    implied_voltage as _implied_voltage,
    is_ground as _is_ground,
    is_power as _is_power,
    norm as _norm,
    refdes_prefix as _prefix,
)
from ..config import Config
from ..model import Component, Net, PinType, Schematic
from ..report import Finding, Severity

# --- Finding codes (stable, machine-readable) -------------------------------
POWER_RAIL = "POWER_RAIL"                       # INFO: a rail + its consumers
POWER_GROUND = "POWER_GROUND"                   # INFO: ground net summary
POWER_NO_RAILS = "POWER_NO_RAILS"               # NOTE: nothing rail-like found
POWER_NO_DECOUPLING = "POWER_NO_DECOUPLING"     # WARNING: IC rail w/o bypass cap
POWER_RAIL_NOT_FOUND = "POWER_RAIL_NOT_FOUND"   # WARNING: config rail absent
POWER_VOLTAGE_MISMATCH = "POWER_VOLTAGE_MISMATCH"  # WARNING: name volts != config
POWER_CURRENT_BUDGET = "POWER_CURRENT_BUDGET"   # NOTE: summed annotated current
POWER_RAIL_NO_SOURCE = "POWER_RAIL_NO_SOURCE"   # WARNING: rail consumed, never fed

# Rail/ground NAME vocabulary and the ``_is_power``/``_is_ground`` heuristics
# live in ``akcli.classify`` — erc.py, this module and the whole review layer
# share ONE implementation (they used to carry verbatim copies).

# IC-like designator prefixes (decoupling + budget consumers of interest).
_IC_PREFIXES: frozenset[str] = frozenset({"U", "IC"})
# Capacitor designator prefix (decoupling candidates).
_CAP_PREFIXES: frozenset[str] = frozenset({"C"})

# --- "who could be feeding this rail?" (POWER_RAIL_NO_SOURCE) ----------------
# Deliberately GENEROUS: this rule only fires when NOTHING plausible feeds a
# rail, so every doubt is resolved in favour of silence.
#
# Designator prefixes that can bring power in from outside the schematic:
# connectors/headers/sockets (J, P, X, XP, CN), batteries (BT), test points and
# bench-supply pads (TP, G).
_SOURCE_PREFIXES: frozenset[str] = frozenset(
    {"J", "P", "X", "XP", "CN", "BT", "TP", "G"}
)
# Pin types that assert "I drive this net" (never required — most boards are
# all-Passive; see the module note — but conclusive when present).
_SOURCE_PIN_TYPES: frozenset[PinType] = frozenset({PinType.POWER_OUT, PinType.OUTPUT})
# Rail names that ARE the board's input by convention: the source is off-board,
# and a bare label with no symbol is the idiomatic way to draw that.
_ENTRY_NAMES: frozenset[str] = frozenset(
    {"VBUS", "VBAT", "VIN", "DCIN", "VDCIN", "VEXT", "VUSB", "VAC", "VBATT"}
)
# Cap the series-island walk (determinism + no pathological schematic blowup).
_ISLAND_MAX_NETS = 40

# Parameter keys (lower-cased) that annotate per-component current draw.
_CURRENT_KEYS: frozenset[str] = frozenset(
    {
        "current", "current_ma", "i_ma", "idd_ma", "i_typ_ma", "imax_ma",
        "max_current_ma", "icc_ma", "current_draw_ma", "current_draw",
    }
)


# --- small helpers ----------------------------------------------------------
# _prefix / _norm / _implied_voltage / _is_power / _is_ground come from
# ``akcli.classify`` (imported above) — the single source of truth.


def _to_ma(value: object) -> float | None:
    """Parse an annotated current into milliamps (``"12mA"`` -> 12, ``"0.5A"`` -> 500)."""
    s = str(value).strip().lower().replace(" ", "")
    if not s:
        return None
    mult = 1.0
    if s.endswith("ma"):
        s = s[:-2]
    elif s.endswith("a"):
        s = s[:-1]
        mult = 1000.0
    try:
        return float(s) * mult
    except ValueError:
        return None


def _annotated_current_ma(comp: Component) -> float | None:
    for key, val in comp.parameters.items():
        if key.strip().lower() in _CURRENT_KEYS:
            ma = _to_ma(val)
            if ma is not None:
                return ma
    return None


def net_candidate_names(net: Net) -> list[str]:
    """All names that could identify a net: display + aliases + contributing sources."""
    names: list[str] = []
    for x in [net.name, *net.aliases, *net.source_names]:
        if x:
            names.append(x)
    return names


def _consumers(net: Net) -> list[str]:
    """Sorted unique designators with a pin on this net."""
    return sorted({des for des, _pin in net.members})


def _is_pwr_flag(comp: Component) -> bool:
    """A KiCad ``PWR_FLAG`` pseudo-part (``power:PWR_FLAG`` / ``#FLG*``)."""
    return ("pwr_flag" in (comp.library_ref or "").lower()
            or comp.designator.lstrip("$").upper().startswith("#FLG"))


def _entry_named(net: Net) -> bool:
    """True when a net's NAME says it is a board input (``VBUS``, ``VBAT_5V``).

    An off-board source (USB host, battery, DC jack) is very often drawn as a
    bare label with no symbol at all, so an entry-named rail is never reported
    as sourceless — the source is simply not on the schematic.
    """
    for name in net_candidate_names(net):
        n = _norm(name).lstrip("-")
        if n in _ENTRY_NAMES:
            return True
        token = re.split(r"[^0-9A-Za-z]+", n)[0] if n else ""
        if token.upper() in _ENTRY_NAMES:
            return True
    return False


def _net_has_source(net: Net, comps: dict[str, Component],
                    pin_types: dict[tuple[str, str], PinType]) -> bool:
    """True when anything on ``net`` could plausibly be feeding it."""
    for des, num in net.members:
        if _prefix(des) in _SOURCE_PREFIXES:
            return True                     # connector / battery / test point
        comp = comps.get(des)
        if comp is not None and _is_pwr_flag(comp):
            return True                     # designer-asserted power source
        if pin_types.get((des, str(num))) in _SOURCE_PIN_TYPES:
            return True                     # a pin that says it drives
    return False


def _series_island(net: Net, nets_of_comp_all: dict[str, list[Net]],
                   comps: dict[str, Component]) -> list[Net]:
    """``net`` plus every net reachable through FITTED two-terminal parts.

    The "supply island": a rail fed through a fuse/ferrite/diode/resistor is
    sourced by whatever feeds the OTHER side, so the source search must cross
    series elements. Ground is never crossed (a cap or a bottom divider leg is
    a load, not a path) and DNP parts are not crossed at all.
    """
    island = [net]
    seen = {id(net)}
    frontier = [net]
    while frontier and len(island) < _ISLAND_MAX_NETS:
        cur = frontier.pop(0)
        for des, _num in sorted(cur.members):
            comp = comps.get(des)
            if comp is not None and comp.dnp:
                continue                    # DNP: no series path through it
            touched = nets_of_comp_all.get(des)
            if touched is None or len(touched) != 2:
                continue                    # not a two-terminal series part
            if touched[0] is cur:
                other = touched[1]
            elif touched[1] is cur:
                other = touched[0]
            else:
                continue
            if id(other) in seen:
                continue
            if any(_is_ground(x) for x in net_candidate_names(other)):
                continue
            seen.add(id(other))
            island.append(other)
            frontier.append(other)
    return island


def _cfg_rail_for_net(net: Net, cfg_rails: list[dict]) -> dict | None:
    """Return the config rail whose ``name`` matches any of the net's names."""
    cand = {_norm(x) for x in net_candidate_names(net)}
    for rail in cfg_rails:
        rn = rail.get("name")
        if rn and _norm(rn) in cand:
            return rail
    return None


# --- main entry point -------------------------------------------------------
def run(sch: Schematic, cfg: Config | None = None) -> list[Finding]:
    """Analyze power distribution; return advisory :class:`Finding` objects."""
    findings: list[Finding] = []
    cfg_rails: list[dict] = list(cfg.rails) if cfg and cfg.rails else []

    named_nets = [n for n in sch.nets if n.name]

    # Classify named nets into ground vs power rails (config rails force "power").
    ground_nets: list[Net] = []
    rail_nets: list[Net] = []
    for net in named_nets:
        names = net_candidate_names(net)
        if any(_is_ground(x) for x in names):
            ground_nets.append(net)
        elif any(_is_power(x) for x in names) or _cfg_rail_for_net(net, cfg_rails):
            rail_nets.append(net)

    rail_nets.sort(key=lambda n: n.name or "")
    ground_nets.sort(key=lambda n: n.name or "")

    ground_name_set = {n.name for n in ground_nets if n.name}

    # Component lookup + per-component nets-touched index.
    comp_by_des: dict[str, Component] = {c.designator: c for c in sch.components}
    nets_of_comp: dict[str, set[str]] = {}
    for net in named_nets:
        if not net.name:
            continue
        for des, _pin in net.members:
            nets_of_comp.setdefault(des, set()).add(net.name)

    # Indexes for the sourceless-rail rule: every net a designator touches
    # (unnamed nets included — a series part can bridge into one), and the pin
    # electrical types (used only as positive evidence, never as a requirement).
    nets_of_comp_all: dict[str, list[Net]] = {}
    for net in sch.nets:
        for des, _pin in net.members:
            entry = nets_of_comp_all.setdefault(des, [])
            if not any(n is net for n in entry):
                entry.append(net)
    pin_types: dict[tuple[str, str], PinType] = {
        (c.designator, str(p.number)): p.electrical_type
        for c in sch.components for p in c.pins
    }

    # Which rails have a decoupling cap (cap touching the rail AND a ground net)?
    decoupled_rails: set[str] = set()
    rail_name_set = {n.name for n in rail_nets if n.name}
    for comp in sch.components:
        if _prefix(comp.designator) not in _CAP_PREFIXES:
            continue
        if comp.dnp:
            continue        # DNP: drawn but never fitted — decouples nothing
        touched = nets_of_comp.get(comp.designator, set())
        if not touched & ground_name_set:
            continue
        for r in touched & rail_name_set:
            decoupled_rails.add(r)

    # ----- ground summary -----
    if ground_nets:
        findings.append(
            Finding(
                POWER_GROUND,
                Severity.INFO,
                f"{len(ground_nets)} ground net(s): "
                + ", ".join(n.name for n in ground_nets),
                refs=[n.name for n in ground_nets],
            )
        )

    # ----- per-rail enumeration + decoupling + current budget -----
    if not rail_nets and not cfg_rails:
        findings.append(
            Finding(
                POWER_NO_RAILS,
                Severity.NOTE,
                "no power rails detected (no power ports/rail-named nets and no "
                "configured [[rail]] entries) — power analysis is vacuous",
                refs=[],
            )
        )

    for net in rail_nets:
        consumers = _consumers(net)
        cfg_rail = _cfg_rail_for_net(net, cfg_rails)
        v_cfg = cfg_rail.get("voltage") if cfg_rail else None
        v_name = _implied_voltage(net.name)
        v_txt = ""
        if v_cfg is not None:
            v_txt = f" @ {v_cfg}V (config)"
        elif v_name is not None:
            v_txt = f" @ ~{v_name}V (from name)"

        findings.append(
            Finding(
                POWER_RAIL,
                Severity.INFO,
                f"rail {net.name!r}{v_txt}: {len(consumers)} consumer(s): "
                + ", ".join(consumers),
                refs=[net.name],
            )
        )

        # Voltage sanity: name-implied voltage vs configured voltage.
        if cfg_rail is not None and v_cfg is not None and v_name is not None:
            tol = cfg_rail.get("tolerance_pct")
            if not _voltage_ok(v_name, float(v_cfg), tol):
                findings.append(
                    Finding(
                        POWER_VOLTAGE_MISMATCH,
                        Severity.WARNING,
                        f"rail {net.name!r} name implies ~{v_name}V but config "
                        f"declares {v_cfg}V",
                        refs=[net.name],
                    )
                )

        # Sourceless rail: consumed by ≥2 parts, fed by nothing we can find.
        # Every doubt silences this rule (see _net_has_source / _series_island /
        # _entry_named): a configured [[rail]] is an intent declaration, an
        # entry-named rail arrives from off-board, and a rail fed through a
        # series element inherits its upstream source.
        if (len(consumers) >= 2 and cfg_rail is None
                and not any(_entry_named(n) or _net_has_source(n, comp_by_des, pin_types)
                            for n in _series_island(net, nets_of_comp_all,
                                                    comp_by_des))):
            findings.append(
                Finding(
                    POWER_RAIL_NO_SOURCE,
                    Severity.WARNING,
                    f"rail {net.name!r} has {len(consumers)} consumer(s) "
                    f"({', '.join(consumers)}) but no source: nothing on it (or "
                    "on the nets it reaches through series parts) is a "
                    "power_out/output pin, a PWR_FLAG, or a "
                    "connector/battery/test point — check that the supply is "
                    "actually wired",
                    refs=[net.name, *consumers],
                )
            )

        # Decoupling heuristic: ICs on this rail without any bypass cap.
        ic_consumers = [
            d for d in consumers if _prefix(d) in _IC_PREFIXES
        ]
        if ic_consumers and net.name not in decoupled_rails:
            findings.append(
                Finding(
                    POWER_NO_DECOUPLING,
                    Severity.WARNING,
                    f"rail {net.name!r} powers IC(s) {', '.join(ic_consumers)} but "
                    "has no decoupling capacitor (a cap tied between this rail and "
                    "ground) — check bypass caps",
                    refs=[net.name, *ic_consumers],
                )
            )

        # Optional current budget (only when at least one consumer is annotated).
        total_ma = 0.0
        contributors: list[str] = []
        for des in consumers:
            comp = comp_by_des.get(des)
            if comp is None:
                continue
            ma = _annotated_current_ma(comp)
            if ma is not None:
                total_ma += ma
                contributors.append(f"{des}={ma:g}mA")
        if contributors:
            findings.append(
                Finding(
                    POWER_CURRENT_BUDGET,
                    Severity.NOTE,
                    f"rail {net.name!r} annotated current budget ~{total_ma:g}mA "
                    f"({', '.join(contributors)})",
                    refs=[net.name],
                )
            )

    # ----- config rails declared but not present in the schematic -----
    present = {_norm(x) for net in rail_nets for x in net_candidate_names(net)}
    for rail in cfg_rails:
        rn = rail.get("name")
        if rn and _norm(rn) not in present:
            findings.append(
                Finding(
                    POWER_RAIL_NOT_FOUND,
                    Severity.WARNING,
                    f"configured rail {rn!r} is not present in the schematic "
                    "(no net or power port carries that name)",
                    refs=[rn],
                )
            )

    return findings


def _voltage_ok(v_name: float, v_cfg: float, tol_pct: object) -> bool:
    """True when a name-implied voltage agrees with the configured voltage.

    Uses the rail's ``tolerance_pct`` if a sane number is given, otherwise a small
    default tolerance to absorb naming rounding (e.g. ``3V3`` -> 3.3 vs 3.30).
    """
    try:
        tol = float(tol_pct) if tol_pct is not None else 2.0
    except (TypeError, ValueError):
        tol = 2.0
    if tol < 2.0:
        tol = 2.0  # never tighter than naming rounding noise
    allowed = abs(v_cfg) * (tol / 100.0)
    # always allow at least 0.05V of slop for the "3V3" rounding case
    allowed = max(allowed, 0.05)
    return abs(v_name - v_cfg) <= allowed
