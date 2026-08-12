"""Shared classification vocabulary: refdes prefixes, rail names, part roles.

**One implementation, every consumer.** ``checks/`` (ERC, power) and
``review/`` (topology + detectors) and ``sim/`` all have to answer the same
three questions — *what class of part is this refdes?*, *is this net a power
rail / a ground?*, *is this component a TVS / crystal / connector?* — and they
used to answer them with six near-copies that had quietly diverged:

* four of the six ``_prefix`` copies did not strip the ``$`` of a synthesized
  designator (``model.Component.designator`` — ``$U3`` when a source file
  carries no refdes), so every rule keyed on them silently skipped
  undesignated parts;
* ``_is_ground``/``_is_power`` existed verbatim twice (``checks/erc.py`` and
  ``checks/power.py``), with the review layer bound to only one of them;
* TVS/ESD recognition lived in the review tables while ``checks/erc.py``
  demanded a power pin from every ``U``-prefixed part — so an ESD array
  (which has no supply pin by construction) was reported as an unpowered IC.

Placement rule: this module is a LEAF — it imports nothing from ``akcli`` —
so ``checks`` and ``review`` can both depend on it without a cycle (the
review layer used to reach into ``checks.power`` for rail semantics).

Everything here is a *name/string* heuristic. Callers that hold a
:class:`~.model.Component` pass its parts (``designator``, ``library_ref``,
``value``); callers that hold a PCB footprint pass theirs — the classifiers
never import the model.

Boundary note (rail regexes): the trailing assertions use ``(?![0-9A-Za-z])``
rather than ``\\b``, because ``_`` is a regex *word* character, so ``\\b`` does
**not** treat ``V3V3_AUX``'s trailing ``_`` as a boundary — that made
underscore-suffixed rails (``V3V3_AUX``, ``V3V3_IO``) fail to register as
power, producing false ``ERC_NO_POWER``. The digit-``V``-digit pattern
deliberately has *no* leading assertion: in ``V3V3`` the matched ``3`` is
preceded by ``V``, so a leading boundary would wrongly reject it.
"""

from __future__ import annotations

import re

__all__ = [
    "refdes_prefix", "split_refdes",
    "natkey",
    "norm",
    "implied_voltage",
    "rail_matches",
    "POWER_NAMES",
    "GROUND_NAMES",
    "SIGNAL_SUFFIXES",
    "STAGE_SUFFIXES",
    "is_stage_name",
    "is_ground",
    "is_power",
    "power_root",
    "TVS_KEYWORDS",
    "CONNECTOR_PREFIXES",
    "CONNECTOR_KEYWORDS",
    "CRYSTAL_PREFIXES",
    "CRYSTAL_KEYWORDS",
    "PASSIVE_TIE_PREFIXES",
    "is_tvs",
    "is_crystal",
    "is_connector",
]

# --------------------------------------------------------------------------- #
# reference designators
# --------------------------------------------------------------------------- #
_PREFIX_RX = re.compile(r"[A-Za-z]+")


_SPLIT_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def split_refdes(ref: str) -> tuple[str, int] | None:
    """``(alpha-prefix, int-suffix)`` of a SIMPLE refdes, or None if compound.

    The second refdes question (the first is :func:`refdes_prefix`): a
    sortable sequence key.  ``R12`` -> ``("R", 12)``; a compound manual name
    (``J_USB_C``) or a synthesized ``$U7`` returns None — such refs have no
    numeric sequence, so gap/duplicate checks skip them.  Case is preserved
    (``refdes_prefix`` upper-cases; a sequence key must not fold ``r1``/``R1``
    onto each other silently — the caller decides).
    """
    m = _SPLIT_RE.match(ref.strip())
    return (m.group(1), int(m.group(2))) if m else None


