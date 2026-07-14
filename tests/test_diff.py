"""Tests for the membership-based schematic diff (checks/diff.py, SPEC §3.6).

Core invariants under test:
* nets are matched by pin MEMBERSHIP (Jaccard), never by display name — so a
  pure rename is reported as a name-only change, not add+remove;
* components match UniqueID > (value, footprint, pin-count) signature > refdes,
  and the resulting refdes-rename map remaps net membership before matching;
* cross-revision diffs (no shared UniqueIDs) raise the low_confidence flag.
"""

from __future__ import annotations

import os

import pytest

from akcli.checks import diff
from akcli.model import Component, Net, Pin, PinType, Schematic
from akcli.readers import altium_sch

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _pins(n: int) -> list[Pin]:
    return [Pin(number=str(i + 1), name=None, x_mil=0.0, y_mil=0.0) for i in range(n)]


def comp(des, value=None, footprint=None, npins=2, uid=None, lib=None,
         rotation=0, mirror="none") -> Component:
    return Component(
        designator=des, library_ref=lib, x_mil=0.0, y_mil=0.0,
        rotation=rotation, mirror=mirror, value=value, footprint=footprint,
        unique_id=uid, pins=_pins(npins),
    )


def net(name, members, *, named=True, aliases=None) -> Net:
    return Net(name=name, members=sorted(members), is_named=named, aliases=aliases or [])


def sch(components, nets, fmt="altium") -> Schematic:
    return Schematic(source_path="x", source_format=fmt, components=components, nets=nets)


# --------------------------------------------------------------------------- #
# net matching by membership, not name
# --------------------------------------------------------------------------- #
def test_pure_rename_same_membership_is_name_only_change():
    members = [("U1", "1"), ("U2", "2")]
    a = sch([comp("U1", uid="a"), comp("U2", uid="b")], [net("OLD_NAME", members)])
    b = sch([comp("U1", uid="a"), comp("U2", uid="b")], [net("NEW_NAME", members)])
    rep = diff.run(a, b)
    # one matched net, NOT an add + remove
    assert len(rep.matched_nets) == 1
    assert not rep.added_nets and not rep.removed_nets
    renamed = rep.renamed_nets
    assert len(renamed) == 1
    assert renamed[0].name_a == "OLD_NAME" and renamed[0].name_b == "NEW_NAME"
    assert renamed[0].name_changed and not renamed[0].membership_changed
    assert renamed[0].jaccard == pytest.approx(1.0)
    # no membership churn
    assert not rep.member_changed_nets


def test_same_name_different_membership_is_not_a_free_match():
    # identical display name but disjoint membership -> NOT the same net
    a = sch([comp("U1", uid="a"), comp("U2", uid="b")],
            [net("VBUS", [("U1", "1"), ("U2", "1")])])
    b = sch([comp("U3", uid="c"), comp("U4", uid="d")],
            [net("VBUS", [("U3", "1"), ("U4", "1")])])
    rep = diff.run(a, b)
    assert not rep.matched_nets
    assert len(rep.added_nets) == 1
    assert len(rep.removed_nets) == 1


def test_membership_change_added_and_removed_members():
    a = sch([comp("U1", uid="a"), comp("U2", uid="b"), comp("U3", uid="c")],
            [net("SIG", [("U1", "1"), ("U2", "1"), ("U3", "1")])])
    # drop U3.1, add U2.2 -> jaccard 2/4 = 0.5, still a match
    b = sch([comp("U1", uid="a"), comp("U2", uid="b"), comp("U3", uid="c")],
            [net("SIG", [("U1", "1"), ("U2", "1"), ("U2", "2")])])
    rep = diff.run(a, b)
    assert len(rep.matched_nets) == 1
    nc = rep.member_changed_nets[0]
    assert nc.membership_changed and not nc.name_changed
    assert ("U2", "2") in nc.added_members
    assert ("U3", "1") in nc.removed_members
    assert nc.jaccard == pytest.approx(0.5)


def test_unrelated_nets_below_threshold_do_not_match():
    # share exactly one pin out of many -> jaccard 1/5 < MIN_JACCARD
    a = sch([comp(f"U{i}", uid=str(i)) for i in range(1, 5)],
            [net("A", [("U1", "1"), ("U2", "1"), ("U3", "1")])])
    b = sch([comp(f"U{i}", uid=str(i)) for i in range(1, 5)],
            [net("B", [("U1", "1"), ("U4", "1"), ("U4", "2")])])
    rep = diff.run(a, b)
    assert not rep.matched_nets
    assert len(rep.added_nets) == 1 and len(rep.removed_nets) == 1


