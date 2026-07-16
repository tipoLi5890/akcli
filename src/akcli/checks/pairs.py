"""Differential-pair and bus continuity checks (ROADMAP v0.9).

``run(sch, cfg) -> list[Finding]`` over the built net model — pure NAME-level
continuity (the review EMC layer owns geometric skew):

* **Broken differential pair** (``PAIR_INCOMPLETE``, WARNING): a net named
  like one side of a pair (``FOO_P``, ``USB_D+``, ``CAN_H``) whose partner
  net does not exist. **Asymmetric by design**: only the *positive/high* side
  alone fires — a lone ``_N``/``_L``/``-`` name is the active-low signal
  convention (``RESET_N``, ``CS_N``), not a broken pair.
* **Pair pin-count mismatch** (``PAIR_PIN_MISMATCH``, NOTE): both sides
  exist but carry different pin counts — legal (test points, terminations)
  but worth a look.
* **Bus index gap** (``BUS_GAP``): a numbered net family (``D0..D7``,
  ``ADDR3``) with holes between its lowest and highest index. A family not
  starting at 0 is fine (``A8..A15`` on a memory bus); only *internal* gaps
  are flagged — NOTE for small families, WARNING once the family has >= 4
  members. Digit-only or single-member families never fire.

``[check]`` config: ``pairs = false`` removes the family from the default
`check` run, ``pair_suffixes = [["_P","_N"], ...]`` replaces the suffix table
(first entry = the side that fires alone), ``bus_min_family`` (default 2)
raises the family-size threshold. Findings are waivable like any other via
``[[waiver]]``.
"""

from __future__ import annotations

import re

from ..config import Config
from ..model import Net, Schematic
from ..report import Finding, Severity

__all__ = ["run"]

PAIR_INCOMPLETE = "PAIR_INCOMPLETE"
PAIR_PIN_MISMATCH = "PAIR_PIN_MISMATCH"
BUS_GAP = "BUS_GAP"

# (positive/high suffix, negative/low suffix) — the FIRST side alone fires;
# the second alone is presumed active-low naming and stays silent.
_DEFAULT_PAIR_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_P", "_N"),
    ("_DP", "_DN"),
    ("_H", "_L"),
    ("+", "-"),
)

# numbered family: prefix must end in a letter or underscore so pure numerals
# ("3V3" -> prefix "3V"? no: "3V" ends in a letter, allowed but needs >= 2
# same-prefix members to matter) and the index is the trailing decimal run.
_FAMILY_RE = re.compile(r"^(.*[A-Za-z_])(\d+)$")

_BUS_WARN_FAMILY = 4  # families this large get WARNING gaps, smaller get NOTE


def _pair_suffixes(cfg: Config | None) -> tuple[tuple[str, str], ...]:
    raw = (cfg.check if cfg else {}).get("pair_suffixes")
    if raw:
        return tuple((str(a), str(b)) for a, b in raw)
    return _DEFAULT_PAIR_SUFFIXES


def _split_suffix(name: str, suffix: str) -> str | None:
    """The base of ``name`` for a case-insensitive ``suffix`` match, or None."""
    if len(name) <= len(suffix):
        return None
    if not name.upper().endswith(suffix.upper()):
        return None
    return name[: -len(suffix)]


def run(sch: Schematic, cfg: Config | None = None) -> list[Finding]:
    findings: list[Finding] = []
    named: dict[str, Net] = {}
    for net in sch.nets:
        for name in [net.name, *net.aliases]:
            if name:
                named.setdefault(name.upper(), net)

    def _lookup(name: str) -> Net | None:
        return named.get(name.upper())

    # --- differential pairs -------------------------------------------------
    seen_bases: set[tuple[str, str]] = set()
    for net in sch.nets:
        if not net.name:
            continue
        for pos_sfx, neg_sfx in _pair_suffixes(cfg):
            base = _split_suffix(net.name, pos_sfx)
            if base is None:
                continue
            key = (base.upper(), pos_sfx.upper())
            if key in seen_bases:
                continue
            seen_bases.add(key)
            partner_name = base + neg_sfx
            partner = _lookup(partner_name)
            if partner is None:
                findings.append(
                    Finding(
                        PAIR_INCOMPLETE,
                        Severity.WARNING,
                        f"net {net.name!r} looks like one side of a "
                        f"differential pair but {partner_name!r} does not "
                        "exist — broken pair, or rename if single-ended",
                        refs=[net.name],
                    )
                )
            elif partner is not net and len(partner.members) != len(net.members):
                findings.append(
                    Finding(
                        PAIR_PIN_MISMATCH,
                        Severity.NOTE,
                        f"pair {net.name!r}/{partner_name!r} pin counts differ "
                        f"({len(net.members)} vs {len(partner.members)}) — "
                        "check terminations/test points are intentional",
                        refs=[net.name, partner_name],
                    )
                )
            break  # first matching suffix owns the name

    # --- bus index gaps ------------------------------------------------------
    min_family = int((cfg.check if cfg else {}).get("bus_min_family", 2) or 2)
    families: dict[str, dict[int, str]] = {}
    for net in sch.nets:
        if not net.name:
            continue
        m = _FAMILY_RE.match(net.name)
        if not m:
            continue
        prefix, idx = m.group(1), int(m.group(2))
        families.setdefault(prefix.upper(), {})[idx] = net.name
    for prefix_key in sorted(families):
        members = families[prefix_key]
        if len(members) < max(2, min_family):
            continue
        lo, hi = min(members), max(members)
        missing = [i for i in range(lo, hi + 1) if i not in members]
        if not missing:
            continue
        prefix = re.sub(r"\d+$", "", members[lo])  # display prefix, original case
        sev = Severity.WARNING if len(members) >= _BUS_WARN_FAMILY else Severity.NOTE
        shown = ", ".join(f"{prefix}{i}" for i in missing[:8])
        if len(missing) > 8:
            shown += f", … ({len(missing)} total)"
        findings.append(
            Finding(
                BUS_GAP,
                sev,
                f"bus family {prefix}{lo}..{prefix}{hi} has "
                f"{len(missing)} missing index(es): {shown} — gap in a "
                "numbered net family (intentional? waive it)",
                refs=sorted(members.values()),
            )
        )
    return findings