def refdes_prefix(designator: str | None) -> str:
    """Leading alpha run of a refdes, upper-cased (``$U3`` -> ``U``, ``IC2`` -> ``IC``).

    The leading ``$`` of a **synthesized** designator (``$U7`` — what the
    readers assign when a source file names no reference; see
    :class:`~.model.Component`) is stripped first: a rule that classifies by
    prefix must see the same class for a placed part whether or not its
    designator survived the import.

    The prefix is the FULL leading letter run (``FB1`` -> ``FB``, never
    ``F``), so a ferrite bead is not a fuse and ``U_MAIN1`` -> ``U`` (the
    underscore terminates the run: a refdes class is letters only, and the
    previous ``[A-Za-z_]+`` copy in ``checks/power.py`` made ``U_MAIN1``
    classify as ``U_MAIN`` — i.e. as no known class at all).
    """
    m = _PREFIX_RX.match((designator or "").strip().lstrip("$"))
    return m.group(0).upper() if m else ""


_NAT_RE = re.compile(r"(\d+)")


def natkey(s: str) -> tuple[int | str, ...]:
    """Natural-sort key: digit runs compare numerically (``U2`` < ``U10``).

    The ordering every designator/pin listing sorts by (``intentgen``,
    ``commands.doc``, ``arrange.propose``): it decides op ORDER in generated
    op-lists, so the three former copies had to agree byte-for-byte — one
    definition here is how they keep agreeing.
    """
    return tuple(int(part) if part.isdigit() else part
                 for part in _NAT_RE.split(s))


# --------------------------------------------------------------------------- #
# rail / net names
# --------------------------------------------------------------------------- #
_DIGIT_V_DIGIT = re.compile(r"(\d+)V(\d+)(?![0-9A-Za-z])")          # 3V3 / 1V8 / V3V3
_TRAILING_V = re.compile(r"(\d+(?:\.\d+)?)V(?![0-9A-Za-z])")        # 5V / 3.3V / 12V
_LEADING_V = re.compile(r"(?<![0-9A-Za-z])V(\d+(?:\.\d+)?)(?![0-9A-Za-z])")  # V5 / V3.3

# Common rail names that carry no parseable voltage but are unambiguously power.
POWER_NAMES: frozenset[str] = frozenset(
    {
        "VCC", "VDD", "VBAT", "VBUS", "VIN", "VOUT", "VREF", "VDDA", "VDDIO",
        "AVDD", "DVDD", "VPP", "VEE", "VCCIO", "VSYS", "VDDH", "VDD33", "VDD18",
        "PVDD", "IOVDD", "VCORE", "VANA", "VDIG",
    }
)

# Ground net names.
GROUND_NAMES: frozenset[str] = frozenset(
    {"GND", "GROUND", "VSS", "VSSA", "AGND", "DGND", "PGND", "GNDA", "GNDD",
     "EGND", "SGND"}
)

# Tokens that turn a rail-rooted name into a SIGNAL about that rail rather
# than the rail itself: ``VBUS_DET`` is a detect line, ``VIN_SENSE`` a divider
# tap, ``VBAT_EN`` a control. A derived name carrying any of these is NOT a
# power net (the derived-rail rule below would otherwise swallow half the
# supervisory signals on a PMIC board).
SIGNAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "DET", "DETECT", "SENSE", "SENSING", "SNS", "MON", "MONITOR", "MEAS",
        "ADC", "FB", "FEEDBACK", "PG", "PGOOD", "GOOD", "OK", "FLT", "FAULT",
        "ERR", "ERROR", "ALERT", "INT", "IRQ", "STAT", "STATUS", "EN",
        "ENABLE", "DIS", "DISABLE", "ON", "OFF", "CTRL", "CTL", "SEL", "RST",
        "RESET", "TRIP", "OC", "OV", "UV", "TAP", "DIV", "CMP", "COMP", "PWM",
    }
)

# Tokens that mark a DOWNSTREAM STAGE of a rail: ``VBUS_FUSED`` is still the
# VBUS rail, but it is the copper *after* the fuse, so it is not where the
# board's power ENTRY is. Rules that reason about the entry point (input
# fusing, reverse-polarity) must not treat a stage name as a fresh entry, or
# every protection element reports as missing on the segment behind it.
STAGE_SUFFIXES: frozenset[str] = frozenset(
    {"FUSED", "FUSE", "PROT", "PROTECTED", "FILT", "FILTERED", "POST", "SW",
     "SEC", "SWITCHED", "GATED"}
)

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z]+")


