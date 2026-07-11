"""Electrical rule checks (ERC) (SPEC §3.6).

``run(sch, cfg) -> list[Finding]``.

The single most important correction (SPEC §0 / risk #3): **do NOT key ERC on the
pin electrical type.** Real boards are ~98% ``Passive`` pins, so any rule that needs an ``OUTPUT``/``INPUT``/``POWER`` pin
to fire would be vacuous — it would silently pass every real board. This module
therefore:

* detects **power / ground by NET NAME** (a net is power/ground when any name it
  answers to — display name, alias, or a contributing power-port/label
  ``source_name`` — matches a rail-name set: the configured ``[[rail]]`` names
  plus the common ``GND``/``VCC``/``VDD``/``3V3``/``5V`` … patterns);
* tests **"every IC has a power and a ground connection" by NET IDENTITY** — does
  the component share a net with a detected power net and with a detected ground
  net — never by pin type;
* gates the genuinely type-dependent rules (**driver conflict**, **floating
  input**) behind a **type-confidence** = the fraction of pins that carry a real
  (non-Passive, non-Unspecified) electrical type. When that confidence is low /
  degenerate the findings are **downgraded to low-severity NOTEs** that state why,
  so a mostly-Passive board never emits garbage;
* flags **single-pin nets** as likely-dangling — but honors **No-ERC** markers
  (a designer-blessed point: a geo-match of the pin tip against
  ``sch.no_erc_points`` within grid tolerance) and config ``erc_waivers``;
* reports a net carrying **multiple distinct explicit names** (e.g.
  ``STAT`` ≡ ``LED1_GPIO_RD``) as a low-severity **NOTE**, never an error — the
  ``netbuild`` same-name merge made it one net on purpose;
* flags **unplaced units of multi-unit parts** (KiCad-sourced schematics only:
  the KiCad reader merges every placed unit into one ``Component`` whose pins
  carry ``owner_part_id``, so a never-placed unit contributes no pins at all —
  its gates float silently. Altium attaches every unit's pins regardless of
  placement and its ``PARTCOUNT`` field is unreliable, so the rule would be
  pure noise there).

Nothing here mutates ``sch``; every finding is advisory for a human/agent.
"""

from __future__ import annotations

import re

from ..config import Config
from ..model import Net, Pin, PinType, Schematic
from ..report import Finding, Severity
from ..units import approx_eq, mil_to_nm
from ._rails import implied_voltage as _implied_voltage, norm as _norm, rail_matches as _rail_matches

__all__ = ["run"]

# --- Finding codes (stable, machine-readable; named like power/bom) ----------
ERC_FLOATING_INPUT = "ERC_FLOATING_INPUT"      # input with no driver on its net
ERC_DRIVER_CONFLICT = "ERC_DRIVER_CONFLICT"    # two+ push-pull drivers on one net
ERC_DANGLING_NET = "ERC_DANGLING_NET"          # single-pin net (likely unconnected)
ERC_NO_POWER = "ERC_NO_POWER"                  # IC shares no detected power net
ERC_NO_GROUND = "ERC_NO_GROUND"                # IC shares no detected ground net
ERC_NET_ALIAS = "ERC_NET_ALIAS"                # one net carries multiple names
ERC_UNPLACED_UNIT = "ERC_UNPLACED_UNIT"        # multi-unit part with units never placed

# Waiver ``rule`` token -> finding code (config ``[[erc_waiver]].rule``).
_RULE_TO_CODE: dict[str, str] = {
    "floating_input": ERC_FLOATING_INPUT,
    "driver_conflict": ERC_DRIVER_CONFLICT,
    "dangling_net": ERC_DANGLING_NET,
    "no_power": ERC_NO_POWER,
    "no_ground": ERC_NO_GROUND,
    "net_alias": ERC_NET_ALIAS,
    "unplaced_unit": ERC_UNPLACED_UNIT,
}

# --- rail-name heuristics (shared shape with power.py; net-NAME based) --------
_POWER_NAMES: frozenset[str] = frozenset(
    {
        "VCC", "VDD", "VBAT", "VBUS", "VIN", "VOUT", "VREF", "VDDA", "VDDIO",
        "AVDD", "DVDD", "VPP", "VEE", "VCCIO", "VSYS", "VDDH", "VDD33", "VDD18",
        "PVDD", "IOVDD", "VCORE", "VANA", "VDIG",
    }
)
_GROUND_NAMES: frozenset[str] = frozenset(
    {"GND", "GROUND", "VSS", "VSSA", "AGND", "DGND", "PGND", "GNDA", "GNDD", "EGND", "SGND"}
)

