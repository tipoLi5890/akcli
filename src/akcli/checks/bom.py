"""BOM hygiene checks (SPEC §3.6).

``run(sch)`` inspects only the *component* layer of a :class:`model.Schematic`
(no net inference needed) and reports five classes of issue:

* **duplicate designator** -- two or more genuinely distinct components share a
  refdes. Multi-unit parts (several placements of one physical component sharing
  a ``UniqueId``) are *deduped*, not flagged.
* **refdes gap** -- a hole in a numeric-suffixed refdes sequence (e.g. ``R7`` and
  ``R12`` present but ``R8..R11`` missing). Gap detection runs **only within a
  numeric-suffixed prefix**: compound / manually-named refs such as ``J_USB_C``
  do not parse to ``(prefix, int)`` and are skipped, and a lone member (``X3``
  with no ``X1``/``X2``) has an empty min..max range so it never reports a gap.
* **corrupt text** -- a component whose value or parameters contain the U+FFFD
  replacement character. The corruption is baked into the ``.SchDoc`` at export
  time (typically a GBK/CP125x-locale value pushed through a lossy UTF-8 decode
  by the authoring/import tool -- both the ANSI field and its ``%UTF8%`` twin
  carry the damage), so **no decoder can recover it**; the fix is to re-export
  from a tool that preserves the text. One aggregated NOTE per schematic.
* **missing value** / **missing footprint** -- a component with no value or no
  footprint string. A component whose ``value`` is empty but that carries a
  concrete *part identity* -- a part-number parameter (``Manufacturer Part``,
  ``LCSC Part Name``, ...) or a digit-bearing ``library_ref`` (``AO2301``,
  ``MCP73831T-2ACI/OT``) -- is **not** flagged: Altium designs sourced from
  vendor libraries routinely leave Comment/Value blank and identify the part
  by its library reference, and flagging those drowned real findings in noise.

Synthesized (undesignated, ``$U<idx>``) components carry no real refdes and are
excluded from every BOM check; their count is already surfaced in the schematic
metadata header so the report is never vacuously clean.
"""

from __future__ import annotations

import re

from ..model import Component, Schematic
from ..report import Finding, Severity, anchor

__all__ = ["run"]

