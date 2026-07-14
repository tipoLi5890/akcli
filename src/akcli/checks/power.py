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
* All findings are advisory hints for a human/agent; nothing here mutates ``sch``.
"""

from __future__ import annotations

import re

from ..config import Config
from ..model import Component, Net, Schematic
from ..report import Finding, Severity
from ._rails import implied_voltage as _implied_voltage, norm as _norm

# --- Finding codes (stable, machine-readable) -------------------------------
POWER_RAIL = "POWER_RAIL"                       # INFO: a rail + its consumers
POWER_GROUND = "POWER_GROUND"                   # INFO: ground net summary
POWER_NO_RAILS = "POWER_NO_RAILS"               # NOTE: nothing rail-like found
POWER_NO_DECOUPLING = "POWER_NO_DECOUPLING"     # WARNING: IC rail w/o bypass cap
POWER_RAIL_NOT_FOUND = "POWER_RAIL_NOT_FOUND"   # WARNING: config rail absent
POWER_VOLTAGE_MISMATCH = "POWER_VOLTAGE_MISMATCH"  # WARNING: name volts != config
POWER_CURRENT_BUDGET = "POWER_CURRENT_BUDGET"   # NOTE: summed annotated current

# Common rail names that carry no parseable voltage but are unambiguously power.
_POWER_NAMES: frozenset[str] = frozenset(
    {
        "VCC", "VDD", "VBAT", "VBUS", "VIN", "VOUT", "VREF", "VDDA", "VDDIO",
        "AVDD", "DVDD", "VPP", "VEE", "VCCIO", "VSYS", "VDDH", "VDD33", "VDD18",
        "PVDD", "IOVDD", "VCORE", "VANA", "VDIG",
    }
)

# Ground net names.
_GROUND_NAMES: frozenset[str] = frozenset(
    {"GND", "GROUND", "VSS", "VSSA", "AGND", "DGND", "PGND", "GNDA", "GNDD", "EGND", "SGND"}
)

# IC-like designator prefixes (decoupling + budget consumers of interest).
_IC_PREFIXES: frozenset[str] = frozenset({"U", "IC"})
# Capacitor designator prefix (decoupling candidates).
_CAP_PREFIXES: frozenset[str] = frozenset({"C"})

# Parameter keys (lower-cased) that annotate per-component current draw.
_CURRENT_KEYS: frozenset[str] = frozenset(
    {
        "current", "current_ma", "i_ma", "idd_ma", "i_typ_ma", "imax_ma",
        "max_current_ma", "icc_ma", "current_draw_ma", "current_draw",
    }
)


# --- small helpers ----------------------------------------------------------
def _prefix(designator: str) -> str:
    """Leading alpha(/underscore) prefix of a refdes (``$U3`` -> ``U``, ``C1`` -> ``C``)."""
    s = designator.lstrip("$")
    m = re.match(r"[A-Za-z_]+", s)
    return m.group(0).upper() if m else ""


# _norm / _implied_voltage are shared with erc.py — see ._rails.


def _is_ground(name: str | None) -> bool:
    if not name:
        return False
    n = _norm(name)
    if n in _GROUND_NAMES:
        return True
    # GND-suffixed/prefixed buses (e.g. "GND_USB", "USB_GND")
    return n.startswith("GND") or n.endswith("GND")


def _is_power(name: str | None) -> bool:
    """True for a (non-ground) power rail by name heuristics."""
    if not name or _is_ground(name):
        return False
    n = _norm(name)
    if n in _POWER_NAMES:
        return True
    return _implied_voltage(name) is not None


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


def _net_candidate_names(net: Net) -> list[str]:
    """All names that could identify a net: display + aliases + contributing sources."""
    names: list[str] = []
    for x in [net.name, *net.aliases, *net.source_names]:
        if x:
            names.append(x)
    return names


def _consumers(net: Net) -> list[str]:
    """Sorted unique designators with a pin on this net."""
    return sorted({des for des, _pin in net.members})


def _cfg_rail_for_net(net: Net, cfg_rails: list[dict]) -> dict | None:
    """Return the config rail whose ``name`` matches any of the net's names."""
    cand = {_norm(x) for x in _net_candidate_names(net)}
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
        names = _net_candidate_names(net)
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

    # Which rails have a decoupling cap (cap touching the rail AND a ground net)?
    decoupled_rails: set[str] = set()
    rail_name_set = {n.name for n in rail_nets if n.name}
    for comp in sch.components:
        if _prefix(comp.designator) not in _CAP_PREFIXES:
            continue
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
    present = {_norm(x) for net in rail_nets for x in _net_candidate_names(net)}
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