# IC-like designator prefixes (the things that must have power + ground).
_IC_PREFIXES: frozenset[str] = frozenset({"U", "IC"})

# --- electrical-type buckets (only used once type-confidence is sufficient) ---
# Pins that carry real, ERC-meaningful electrical information.
_INFORMATIVE_TYPES: frozenset[PinType] = frozenset(
    {
        PinType.INPUT, PinType.OUTPUT, PinType.BIDIRECTIONAL, PinType.TRI_STATE,
        PinType.POWER_IN, PinType.POWER_OUT, PinType.OPEN_COLLECTOR,
        PinType.OPEN_EMITTER,
    }
)
# Push-pull drivers: two or more on one net is a hard conflict (open-collector /
# open-emitter / tri-state / bidirectional are wired-OR / bus safe and excluded).
_STRONG_DRIVERS: frozenset[PinType] = frozenset({PinType.OUTPUT, PinType.POWER_OUT})
# Anything that can drive a level onto a net (so an input on it is NOT floating).
_DRIVING_TYPES: frozenset[PinType] = frozenset(
    {
        PinType.OUTPUT, PinType.BIDIRECTIONAL, PinType.TRI_STATE, PinType.POWER_OUT,
        PinType.OPEN_COLLECTOR, PinType.OPEN_EMITTER,
    }
)

# Below this fraction of typed pins, type-based rules are demoted to NOTE.
_TYPE_CONFIDENCE_MIN = 0.2
# A No-ERC marker within this distance (mils) of a pin tip suppresses ERC there.
_NO_ERC_TOL_MIL = 25.0  # half of the default 50-mil grid
_NO_ERC_TOL_NM = mil_to_nm(_NO_ERC_TOL_MIL)


# --- small helpers -----------------------------------------------------------
def _prefix(designator: str) -> str:
    """Leading alpha prefix of a refdes (``$U3`` -> ``U``, ``IC2`` -> ``IC``)."""
    m = re.match(r"[A-Za-z]+", designator.lstrip("$"))
    return m.group(0).upper() if m else ""


def _unit_letter(unit: int) -> str:
    """KiCad's display letter for a unit number (1 -> A, 2 -> B, 27 -> AA)."""
    s = ""
    while unit > 0:
        unit, r = divmod(unit - 1, 26)
        s = chr(ord("A") + r) + s
    return s


# _norm / _implied_voltage / _rail_matches are shared with power.py — see ._rails.


def _is_ground(name: str | None) -> bool:
    if not name:
        return False
    n = _norm(name)
    if n in _GROUND_NAMES:
        return True
    return n.startswith("GND") or n.endswith("GND")


def _is_power(name: str | None) -> bool:
    """True for a (non-ground) power rail by name heuristics."""
    if not name or _is_ground(name):
        return False
    n = _norm(name)
    if n in _POWER_NAMES:
        return True
    return _implied_voltage(name) is not None


def _net_names(net: Net) -> list[str]:
    """Every name a net answers to: display + aliases + contributing sources."""
    out: list[str] = []
    for x in [net.name, *net.aliases, *net.source_names]:
        if x:
            out.append(x)
    return out


def _net_label(net: Net) -> str:
    """A human label for a net (its name, or a stable id for an unnamed net)."""
    return net.name if net.name else f"<unnamed {net.stable_id}>"


def _pin_index(sch: Schematic) -> dict[tuple[str, str], Pin]:
    """Map every ``(designator, pin_number)`` to its :class:`model.Pin`."""
    index: dict[tuple[str, str], Pin] = {}
    for comp in sch.components:
        for pin in comp.pins:
            index[(comp.designator, pin.number)] = pin
    return index


def _type_confidence(sch: Schematic) -> tuple[float, int, int]:
    """Fraction of pins carrying a real electrical type. Returns (conf, typed, total)."""
    pins = [p for c in sch.components for p in c.pins]
    total = len(pins)
    typed = sum(1 for p in pins if p.electrical_type in _INFORMATIVE_TYPES)
    conf = (typed / total) if total else 0.0
    return conf, typed, total


class _Waivers:
    """Lookup over config ``erc_waivers`` keyed by (normalized net name, code)."""

    def __init__(self, waivers: list[dict]) -> None:
        self._by_code: dict[str, set[str]] = {}
        for w in waivers or []:
            code = _RULE_TO_CODE.get(str(w.get("rule", "")).strip())
            net = w.get("net")
            if code and net:
                self._by_code.setdefault(code, set()).add(_norm(str(net)))

    def waives(self, code: str, names: set[str]) -> bool:
        """True when any of ``names`` (already normalized) is waived for ``code``."""
        targets = self._by_code.get(code)
        return bool(targets) and bool(targets & names)


