"""Connectivity delta between two inferred netlists (the pre-apply safety net).

``writers.kicad.apply`` edits geometry; whether the RESULTING connectivity
still matches the author's intent only becomes visible after
``netbuild.build_nets`` runs over the edited sheet. Twice in real sessions an
innocent-looking wire op silently SPLIT a named net (or shorted two rails into
one MERGED net) and nothing warned before apply. This module computes the
before/after delta so plan/draw can print it FIRST.

Contract:

* ``diff(nets_before, nets_after)`` — each side is whatever
  ``netbuild.build_nets`` returns (``model.Net`` is duck-typed via
  ``.members`` / ``.name`` / ``.is_named``); plain ``(name, pins)`` pairs are
  also accepted for fixtures, where each pin is an ``"REF.PIN"`` string or a
  ``(ref, pin)`` tuple. Nets are matched across sides by PIN-MEMBERSHIP
  OVERLAP, never by name — a rename must not masquerade as remove+create.
* Classification (every net lands in at least one bucket):
  UNCHANGED / RENAMED (identical members, different name) / MODIFIED (single
  mutual best-overlap partner with members added/removed; a simultaneous
  rename rides on the same entry) / SPLIT (one before-net's pins land in >=2
  after-nets) / MERGED (>=2 before-nets land in one after-net) / CREATED /
  REMOVED (no pin overlap with the other side at all). A net that both splits
  and feeds a merge appears in both entries — each is a distinct hazard.
* ``NetDiff.equivalent`` is True only when the netlists are IDENTICAL (same
  partitions AND same names); a pure rename still changes the emitted netlist.
* ``has_risk(d)`` — True when any SPLIT or MERGE touches a NAMED net on either
  side; the ``draw --strict-nets`` refusal gate. Splits/merges among purely
  unnamed nets are ordinary wiring edits and do not trip it.
* ``format_summary(d, limit=...)`` — compact one-line-per-change strings, most
  severe first. Unnamed nets render as ``<unnamed@PIN>`` anchored on their
  lexicographically-smallest member pin so lines are stable run-to-run.

Pure module: no file I/O, no CLI, stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# How many individual +pin/-pin tokens a MODIFIED line shows per direction
# before collapsing the tail into "(+N more)".
_MAX_PIN_TOKENS = 3


@dataclass(frozen=True)
class NetView:
    """One net reduced to what the diff needs: a name and a pin set."""

    name: str | None            # None => unnamed (auto-named) net
    pins: frozenset[str]        # "REF.PIN" strings

    @property
    def display(self) -> str:
        if self.name:
            return self.name
        anchor = min(self.pins) if self.pins else "?"
        return f"<unnamed@{anchor}>"


@dataclass(frozen=True)
class Renamed:
    """Identical membership, different name."""

    before: NetView
    after: NetView


@dataclass(frozen=True)
class Modified:
    """Mutual 1:1 best-overlap match whose membership changed."""

    before: NetView
    after: NetView
    added: frozenset[str]       # pins in after but not before
    removed: frozenset[str]     # pins in before but not after

    @property
    def renamed(self) -> bool:
        return self.before.name != self.after.name


@dataclass(frozen=True)
class Split:
    """One before-net whose pins landed in two or more after-nets."""

    before: NetView
    # (after-net fragment, pin count inherited FROM the before-net) — the count
    # is the overlap, not the fragment's total (a fragment may also gain pins).
    fragments: tuple[tuple[NetView, int], ...]


@dataclass(frozen=True)
class Merged:
    """Two or more before-nets whose pins landed in one after-net."""

    sources: tuple[NetView, ...]
    after: NetView


@dataclass
class NetDiff:
    unchanged: list[NetView] = field(default_factory=list)
    renamed: list[Renamed] = field(default_factory=list)
    modified: list[Modified] = field(default_factory=list)
    split: list[Split] = field(default_factory=list)
    merged: list[Merged] = field(default_factory=list)
    created: list[NetView] = field(default_factory=list)
    removed: list[NetView] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        """True iff the two netlists are identical (partitions AND names)."""
        return not (
            self.renamed or self.modified or self.split
            or self.merged or self.created or self.removed
        )


def _view(net: "Any") -> NetView:
    """Coerce one input net (``model.Net`` or ``(name, pins)`` pair) — duck-typed."""
    members = getattr(net, "members", None)
    if members is None:
        name, members = net
    else:
        # model.Net: unnamed nets carry is_named=False (name may be None).
        name = net.name if getattr(net, "is_named", True) else None
    pins = frozenset(
        p if isinstance(p, str) else f"{p[0]}.{p[1]}" for p in members
    )
    return NetView(name=name or None, pins=pins)


def _key(v: NetView) -> tuple[bool, str, tuple[str, ...]]:
    """Deterministic ordering: named first, then by display name, then pins."""
    return (v.name is None, v.display, tuple(sorted(v.pins)))


def diff(nets_before: "list | tuple", nets_after: "list | tuple") -> NetDiff:
    """Classify every net of both sides by pin-membership overlap."""
    before = [_view(n) for n in nets_before]
    after = [_view(n) for n in nets_after]

    # Each side is a partition (netbuild's union-find guarantees a pin appears
    # in exactly one net), so a plain pin -> net index is sufficient.
    pin_to_after: dict[str, int] = {}
    for j, v in enumerate(after):
        for p in v.pins:
            pin_to_after[p] = j

    # overlap[i][j] = number of before[i] pins living in after[j].
    overlap: list[dict[int, int]] = [{} for _ in before]
    sources: list[set[int]] = [set() for _ in after]
    for i, v in enumerate(before):
        for p in v.pins:
            hit = pin_to_after.get(p)
            if hit is not None:
                overlap[i][hit] = overlap[i].get(hit, 0) + 1
                sources[hit].add(i)

    d = NetDiff()
    for i, v in enumerate(before):
        if not overlap[i]:
            d.removed.append(v)
        elif len(overlap[i]) >= 2:
            frags = sorted(
                ((after[j], n) for j, n in overlap[i].items()),
                key=lambda fn: (-fn[1], _key(fn[0])),
            )
            d.split.append(Split(before=v, fragments=tuple(frags)))

    for j, v in enumerate(after):
        src = sources[j]
        if not src:
            d.created.append(v)
        elif len(src) >= 2:
            # The source whose name survived leads ("+3V + VBAT -> +3V"),
            # then larger nets first.
            def _merge_order(s: NetView, a: NetView = v) -> tuple:
                return (s.name != a.name, -len(s.pins), _key(s))

            srcs = tuple(sorted((before[i] for i in src), key=_merge_order))
            d.merged.append(Merged(sources=srcs, after=v))
        else:
            (i,) = src
            if len(overlap[i]) != 1:
                continue  # fragment of a split — reported on the Split entry
            b = before[i]
            if b.pins == v.pins:
                if b.name == v.name:
                    d.unchanged.append(v)
                else:
                    d.renamed.append(Renamed(before=b, after=v))
            else:
                d.modified.append(Modified(
                    before=b, after=v,
                    added=v.pins - b.pins, removed=b.pins - v.pins,
                ))

    for lst in (d.unchanged, d.created, d.removed):
        lst.sort(key=_key)
    d.renamed.sort(key=lambda r: _key(r.after))
    d.modified.sort(key=lambda m: _key(m.after))
    d.split.sort(key=lambda s: _key(s.before))
    d.merged.sort(key=lambda m: _key(m.after))
    return d


def has_risk(d: NetDiff) -> bool:
    """True when any SPLIT/MERGE touches a NAMED net (either side)."""
    for s in d.split:
        if s.before.name or any(f.name for f, _ in s.fragments):
            return True
    for m in d.merged:
        if m.after.name or any(v.name for v in m.sources):
            return True
    return False


def _pin_tokens(sign: str, pins: frozenset[str]) -> list[str]:
    ordered = sorted(pins)
    shown = [f"{sign}{p}" for p in ordered[:_MAX_PIN_TOKENS]]
    extra = len(ordered) - _MAX_PIN_TOKENS
    if extra > 0:
        shown.append(f"(+{extra} more)")
    return shown


def format_summary(d: NetDiff, limit: int | None = 20) -> list[str]:
    """Compact human lines, most severe first; [] when ``d.equivalent``.

    ``limit`` caps the output; the tail is collapsed into a final
    ``... (+N more)`` line. ``limit=None`` disables truncation.
    """
    lines: list[str] = []
    for s in d.split:
        frags = " + ".join(f"{v.display}({n})" for v, n in s.fragments)
        lines.append(
            f"! SPLIT {s.before.display} ({len(s.before.pins)} pins) -> {frags}"
        )
    for m in d.merged:
        srcs = " + ".join(v.display for v in m.sources)
        lines.append(f"! MERGE {srcs} -> {m.after.display}")
    for mo in d.modified:
        name = (f"{mo.before.display}->{mo.after.display}" if mo.renamed
                else mo.after.display)
        delta = " ".join(_pin_tokens("+", mo.added) + _pin_tokens("-", mo.removed))
        lines.append(
            f"~ {name}: {delta} ({len(mo.before.pins)}->{len(mo.after.pins)} pins)"
        )
    for r in d.renamed:
        lines.append(
            f"= RENAME {r.before.display} -> {r.after.display} "
            f"({len(r.after.pins)} pins)"
        )
    for v in d.created:
        lines.append(f"+ NEW {v.display} ({len(v.pins)})")
    for v in d.removed:
        lines.append(f"- GONE {v.display} ({len(v.pins)})")

    if limit is not None and len(lines) > limit:
        hidden = len(lines) - limit
        lines = lines[:limit] + [f"... (+{hidden} more)"]
    return lines