def norm(name: str) -> str:
    """Normalize a net/rail name for matching: upper-case, drop a leading ``+``."""
    return name.upper().lstrip("+").strip()


def implied_voltage(name: str | None) -> float | None:
    """Best-effort voltage implied by a rail name (``+3V3`` -> 3.3, ``5V`` -> 5.0)."""
    if not name:
        return None
    s = norm(name)
    m = _DIGIT_V_DIGIT.search(s)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            return None
    m = _TRAILING_V.search(s)
    if m:
        return float(m.group(1))
    m = _LEADING_V.search(s)
    if m:
        return float(m.group(1))
    return None


def rail_matches(net_name: str, cfg_rail_names: set[str]) -> bool:
    """True if ``net_name`` is a configured rail, exactly or as a ``<rail>_suffix``.

    So a config ``[[rail]] name = "V3V3"`` covers ``V3V3``, ``V3V3_AUX``, ``V3V3-IO``
    (rail name followed by a ``_``/``-`` separator), not arbitrary substrings.
    """
    n = norm(net_name)
    if n in cfg_rail_names:
        return True
    return any(n.startswith(r + "_") or n.startswith(r + "-") for r in cfg_rail_names)


def power_root(name: str | None) -> str | None:
    """The rail root of a DERIVED power-net name, else ``None``.

    A board almost never names its rails exactly ``VBUS``: the same copper is
    ``VBUS_CONN`` at the receptacle, ``VBUS_FUSED`` after the PTC and
    ``VBUS_PROT`` after the TVS. Exact-set matching saw none of those as
    power, so every IC on them drew a false ``ERC_NO_POWER`` and
    ``check --power`` never listed the rail.

    The rule: the name must SPLIT on a ``_``/``-``/``.`` separator, its first
    token must be a known rail name, and no later token may be a
    :data:`SIGNAL_SUFFIXES` word — ``VBUS_FUSED`` and ``VCC_3V3_A`` are
    rails, ``VBUS_DET`` and ``VIN_SENSE`` are signals about one.

    The separator is required: a glued name (``VBUSOUT``) cannot be told
    apart from an ordinary word that merely starts with a rail name
    (``VBUSY``, ``VINPUT``), and the rail-name set is short enough that
    guessing there buys little and risks a lot.
    """
    if not name:
        return None
    tokens = [t for t in _TOKEN_SPLIT.split(norm(name)) if t]
    if len(tokens) < 2 or tokens[0] not in POWER_NAMES:
        return None
    if any(t in SIGNAL_SUFFIXES for t in tokens[1:]):
        return None
    return tokens[0]


def is_stage_name(name: str | None) -> bool:
    """True when a rail name says it is a DOWNSTREAM STAGE (``VBUS_FUSED``).

    See :data:`STAGE_SUFFIXES`. Used by the power-entry rules to seed only at
    the real entry: the same physical rail is drawn as ``VBUS_CONN`` at the
    receptacle and ``VBUS_FUSED``/``VBUS_PROT`` after each protector, and
    seeding at a stage reports the protector *upstream of it* as missing.
    """
    if not name:
        return False
    tokens = [t for t in _TOKEN_SPLIT.split(norm(name)) if t]
    return any(t in STAGE_SUFFIXES for t in tokens[1:])


def is_ground(name: str | None) -> bool:
    """True for a ground net by name heuristics (``GND``, ``AGND``, ``USB_GND``)."""
    if not name:
        return False
    n = norm(name)
    if n in GROUND_NAMES:
        return True
    # GND-suffixed/prefixed buses (e.g. "GND_USB", "USB_GND")
    return n.startswith("GND") or n.endswith("GND")


def is_power(name: str | None) -> bool:
    """True for a (non-ground) power rail by name heuristics."""
    if not name or is_ground(name):
        return False
    n = norm(name)
    if n in POWER_NAMES:
        return True
    if implied_voltage(name) is not None:
        return True
    return power_root(n) is not None