def _conf_suffix(low: bool, conf: float) -> str:
    """Trailing explanation appended when a type-based rule is demoted."""
    if not low:
        return ""
    return (
        f" [type-confidence low: only {conf:.0%} of pins carry an electrical type; "
        "this type-based ERC rule is unreliable on a mostly-Passive board — "
        "downgraded to a NOTE]"
    )


# --- main entry point --------------------------------------------------------
def run(sch: Schematic, cfg: Config | None = None) -> list[Finding]:
    """Run electrical rule checks on ``sch``; return advisory :class:`Finding`s."""
    findings: list[Finding] = []
    waivers = _Waivers(list(cfg.erc_waivers) if cfg and cfg.erc_waivers else [])
    cfg_rail_names = {
        _norm(r["name"]) for r in (cfg.rails if cfg and cfg.rails else []) if r.get("name")
    }

    pin_index = _pin_index(sch)
    conf, _typed, _total = _type_confidence(sch)
    low_conf = conf < _TYPE_CONFIDENCE_MIN

    # No-ERC marker coordinates in nm (the reader stores mils, +Y-down canonical).
    no_erc_nm = [(mil_to_nm(x), mil_to_nm(y)) for x, y in sch.no_erc_points]

    def _no_erc_suppressed(pin: Pin | None) -> bool:
        if pin is None or not no_erc_nm:
            return False
        px, py = mil_to_nm(pin.x_mil), mil_to_nm(pin.y_mil)
        return any(
            approx_eq(px, nx, _NO_ERC_TOL_NM) and approx_eq(py, ny, _NO_ERC_TOL_NM)
            for nx, ny in no_erc_nm
        )

    # --- classify nets as power / ground purely by NAME --------------------- #
    power_nets: list[Net] = []
    ground_nets: list[Net] = []
    for net in sch.nets:
        names = _net_names(net)
        if any(_is_ground(x) for x in names):
            ground_nets.append(net)
        elif any(_is_power(x) for x in names) or any(_rail_matches(x, cfg_rail_names) for x in names):
            power_nets.append(net)
    power_ids = {id(n) for n in power_nets}
    ground_ids = {id(n) for n in ground_nets}
    pg_ids = power_ids | ground_ids
    has_power = bool(power_nets)
    has_ground = bool(ground_nets)

    # designator -> nets it has a pin on (for the by-identity IC power/ground rule)
    nets_of_comp: dict[str, list[Net]] = {}
    for net in sch.nets:
        for des, _num in net.members:
            nets_of_comp.setdefault(des, []).append(net)

    # --- per-net rules ------------------------------------------------------ #
    for net in sch.nets:
        names = _net_names(net)
        norm_names = {_norm(x) for x in names}
        label = _net_label(net)

        # (a) net-alias conflict: multiple distinct explicit names -> NOTE, never error.
        distinct = []
        for n in [net.name, *net.aliases]:
            if n and n not in distinct:
                distinct.append(n)
        if len(distinct) > 1 and not waivers.waives(ERC_NET_ALIAS, norm_names):
            findings.append(
                Finding(
                    ERC_NET_ALIAS,
                    Severity.NOTE,
                    f"net carries {len(distinct)} explicit names "
                    f"({', '.join(distinct)}); merged into one net by name "
                    "(informational, not an error)",
                    refs=list(distinct),
                )
            )

        member_pins = [
            (ref, pin_index.get(ref))
            for ref in net.members
        ]

        # (b) dangling single-pin net (robust; not type-gated). Respect No-ERC,
        #     explicit no-connect pins, and waivers.
        if len(net.members) == 1:
            ref, pin = member_pins[0]
            ref_str = f"{ref[0]}.{ref[1]}"
            suppress = (
                (pin is not None and pin.electrical_type is PinType.NO_CONNECT)
                or _no_erc_suppressed(pin)
                or waivers.waives(ERC_DANGLING_NET, norm_names | {_norm(ref[0])})
            )
            if not suppress:
                findings.append(
                    Finding(
                        ERC_DANGLING_NET,
                        Severity.WARNING,
                        f"single-pin net {label}: {ref_str} is the only connection "
                        "(likely dangling / unrouted)",
                        refs=[ref_str],
                    )
                )

        # (c) driver conflict (TYPE-gated): 2+ push-pull drivers on one net.
        strong = [
            (ref, pin)
            for ref, pin in member_pins
            if pin is not None and pin.electrical_type in _STRONG_DRIVERS
        ]
        if len(strong) >= 2 and not waivers.waives(ERC_DRIVER_CONFLICT, norm_names):
            refs = [f"{d}.{n}" for (d, n), _p in strong]
            findings.append(
                Finding(
                    ERC_DRIVER_CONFLICT,
                    Severity.NOTE if low_conf else Severity.WARNING,
                    f"net {label} has {len(strong)} push-pull drivers "
                    f"({', '.join(refs)}) — output contention" + _conf_suffix(low_conf, conf),
                    refs=[*refs, label],
                )
            )

        # (d) floating input (TYPE-gated): an INPUT on a multi-pin, non-rail net
        #     with no driver. Single-pin inputs are covered by the dangling rule.
        if len(net.members) >= 2 and id(net) not in pg_ids:
            has_driver = any(
                pin is not None and pin.electrical_type in _DRIVING_TYPES
                for _ref, pin in member_pins
            )
            if not has_driver and not waivers.waives(ERC_FLOATING_INPUT, norm_names):
                for ref, pin in member_pins:
                    if pin is None or pin.electrical_type is not PinType.INPUT:
                        continue
                    if _no_erc_suppressed(pin):
                        continue
                    ref_str = f"{ref[0]}.{ref[1]}"
                    pname = f" ({pin.name})" if pin.name else ""
                    findings.append(
                        Finding(
                            ERC_FLOATING_INPUT,
                            Severity.NOTE if low_conf else Severity.WARNING,
                            f"{ref_str}{pname} is an INPUT on net {label} with no "
                            "driver (floating input)" + _conf_suffix(low_conf, conf),
                            refs=[ref_str],
                        )
                    )

    # --- per-IC power + ground, by NET IDENTITY (robust; not type-gated) ----- #
    # Only assert an IC is "missing" power/ground when the board actually HAS a
    # power/ground net — otherwise (a board with no power infrastructure at all)
    # flagging every IC is the vacuous garbage the SPEC warns against.
    for comp in sch.components:
        if comp.undesignated or _prefix(comp.designator) not in _IC_PREFIXES:
            continue
        # A "U"-prefixed part with fewer than 3 pins cannot be an IC needing
        # both power and ground -- it is a header/jumper stub (e.g. a 2-pin
        # piezo header designated U9). Flagging those is pure noise.
        if len(comp.pins) < 3:
            continue
        touched = nets_of_comp.get(comp.designator, [])
        des_names = {_norm(comp.designator)}
        if has_power and not any(id(n) in power_ids for n in touched):
            if not waivers.waives(ERC_NO_POWER, des_names):
                findings.append(
                    Finding(
                        ERC_NO_POWER,
                        Severity.WARNING,
                        f"IC {comp.designator} has no connection to any detected "
                        "power net",
                        refs=[comp.designator],
                    )
                )
        if has_ground and not any(id(n) in ground_ids for n in touched):
            if not waivers.waives(ERC_NO_GROUND, des_names):
                findings.append(
                    Finding(
                        ERC_NO_GROUND,
                        Severity.WARNING,
                        f"IC {comp.designator} has no connection to any detected "
                        "ground net",
                        refs=[comp.designator],
                    )
                )

    # --- unplaced units of multi-unit parts (KiCad-sourced only) ------------- #
    # The KiCad reader merges every placed unit of a part into ONE Component
    # whose pins carry owner_part_id = the placed unit (0 = common pins), so a
    # unit that was never placed contributes no pins at all — its gate inputs
    # float and its power pins connect to nothing, invisibly to every net-level
    # rule above. Altium attaches all units' pins to the component record
    # regardless of placement and its PARTCOUNT is unreliably off-by-one, so
    # this rule stays silent for Altium sources.
    if sch.source_format == "kicad":
        for comp in sch.components:
            if comp.undesignated or comp.part_count <= 1:
                continue
            placed = {p.owner_part_id for p in comp.pins if p.owner_part_id >= 1}
            missing = sorted(set(range(1, comp.part_count + 1)) - placed)
            if not missing or not placed:
                continue
            if waivers.waives(ERC_UNPLACED_UNIT, {_norm(comp.designator)}):
                continue
            placed_s = ", ".join(_unit_letter(u) for u in sorted(placed))
            missing_s = ", ".join(_unit_letter(u) for u in missing)
            findings.append(
                Finding(
                    ERC_UNPLACED_UNIT,
                    Severity.WARNING,
                    f"{comp.designator} has {comp.part_count} units but only "
                    f"unit(s) {placed_s} placed — unit(s) {missing_s} missing "
                    "(their pins connect to nothing). Place the missing units "
                    "or tie them off with the terminate_unused_unit macro "
                    "(akcli ops template terminate_unused_unit)",
                    refs=[comp.designator],
                )
            )

    return findings