# A "simple" / numeric-suffixed refdes: one or more letters then digits, nothing
# else (``R12`` -> ("R", 12), ``U3`` -> ("U", 3)). Compound refs like ``J_USB_C``
# (an underscore-bearing manual name) do NOT match and are skipped from gaps.
_REFDES_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _parse_refdes(ref: str) -> tuple[str, int] | None:
    """Split a refdes into ``(alpha-prefix, int-suffix)`` or ``None`` if compound."""
    m = _REFDES_RE.match(ref.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _clean(s: str | None) -> str | None:
    """Normalize a value/footprint string: blank/whitespace -> ``None``."""
    if s is None:
        return None
    s = s.strip()
    return s or None


# Parameter names that carry a concrete part number (vendor-library exports).
_PART_ID_PARAMS: tuple[str, ...] = (
    "Manufacturer Part",
    "MPN",
    "LCSC Part Name",
    "LCSC Part",
    "Supplier Part",
    "Part Number",
)


def _part_identity(comp: Component) -> str | None:
    """A concrete part identity that substitutes for a blank value, or ``None``.

    A part-number parameter wins; else a ``library_ref`` that contains a digit
    (real part numbers do -- ``AO2301``; generic symbols -- ``R``, ``LED`` --
    do not, so those still report a missing value).
    """
    params = comp.parameters or {}
    for key in _PART_ID_PARAMS:
        v = _clean(params.get(key))
        if v:
            return v
    lib = _clean(comp.library_ref)
    if lib and any(ch.isdigit() for ch in lib):
        return lib
    return None


def _identity(comp: Component) -> object:
    """A dedup key: the ``UniqueId`` if present, else a per-object distinct key.

    Components sharing a non-empty ``UniqueId`` are units of one physical part
    (deduped); a missing id makes each placement its own identity so genuine
    duplicate refdes are still caught.
    """
    uid = _clean(comp.unique_id)
    return uid if uid is not None else ("__no_uid__", id(comp))


def _pos(comp: Component) -> tuple[float, float]:
    return (comp.x_mil, comp.y_mil)


def _real_components(sch: Schematic) -> list[Component]:
    """Components eligible for BOM checks.

    Drops synthesized/undesignated placements and ``#``-prefixed virtual
    parts (power ports, PWR_FLAG): those never appear on a BOM, so flagging
    their missing value/footprint — or "gaps" in #PWR numbering — is noise.
    """
    return [c for c in sch.components
            if not c.undesignated
            and _clean(c.designator)
            and not c.designator.lstrip().startswith("#")]


def run(sch: Schematic) -> list[Finding]:
    """Run BOM hygiene checks on ``sch`` and return findings (possibly empty)."""
    findings: list[Finding] = []
    comps = _real_components(sch)

    # Group by designator, preserving first-seen order of the designators.
    groups: dict[str, list[Component]] = {}
    for c in comps:
        groups.setdefault(c.designator, []).append(c)

    # --- duplicate designators (deduped by UniqueId / multi-unit part) ----------
    for desig, members in groups.items():
        if len(members) < 2:
            continue
        distinct = {_identity(c) for c in members}
        if len(distinct) > 1:
            # One anchor per distinct placement sharing the offending desig —
            # the ambiguity IS "which of these is really `desig`".
            seen_ids: set = set()
            dup_anchors = []
            for c in members:
                ident = _identity(c)
                if ident in seen_ids:
                    continue
                seen_ids.add(ident)
                dup_anchors.append(anchor("component", desig, _pos(c)))
            findings.append(
                Finding(
                    code="BOM_DUPLICATE_DESIGNATOR",
                    severity=Severity.ERROR,
                    message=(
                        f"designator {desig!r} is used by {len(distinct)} distinct "
                        f"components ({len(members)} placements)"
                    ),
                    refs=[desig],
                    pos=_pos(members[0]),
                    anchors=dup_anchors,
                )
            )

    # --- refdes gaps (only within a numeric-suffixed prefix) --------------------
    by_prefix: dict[str, set[int]] = {}
    for desig in groups:  # one logical entry per designator
        parsed = _parse_refdes(desig)
        if parsed is None:  # compound / manually-named ref (e.g. J_USB_C) -> skip
            continue
        prefix, num = parsed
        by_prefix.setdefault(prefix, set()).add(num)

    for prefix in sorted(by_prefix):
        nums = by_prefix[prefix]
        lo, hi = min(nums), max(nums)
        missing = [n for n in range(lo, hi + 1) if n not in nums]
        if missing:
            refs = [f"{prefix}{n}" for n in missing]
            findings.append(
                Finding(
                    code="BOM_REFDES_GAP",
                    severity=Severity.NOTE,
                    message=(
                        f"refdes gap in {prefix!r} sequence ({prefix}{lo}..{prefix}{hi}): "
                        f"missing {', '.join(refs)}"
                    ),
                    refs=refs,
                )
            )

    # --- corrupt (U+FFFD-bearing) text, aggregated per schematic ----------------
    corrupt: list[str] = []
    corrupt_anchors = []
    corrupt_pos: tuple[float, float] | None = None
    for desig, members in groups.items():
        for c in members:
            texts = [c.value, *(c.parameters or {}).values()]
            if any(t and "�" in t for t in texts):
                corrupt.append(desig)
                p = _pos(c)
                corrupt_anchors.append(anchor("component", desig, p))
                if corrupt_pos is None:
                    corrupt_pos = p
                break
    if corrupt:
        findings.append(
            Finding(
                code="BOM_CORRUPT_TEXT",
                severity=Severity.NOTE,
                message=(
                    f"{len(corrupt)} component(s) carry U+FFFD-corrupted text in "
                    f"value/parameters ({', '.join(corrupt[:8])}"
                    f"{', ...' if len(corrupt) > 8 else ''}) -- the replacement "
                    "characters are baked into the file at export time and no "
                    "decoder can recover them; re-export from a tool that "
                    "preserves the original text"
                ),
                refs=corrupt,
                pos=corrupt_pos,
                anchors=corrupt_anchors,
            )
        )

    # --- missing value / footprint (per logical component) ----------------------
    for desig, members in groups.items():
        has_value = any(_clean(c.value) or _part_identity(c) for c in members)
        has_footprint = any(_clean(c.footprint) for c in members)
        pos = _pos(members[0])
        if not has_value:
            findings.append(
                Finding(
                    code="BOM_MISSING_VALUE",
                    severity=Severity.WARNING,
                    message=f"component {desig} has no value",
                    refs=[desig],
                    pos=pos,
                    anchors=[anchor("component", desig, pos)],
                )
            )
        if not has_footprint:
            findings.append(
                Finding(
                    code="BOM_MISSING_FOOTPRINT",
                    severity=Severity.WARNING,
                    message=f"component {desig} has no footprint",
                    refs=[desig],
                    pos=pos,
                    anchors=[anchor("component", desig, pos)],
                )
            )

    return findings