# --------------------------------------------------------------------------- #
# component matching priority
# --------------------------------------------------------------------------- #
def test_component_matched_by_unique_id_despite_refdes_change():
    a = sch([comp("R7", value="10k", footprint="0402", uid="UID-1")], [])
    b = sch([comp("R8", value="10k", footprint="0402", uid="UID-1")], [])
    rep = diff.run(a, b)
    assert not rep.added_components and not rep.removed_components
    m = rep.matched_components[0]
    assert m.method == "unique_id"
    assert m.designator_a == "R7" and m.designator_b == "R8"
    assert "designator" in m.field_changes
    assert m.field_changes["designator"] == ("R7", "R8")


def test_unique_id_match_beats_signature_collision():
    # two parts with identical signature; uid forces the correct pairing
    a = sch([comp("R1", value="1k", footprint="0402", uid="X"),
             comp("R2", value="1k", footprint="0402", uid="Y")], [])
    b = sch([comp("R2", value="1k", footprint="0402", uid="X"),
             comp("R1", value="1k", footprint="0402", uid="Y")], [])
    rep = diff.run(a, b)
    pairs = {(m.designator_a, m.designator_b): m for m in rep.matched_components}
    # X stayed X across the rename R1->R2; Y across R2->R1
    assert ("R1", "R2") in pairs and pairs[("R1", "R2")].method == "unique_id"
    assert ("R2", "R1") in pairs and pairs[("R2", "R1")].method == "unique_id"


def test_component_matched_by_signature_when_no_uid():
    a = sch([comp("C1", value="100nF", footprint="0402", npins=2)], [])
    b = sch([comp("C9", value="100nF", footprint="0402", npins=2)], [])
    rep = diff.run(a, b)
    m = rep.matched_components[0]
    assert m.method == "signature"
    assert m.designator_a == "C1" and m.designator_b == "C9"


def test_signature_prefers_same_refdes_tiebreak():
    a = sch([comp("R1", value="1k", footprint="0402"),
             comp("R2", value="1k", footprint="0402")], [])
    b = sch([comp("R2", value="1k", footprint="0402"),
             comp("R1", value="1k", footprint="0402")], [])
    rep = diff.run(a, b)
    # no uids -> signature pass, refdes tiebreak keeps R1<->R1, R2<->R2
    pairs = {(m.designator_a, m.designator_b) for m in rep.matched_components}
    assert ("R1", "R1") in pairs and ("R2", "R2") in pairs


def test_component_matched_by_refdes_when_signature_differs():
    # same refdes, changed value -> refdes pass, value reported as a field change
    a = sch([comp("U3", value="nRF52833", footprint="QFN48")], [])
    b = sch([comp("U3", value="nRF52840", footprint="QFN48")], [])
    rep = diff.run(a, b)
    m = rep.matched_components[0]
    assert m.method == "refdes"
    assert m.field_changes["value"] == ("nRF52833", "nRF52840")


def test_added_and_removed_components():
    a = sch([comp("R1", uid="a"), comp("R2", uid="b")], [])
    b = sch([comp("R1", uid="a"), comp("R3", uid="c")], [])
    rep = diff.run(a, b)
    assert [c.designator_a for c in rep.removed_components] == ["R2"]
    assert [c.designator_b for c in rep.added_components] == ["R3"]


# --------------------------------------------------------------------------- #
# refdes-rename map remaps net membership
# --------------------------------------------------------------------------- #
def test_refdes_rename_keeps_net_matched_via_remap():
    # U1 -> U9 (matched by uid); the net it touches must still match, not churn
    a = sch([comp("U1", uid="K", npins=2), comp("U2", uid="L", npins=2)],
            [net("DAT", [("U1", "1"), ("U2", "1")])])
    b = sch([comp("U9", uid="K", npins=2), comp("U2", uid="L", npins=2)],
            [net("DAT", [("U9", "1"), ("U2", "1")])])
    rep = diff.run(a, b)
    assert rep.rename_map == {"U1": "U9"}
    assert len(rep.matched_nets) == 1
    nc = rep.matched_nets[0]
    assert not nc.membership_changed  # remap made memberships identical
    assert not rep.added_nets and not rep.removed_nets


# --------------------------------------------------------------------------- #
# low-confidence (cross-revision) flag
# --------------------------------------------------------------------------- #
def test_low_confidence_when_no_shared_unique_ids():
    a = sch([comp("R1", value="1k", footprint="0402")],
            [net("N", [("R1", "1"), ("R1", "2")])])
    b = sch([comp("R1", value="1k", footprint="0402")],
            [net("N", [("R1", "1"), ("R1", "2")])])
    rep = diff.run(a, b)
    assert rep.low_confidence is True
    assert rep.notes
    assert "UniqueID" in rep.notes[0]