# --------------------------------------------------------------------------- #
# component roles
# --------------------------------------------------------------------------- #
# TVS / ESD protection parts, matched case-insensitively against lib_id, value
# and (on the PCB side) the footprint name — common vendor family names.
TVS_KEYWORDS: tuple[str, ...] = (
    "tvs", "esd", "pesd", "usblc", "srv05", "sp05", "tpd2", "tpd4", "tpd6",
    "pusb", "ip4220", "d_tvs", "esda",
)

# Connector designator prefixes (industry convention). ``X`` is deliberately
# ABSENT: it collides with the equally conventional crystal prefix (``X1``),
# and a mis-classified crystal cost more than a mis-classified connector —
# the whole crystal rule family skipped the part while the connector rules
# demanded ESD protection on XTAL1/XTAL2. A connector drawn as ``X1`` is
# still caught by its library name (``Connector_…``).
CONNECTOR_PREFIXES: frozenset[str] = frozenset({"J", "P", "CN", "USB", "XP"})
CONNECTOR_KEYWORDS: tuple[str, ...] = ("conn", "usb", "header", "receptacle",
                                       "socket", "jack")

# Crystal / resonator: prefix ``Y`` (IEC 60617) or ``X`` (equally common in
# vendor and JLC libraries).
CRYSTAL_PREFIXES: frozenset[str] = frozenset({"Y", "X"})
CRYSTAL_KEYWORDS: tuple[str, ...] = ("crystal", "xtal", "resonator")

# Two-terminal parts that can TIE a net to a rail (pull-up/pull-down, series
# feed): resistors, resistor networks, ferrites/inductors, fuses. Capacitors
# are absent on purpose — a cap to ground stores charge, it does not drive a
# level onto the net.
PASSIVE_TIE_PREFIXES: frozenset[str] = frozenset({"R", "RN", "L", "FB", "F"})


def _hay(*parts: str | None) -> str:
    return " ".join((p or "").lower() for p in parts)


def is_tvs(designator: str | None = None, lib_id: str | None = None,
           value: str | None = None, footprint: str | None = None) -> bool:
    """True for a TVS / ESD-clamp part, judged by library / value / footprint.

    Keyword-only evidence (no designator prefix): TVS arrays ship as ``D``,
    ``U``, ``ESD`` and ``Z`` alike, so the prefix says nothing.
    """
    del designator                       # role is not prefix-decidable
    hay = _hay(lib_id, value, footprint)
    return any(k in hay for k in TVS_KEYWORDS)


def is_crystal(designator: str | None = None, lib_id: str | None = None,
               value: str | None = None) -> bool:
    """True for a crystal / resonator.

    Ladder: library or value evidence FIRST (``Device:Crystal``,
    ``…_32.768kHz…``), then the designator prefix (``Y``/``X``) as a
    fallback. Library evidence wins, so a part in a ``Connector_*`` library
    is never a crystal however it is designated.
    """
    hay = _hay(lib_id, value)
    if any(k in hay for k in CRYSTAL_KEYWORDS):
        return True
    if any(k in _hay(lib_id) for k in CONNECTOR_KEYWORDS):
        return False
    return refdes_prefix(designator) in CRYSTAL_PREFIXES


def is_connector(designator: str | None = None, lib_id: str | None = None,
                 footprint: str | None = None, value: str | None = None) -> bool:
    """True for a connector / header / receptacle.

    Same ladder as :func:`is_crystal`: library/footprint evidence first, then
    the designator prefix. A part whose library or value says *crystal* is
    never a connector (that mis-classification put ``XTAL1``/``XTAL2`` on the
    "unprotected connector net" report), and neither is a TVS — a
    ``USBLC6``'s library name carries ``usb`` but the part is a clamp, not a
    receptacle.
    """
    if any(k in _hay(lib_id, value) for k in CRYSTAL_KEYWORDS):
        return False
    if is_tvs(designator, lib_id, value, footprint):
        return False
    if any(k in _hay(lib_id, footprint) for k in CONNECTOR_KEYWORDS):
        return True
    return refdes_prefix(designator) in CONNECTOR_PREFIXES
