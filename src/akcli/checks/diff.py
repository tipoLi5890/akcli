"""Net-level v1<->v2 schematic diff (SPEC §3.6).

The hard correctness requirement (risk #10): a schematic diff that keys on the
*display name* of a net is useless on real boards, because both Altium and KiCad
hand out coordinate-derived auto-names (``N$1234``) that churn on every edit, and
designers rename nets freely. This module therefore matches:

* **nets by MEMBERSHIP** — a maximum-weight (greedy) bipartite match on the
  Jaccard overlap of pin-membership sets, *never* on display name. A rename with
  identical connectivity is reported as a name-only change; a connectivity change
  is reported separately.
* **components by UniqueID** (the stable Altium identity, when both sides carry
  it) → then a ``(value, footprint, pin-count)`` *signature* → then refdes. The
  resulting designator-rename map is applied to net membership before the Jaccard
  pass, so a part renamed ``R7``→``R8`` does not desync every net it touches.

When the two schematics share no UniqueIDs (the usual *cross-revision* case) the
report is flagged ``low_confidence`` with an explanatory note — a signature/
refdes/membership match is a heuristic, not ground truth.

``run(a, b) -> DiffReport`` is pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import SCHEMA_VERSION, Component, Net, PinRef, Schematic
from ..report import Finding, Severity

# A matched net pair below this Jaccard overlap is treated as "no match" (the two
# nets become an add/remove pair instead). Kept deliberately low so that a net
# which merely gains or loses a pin still matches its prior self, while two
# genuinely unrelated nets that happen to brush one shared pin do not.
MIN_JACCARD = 0.34

# Net-match Jaccard below this is itself evidence of a shaky (low-confidence) diff.
_SHAKY_JACCARD = 0.5

# Component fields compared field-by-field on a matched pair.
_COMPARED_FIELDS = ("designator", "value", "footprint", "library_ref", "rotation", "mirror")

ComponentSig = tuple[str | None, str | None, int]


# --------------------------------------------------------------------------- #
# Report data model (DiffReport lives here — model.py is frozen)
# --------------------------------------------------------------------------- #
@dataclass
class ComponentChange:
    """One component across the two revisions.

    ``method`` is how the pair was matched (``unique_id`` / ``signature`` /
    ``refdes``) or ``added`` / ``removed`` when it exists on only one side.
    ``field_changes`` maps a field name to ``(old, new)``.
    """

    designator_a: str | None
    designator_b: str | None
    method: str
    confidence: float
    field_changes: dict[str, tuple] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.field_changes)

    def export(self) -> dict:
        return {
            "designator_a": self.designator_a,
            "designator_b": self.designator_b,
            "method": self.method,
            "confidence": round(self.confidence, 4),
            "field_changes": {k: list(v) for k, v in self.field_changes.items()},
        }


@dataclass
class NetChange:
    """One net across the two revisions, matched by pin membership (not name)."""

    name_a: str | None
    name_b: str | None
    stable_id_a: str | None
    stable_id_b: str | None
    method: str  # "membership" | "added" | "removed"
    jaccard: float
    added_members: list[PinRef] = field(default_factory=list)
    removed_members: list[PinRef] = field(default_factory=list)
    name_changed: bool = False
    membership_changed: bool = False
    confidence: float = 1.0

    def export(self) -> dict:
        return {
            "name_a": self.name_a,
            "name_b": self.name_b,
            "stable_id_a": self.stable_id_a,
            "stable_id_b": self.stable_id_b,
            "method": self.method,
            "jaccard": round(self.jaccard, 4),
            "added_members": [list(m) for m in self.added_members],
            "removed_members": [list(m) for m in self.removed_members],
            "name_changed": self.name_changed,
            "membership_changed": self.membership_changed,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class DiffReport:
    """Result of ``run(a, b)``."""

    component_changes: list[ComponentChange]
    net_changes: list[NetChange]
    low_confidence: bool = False
    notes: list[str] = field(default_factory=list)
    rename_map: dict[str, str] = field(default_factory=dict)

    # -- component views ---------------------------------------------------- #
    @property
    def added_components(self) -> list[ComponentChange]:
        return [c for c in self.component_changes if c.method == "added"]

    @property
    def removed_components(self) -> list[ComponentChange]:
        return [c for c in self.component_changes if c.method == "removed"]

    @property
    def matched_components(self) -> list[ComponentChange]:
        return [c for c in self.component_changes if c.method not in ("added", "removed")]

    @property
    def changed_components(self) -> list[ComponentChange]:
        return [c for c in self.matched_components if c.changed]

    # -- net views ---------------------------------------------------------- #
    @property
    def added_nets(self) -> list[NetChange]:
        return [n for n in self.net_changes if n.method == "added"]

    @property
    def removed_nets(self) -> list[NetChange]:
        return [n for n in self.net_changes if n.method == "removed"]

    @property
    def matched_nets(self) -> list[NetChange]:
        return [n for n in self.net_changes if n.method == "membership"]

    @property
    def renamed_nets(self) -> list[NetChange]:
        """Matched nets whose display name changed but membership did NOT."""
        return [n for n in self.matched_nets if n.name_changed and not n.membership_changed]

    @property
    def member_changed_nets(self) -> list[NetChange]:
        """Matched nets whose pin membership changed (name may or may not differ)."""
        return [n for n in self.matched_nets if n.membership_changed]

    def summary(self) -> dict:
        return {
            "components": {
                "added": len(self.added_components),
                "removed": len(self.removed_components),
                "changed": len(self.changed_components),
                "matched": len(self.matched_components),
            },
            "nets": {
                "added": len(self.added_nets),
                "removed": len(self.removed_nets),
                "renamed": len(self.renamed_nets),
                "membership_changed": len(self.member_changed_nets),
                "matched": len(self.matched_nets),
            },
            "low_confidence": self.low_confidence,
        }

    def export(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "summary": self.summary(),
            "low_confidence": self.low_confidence,
            "notes": list(self.notes),
            "rename_map": dict(self.rename_map),
            "component_changes": [c.export() for c in self.component_changes],
            "net_changes": [n.export() for n in self.net_changes],
        }

    def findings(self) -> list[Finding]:
        """Render the diff as report ``Finding`` objects (for the CLI ``diff``)."""
        out: list[Finding] = []
        if self.low_confidence:
            note = self.notes[0] if self.notes else "cross-revision heuristic match"
            out.append(
                Finding(
                    "DIFF_LOW_CONFIDENCE",
                    Severity.NOTE,
                    f"low-confidence diff: {note}",
                )
            )
        for c in self.removed_components:
            out.append(
                Finding("DIFF_COMPONENT_REMOVED", Severity.WARNING,
                        f"component removed: {c.designator_a}", [c.designator_a])
            )
        for c in self.added_components:
            out.append(
                Finding("DIFF_COMPONENT_ADDED", Severity.WARNING,
                        f"component added: {c.designator_b}", [c.designator_b])
            )
        for c in self.changed_components:
            bits = ", ".join(f"{k}: {old!r}->{new!r}" for k, (old, new) in sorted(c.field_changes.items()))
            ref = c.designator_b or c.designator_a
            out.append(
                Finding("DIFF_COMPONENT_CHANGED", Severity.WARNING,
                        f"component {ref} changed ({c.method}): {bits}", [ref])
            )
        for n in self.removed_nets:
            out.append(
                Finding("DIFF_NET_REMOVED", Severity.WARNING,
                        f"net removed: {_net_label(n.name_a, n.stable_id_a)}")
            )
        for n in self.added_nets:
            out.append(
                Finding("DIFF_NET_ADDED", Severity.WARNING,
                        f"net added: {_net_label(n.name_b, n.stable_id_b)}")
            )
        for n in self.member_changed_nets:
            adds = ", ".join(f"{d}.{p}" for d, p in n.added_members)
            rems = ", ".join(f"{d}.{p}" for d, p in n.removed_members)
            detail = []
            if adds:
                detail.append(f"+[{adds}]")
            if rems:
                detail.append(f"-[{rems}]")
            label = _net_label(n.name_b or n.name_a, n.stable_id_b)
            out.append(
                Finding("DIFF_NET_MEMBERSHIP", Severity.WARNING,
                        f"net {label} membership changed: {' '.join(detail)}")
            )
        for n in self.renamed_nets:
            out.append(
                Finding("DIFF_NET_RENAMED", Severity.NOTE,
                        f"net renamed (same membership): {n.name_a!r} -> {n.name_b!r}")
            )
        return out


def _net_label(name: str | None, stable_id: str | None) -> str:
    if name:
        return name
    return f"<unnamed {stable_id}>" if stable_id else "<unnamed>"


# --------------------------------------------------------------------------- #
# Component matching
# --------------------------------------------------------------------------- #
def _signature(c: Component) -> ComponentSig:
    return (c.value, c.footprint, len(c.pins))


def _sig_is_distinct(c: Component) -> bool:
    """A signature is matchable only if it carries a distinguishing attribute.

    Pin-count alone is far too weak to assert identity: two unrelated parts that
    both have no value and no footprint (e.g. ``(None, None, 2)``) would otherwise
    be force-matched, silently hiding real add/remove pairs and — worse — remapping
    net membership so disjoint nets appear identical. Require value OR footprint.
    """
    return bool(c.value) or bool(c.footprint)


def _field_changes(a: Component, b: Component) -> dict[str, tuple]:
    changes: dict[str, tuple] = {}
    for f in _COMPARED_FIELDS:
        av, bv = getattr(a, f), getattr(b, f)
        if av != bv:
            changes[f] = (av, bv)
    if len(a.pins) != len(b.pins):
        changes["pin_count"] = (len(a.pins), len(b.pins))
    return changes


def _match_components(
    a_comps: list[Component], b_comps: list[Component]
) -> tuple[list[tuple[Component, Component, str, float]], list[Component], list[Component], int]:
    """Greedy 3-pass match. Returns (pairs, removed_only_a, added_only_b, uid_count)."""
    pool_a = list(a_comps)
    pool_b = list(b_comps)
    pairs: list[tuple[Component, Component, str, float]] = []
    uid_count = 0

    # Pass 1: UniqueID (exact, non-empty, unambiguous on each side).
    b_by_uid: dict[str, list[Component]] = {}
    for c in pool_b:
        if c.unique_id:
            b_by_uid.setdefault(c.unique_id, []).append(c)
    matched_b: set[int] = set()
    rest_a: list[Component] = []
    for ca in pool_a:
        cand = b_by_uid.get(ca.unique_id or "")
        chosen = None
        if cand:
            # prefer an as-yet-unused candidate (defensive against dup uids)
            for cb in cand:
                if id(cb) not in matched_b:
                    chosen = cb
                    break
        if chosen is not None:
            matched_b.add(id(chosen))
            pairs.append((ca, chosen, "unique_id", 1.0))
            uid_count += 1
        else:
            rest_a.append(ca)
    pool_a = rest_a
    pool_b = [c for c in pool_b if id(c) not in matched_b]

    # Pass 2: (value, footprint, pin-count) signature; refdes used as tie-breaker.
    b_by_sig: dict[ComponentSig, list[Component]] = {}
    for c in pool_b:
        if _sig_is_distinct(c):
            b_by_sig.setdefault(_signature(c), []).append(c)
    rest_a = []
    used_b: set[int] = set()
    for ca in pool_a:
        if not _sig_is_distinct(ca):
            rest_a.append(ca)
            continue
        bucket = [c for c in b_by_sig.get(_signature(ca), []) if id(c) not in used_b]
        if not bucket:
            rest_a.append(ca)
            continue
        same_ref = [c for c in bucket if c.designator == ca.designator]
        chosen = same_ref[0] if same_ref else bucket[0]
        used_b.add(id(chosen))
        conf = 0.9 if chosen.designator == ca.designator else 0.75
        pairs.append((ca, chosen, "signature", conf))
    pool_a = rest_a
    pool_b = [c for c in pool_b if id(c) not in used_b]

    # Pass 3: refdes (signature differed, e.g. value/footprint edit).
    b_by_ref: dict[str, list[Component]] = {}
    for c in pool_b:
        b_by_ref.setdefault(c.designator, []).append(c)
    rest_a = []
    used_b = set()
    for ca in pool_a:
        bucket = [c for c in b_by_ref.get(ca.designator, []) if id(c) not in used_b]
        if not bucket:
            rest_a.append(ca)
            continue
        chosen = bucket[0]
        used_b.add(id(chosen))
        pairs.append((ca, chosen, "refdes", 0.6))
    pool_a = rest_a
    pool_b = [c for c in pool_b if id(c) not in used_b]

    return pairs, pool_a, pool_b, uid_count


# --------------------------------------------------------------------------- #
# Net matching (Jaccard bipartite, on remapped membership)
# --------------------------------------------------------------------------- #
def _translate_members(members: list[PinRef], rename: dict[str, str]) -> frozenset[PinRef]:
    return frozenset((rename.get(d, d), p) for d, p in members)


def _names_differ(name_a: str | None, name_b: str | None, net_a: Net, net_b: Net) -> bool:
    if name_a == name_b:
        return False
    # A name that survives only as an alias on the other side is not a "rename".
    if name_a and name_a in (net_b.aliases or []):
        return False
    if name_b and name_b in (net_a.aliases or []):
        return False
    return True


def _match_nets(
    a_nets: list[Net], b_nets: list[Net], rename: dict[str, str]
) -> list[NetChange]:
    a_sets = [_translate_members(n.members, rename) for n in a_nets]
    b_sets = [frozenset(n.members) for n in b_nets]

    candidates: list[tuple[float, int, int]] = []
    for i, asn in enumerate(a_sets):
        if not asn:
            continue
        for j, bsn in enumerate(b_sets):
            if not bsn:
                continue
            inter = asn & bsn
            if not inter:
                continue
            jac = len(inter) / len(asn | bsn)
            if jac >= MIN_JACCARD:
                candidates.append((jac, i, j))
    # Greedy maximum-weight bipartite match (deterministic tie-break by index).
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for jac, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append((i, j, jac))

    changes: list[NetChange] = []
    for i, j, jac in matched:
        na, nb = a_nets[i], b_nets[j]
        a_set, b_set = a_sets[i], b_sets[j]
        added = sorted(b_set - a_set)
        removed = sorted(a_set - b_set)
        name_changed = _names_differ(na.name, nb.name, na, nb)
        membership_changed = jac < 1.0
        changes.append(
            NetChange(
                name_a=na.name,
                name_b=nb.name,
                stable_id_a=na.stable_id,
                stable_id_b=nb.stable_id,
                method="membership",
                jaccard=jac,
                added_members=added,
                removed_members=removed,
                name_changed=name_changed,
                membership_changed=membership_changed,
                confidence=jac,
            )
        )

    for i, na in enumerate(a_nets):
        if i in used_a or not a_sets[i]:
            continue
        changes.append(
            NetChange(na.name, None, na.stable_id, None, "removed", 0.0,
                      removed_members=sorted(a_sets[i]))
        )
    for j, nb in enumerate(b_nets):
        if j in used_b or not b_sets[j]:
            continue
        changes.append(
            NetChange(None, nb.name, None, nb.stable_id, "added", 0.0,
                      added_members=sorted(b_sets[j]))
        )

    # Stable ordering: matched first (by a-index), then removed, then added.
    order = {"membership": 0, "removed": 1, "added": 2}
    changes.sort(key=lambda n: (order[n.method], n.name_a or "", n.name_b or "", n.stable_id_a or "", n.stable_id_b or ""))
    return changes


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run(a: Schematic, b: Schematic) -> DiffReport:
    """Diff schematic ``a`` (old) against ``b`` (new). Deterministic, pure."""
    pairs, removed_a, added_b, uid_count = _match_components(a.components, b.components)

    # designator-rename map (a.designator -> b.designator) from every matched pair,
    # used to translate A's net membership into B's namespace before the Jaccard pass.
    rename: dict[str, str] = {}
    for ca, cb, _method, _conf in pairs:
        if ca.designator != cb.designator:
            rename[ca.designator] = cb.designator

    component_changes: list[ComponentChange] = []
    for ca, cb, method, conf in pairs:
        component_changes.append(
            ComponentChange(ca.designator, cb.designator, method, conf, _field_changes(ca, cb))
        )
    for ca in removed_a:
        component_changes.append(ComponentChange(ca.designator, None, "removed", 1.0))
    for cb in added_b:
        component_changes.append(ComponentChange(None, cb.designator, "added", 1.0))

    net_changes = _match_nets(a.nets, b.nets, rename)

    # Confidence: cross-revision (no shared UniqueIDs) or shaky net overlaps.
    total_matched = len(pairs)
    uid_ratio = (uid_count / total_matched) if total_matched else 1.0
    shaky_net = any(
        n.method == "membership" and n.jaccard < _SHAKY_JACCARD for n in net_changes
    )
    notes: list[str] = []
    low_confidence = False
    if total_matched and uid_ratio < 0.5:
        low_confidence = True
        if uid_count == 0:
            notes.append(
                "no shared UniqueIDs between revisions; components matched by "
                "signature/refdes (heuristic, not authoritative)"
            )
        else:
            notes.append(
                f"only {uid_count}/{total_matched} components matched by UniqueID; "
                "the rest by signature/refdes heuristic"
            )
    if shaky_net:
        low_confidence = True
        notes.append("some nets matched on weak membership overlap (Jaccard < 0.5)")

    # Lower per-net confidence when the whole diff is cross-revision heuristic.
    if low_confidence:
        for n in net_changes:
            if n.method == "membership":
                n.confidence = min(n.confidence, 0.6)

    return DiffReport(
        component_changes=component_changes,
        net_changes=net_changes,
        low_confidence=low_confidence,
        notes=notes,
        rename_map=rename,
    )