def test_high_confidence_when_uids_match():
    a = sch([comp("R1", uid="a"), comp("R2", uid="b")], [])
    b = sch([comp("R1", uid="a"), comp("R2", uid="b")], [])
    rep = diff.run(a, b)
    assert rep.low_confidence is False


def test_low_confidence_lowers_net_confidence():
    members = [("R1", "1"), ("R1", "2")]
    a = sch([comp("R1", value="1k", footprint="0402")], [net("N", members)])
    b = sch([comp("R1", value="1k", footprint="0402")], [net("N", members)])
    rep = diff.run(a, b)
    assert rep.matched_nets[0].confidence <= 0.6


# --------------------------------------------------------------------------- #
# identity / determinism / export / findings
# --------------------------------------------------------------------------- #
def test_identity_diff_is_empty():
    c = [comp("U1", uid="a"), comp("U2", uid="b")]
    n = [net("GND", [("U1", "1"), ("U2", "1")])]
    a = sch(list(c), list(n))
    b = sch(list(c), list(n))
    rep = diff.run(a, b)
    assert not rep.added_components and not rep.removed_components
    assert not rep.changed_components
    assert not rep.added_nets and not rep.removed_nets
    assert not rep.renamed_nets and not rep.member_changed_nets


def test_run_is_deterministic():
    a = sch([comp("U1", uid="a"), comp("U2", uid="b"), comp("U3", uid="c")],
            [net("A", [("U1", "1"), ("U2", "1")]), net("B", [("U3", "1"), ("U3", "2")])])
    b = sch([comp("U1", uid="a"), comp("U2", uid="b")],
            [net("A2", [("U1", "1"), ("U2", "1")])])
    r1, r2 = diff.run(a, b), diff.run(a, b)
    assert r1.export() == r2.export()


def test_export_is_json_native():
    import json

    a = sch([comp("U1", uid="a")], [net("A", [("U1", "1"), ("U1", "2")])])
    b = sch([comp("U1", uid="a"), comp("U2", uid="b")],
            [net("A", [("U1", "1"), ("U1", "2")]), net("B", [("U2", "1"), ("U2", "2")])])
    rep = diff.run(a, b)
    s = json.dumps(rep.export())  # must not raise
    back = json.loads(s)
    assert back["summary"]["nets"]["added"] == 1
    assert back["summary"]["components"]["added"] == 1


def test_findings_cover_changes():
    a = sch([comp("U1", value="A", footprint="F", uid="a"), comp("U2", uid="b")],
            [net("OLD", [("U1", "1"), ("U2", "1")])])
    b = sch([comp("U1", value="B", footprint="F", uid="a"), comp("U3", uid="c")],
            [net("NEW", [("U1", "1"), ("U2", "1")]), net("EXTRA", [("U3", "1"), ("U3", "2")])])
    rep = diff.run(a, b)
    codes = {f.code for f in rep.findings()}
    assert "DIFF_COMPONENT_CHANGED" in codes  # U1 value A->B
    assert "DIFF_COMPONENT_REMOVED" in codes  # U2
    assert "DIFF_COMPONENT_ADDED" in codes    # U3
    assert "DIFF_NET_RENAMED" in codes        # OLD->NEW same membership
    assert "DIFF_NET_ADDED" in codes          # EXTRA


def test_pin_count_change_reported():
    a = sch([comp("U1", value="v", footprint="f", npins=4, uid="a")], [])
    b = sch([comp("U1", value="v", footprint="f", npins=6, uid="a")], [])
    rep = diff.run(a, b)
    m = rep.matched_components[0]
    assert m.field_changes["pin_count"] == (4, 6)


# --------------------------------------------------------------------------- #
# real fixture end-to-end (uses the frozen Altium reader)
# --------------------------------------------------------------------------- #
def test_real_fixture_self_diff_is_clean():
    a = altium_sch.read(os.path.join(FIX, "shared_name_label.SchDoc"))
    b = altium_sch.read(os.path.join(FIX, "shared_name_label.SchDoc"))
    rep = diff.run(a, b)
    assert not rep.added_nets and not rep.removed_nets
    assert not rep.member_changed_nets and not rep.renamed_nets
    assert not rep.added_components and not rep.removed_components


def test_real_fixture_detects_added_component():
    a = altium_sch.read(os.path.join(FIX, "two_gnd_ports.SchDoc"))
    b = altium_sch.read(os.path.join(FIX, "shared_name_label.SchDoc"))
    rep = diff.run(a, b)
    # different boards -> something must differ and it is flagged low-confidence
    assert rep.low_confidence is True
    assert rep.net_changes  # at minimum the GND vs STAT nets diverge
