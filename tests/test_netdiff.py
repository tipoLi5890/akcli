"""Tests for netdiff — connectivity delta between two inferred netlists.

Fixtures are built DIRECTLY as (name, pin-set) pairs (the coercion netdiff
accepts alongside real ``model.Net`` objects), so every classification —
UNCHANGED / RENAMED / MODIFIED (incl. rename+modify combined) / SPLIT /
MERGED / MOVED / CREATED / REMOVED — is exercised without touching any reader.
One test feeds genuine ``model.Net`` inputs to pin down the duck-typed
coercion (PinRef tuples, is_named=False => unnamed).
"""

from __future__ import annotations

import pytest

from akcli import model
from akcli.netdiff import Moved, NetView, diff, format_summary, has_risk


# --- helpers -----------------------------------------------------------------

def _net(name, *pins):
    """Fixture net: name (None => unnamed) + 'REF.PIN' member strings."""
    return (name, set(pins))


def _only(d, *buckets):
    """Assert exactly the given NetDiff buckets are non-empty ('unchanged' free)."""
    all_buckets = ("renamed", "modified", "split", "merged", "moved",
                   "created", "removed")
    for b in all_buckets:
        got = getattr(d, b)
        if b in buckets:
            assert got, f"expected {b} entries, found none"
        else:
            assert not got, f"unexpected {b} entries: {got}"


# --- self-diff / equivalence -------------------------------------------------

def test_self_diff_is_equivalent():
    nets = [
        _net("GND", "U1.4", "C1.2", "C2.2"),
        _net("VTH", "U1.7", "R1.1"),
        _net(None, "R2.1", "R3.2"),
    ]
    d = diff(nets, nets)
    assert d.equivalent is True
    assert has_risk(d) is False
    assert format_summary(d) == []
    _only(d)  # only unchanged populated
    assert len(d.unchanged) == 3


def test_empty_inputs_are_equivalent():
    d = diff([], [])
    assert d.equivalent is True
    assert format_summary(d) == []


def test_member_order_and_container_type_do_not_matter():
    before = [("VTH", ["U1.7", "R1.1"])]
    after = [("VTH", ("R1.1", "U1.7"))]
    d = diff(before, after)
    assert d.equivalent is True


# --- RENAMED -----------------------------------------------------------------

def test_rename_same_members_different_name():
    d = diff([_net("THR", "U1.1", "R1.1")], [_net("THERM", "U1.1", "R1.1")])
    _only(d, "renamed")
    assert d.equivalent is False
    r = d.renamed[0]
    assert (r.before.name, r.after.name) == ("THR", "THERM")
    assert has_risk(d) is False  # rename is not a split/merge
    assert format_summary(d) == ["= RENAME THR -> THERM (2 pins)"]


def test_named_to_unnamed_is_a_rename_not_remove():
    """Losing a label keeps the cluster: matched by pins, classified RENAMED."""
    d = diff([_net("THR", "U1.1", "R1.1")], [_net(None, "U1.1", "R1.1")])
    _only(d, "renamed")
    assert d.renamed[0].after.name is None
    assert format_summary(d) == ["= RENAME THR -> <unnamed@R1.1> (2 pins)"]


# --- MODIFIED (incl. rename+modify combined) ---------------------------------

def test_modified_pin_added():
    before = [_net("VTH", "U1.1", "U1.2", "R1.1", "R2.1", "R3.1")]
    after = [_net("VTH", "U1.1", "U1.2", "R1.1", "R2.1", "R3.1", "U1.7")]
    d = diff(before, after)
    _only(d, "modified")
    m = d.modified[0]
    assert m.added == frozenset({"U1.7"})
    assert m.removed == frozenset()
    assert m.renamed is False
    assert format_summary(d) == ["~ VTH: +U1.7 (5->6 pins)"]
    assert has_risk(d) is False  # membership drift is warned, not strict-gated


def test_modified_pin_removed():
    d = diff([_net("SDA", "U1.3", "R5.1", "J1.2")], [_net("SDA", "U1.3", "R5.1")])
    _only(d, "modified")
    m = d.modified[0]
    assert m.removed == frozenset({"J1.2"})
    assert format_summary(d) == ["~ SDA: -J1.2 (3->2 pins)"]


def test_rename_and_modify_combined_is_one_modified_entry():
    d = diff(
        [_net("THR", "U1.1", "R1.1", "R2.1")],
        [_net("THERM", "U1.1", "R1.1", "C1.1")],
    )
    _only(d, "modified")
    m = d.modified[0]
    assert m.renamed is True
    assert (m.before.name, m.after.name) == ("THR", "THERM")
    assert m.added == frozenset({"C1.1"})
    assert m.removed == frozenset({"R2.1"})
    assert format_summary(d) == ["~ THR->THERM: +C1.1 -R2.1 (3->3 pins)"]


def test_modified_long_pin_delta_is_truncated():
    before = [_net("BUS", "U1.1")]
    after = [_net("BUS", "U1.1", "A.1", "B.1", "C.1", "D.1", "E.1")]
    d = diff(before, after)
    (line,) = format_summary(d)
    # only 3 pin tokens shown, remainder collapsed
    assert line == "~ BUS: +A.1 +B.1 +C.1 (+2 more) (1->6 pins)"


# --- SPLIT -------------------------------------------------------------------

def test_split_named_net_into_named_plus_unnamed():
    before = [_net("THR", "U1.1", "U1.2", "R1.1", "R2.1")]
    after = [
        _net("THR", "U1.1", "U1.2"),
        _net(None, "R1.1", "R2.1"),
    ]
    d = diff(before, after)
    _only(d, "split")
    s = d.split[0]
    assert s.before.name == "THR"
    assert [(v.name, n) for v, n in s.fragments] == [("THR", 2), (None, 2)]
    assert has_risk(d) is True
    assert format_summary(d) == [
        "! SPLIT THR (4 pins) -> THR(2) + <unnamed@R1.1>(2)"
    ]


def test_split_fragments_ordered_by_overlap_desc():
    before = [_net("CLK", "U1.1", "U2.1", "U3.1", "R1.1", "R2.1")]
    after = [
        _net(None, "R1.1", "R2.1"),
        _net("CLK", "U1.1", "U2.1", "U3.1"),
    ]
    d = diff(before, after)
    s = d.split[0]
    assert [n for _, n in s.fragments] == [3, 2]
    assert s.fragments[0][0].name == "CLK"


def test_split_of_purely_unnamed_nets_carries_no_risk():
    before = [_net(None, "R1.1", "R2.1", "R3.1", "R4.1")]
    after = [_net(None, "R1.1", "R2.1"), _net(None, "R3.1", "R4.1")]
    d = diff(before, after)
    _only(d, "split")
    assert has_risk(d) is False


# --- MERGED ------------------------------------------------------------------

def test_merge_two_named_rails():
    before = [
        _net("+3V", "U1.8", "C1.1", "C2.1"),
        _net("VBAT", "J1.1", "D1.2"),
    ]
    after = [_net("+3V", "U1.8", "C1.1", "C2.1", "J1.1", "D1.2")]
    d = diff(before, after)
    _only(d, "merged")
    m = d.merged[0]
    assert [v.name for v in m.sources] == ["+3V", "VBAT"]  # bigger source first
    assert m.after.name == "+3V"
    assert has_risk(d) is True
    assert format_summary(d) == ["! MERGE +3V + VBAT -> +3V"]


def test_merge_surviving_name_listed_first_even_when_smaller():
    before = [_net("+3V", "U9.8"), _net("VBAT", "J1.1", "D1.2")]
    after = [_net("+3V", "U9.8", "J1.1", "D1.2")]
    d = diff(before, after)
    assert format_summary(d) == ["! MERGE +3V + VBAT -> +3V"]


def test_merge_of_purely_unnamed_nets_carries_no_risk():
    before = [_net(None, "R1.1", "R2.1"), _net(None, "R3.1", "R4.1")]
    after = [_net(None, "R1.1", "R2.1", "R3.1", "R4.1")]
    d = diff(before, after)
    _only(d, "merged")
    assert has_risk(d) is False


def test_merge_unnamed_into_named_rail_is_risky():
    """Absorbing an anonymous cluster into GND still touches a NAMED net."""
    before = [_net("GND", "U1.4"), _net(None, "R9.2", "C3.2")]
    after = [_net("GND", "U1.4", "R9.2", "C3.2")]
    d = diff(before, after)
    _only(d, "merged")
    assert has_risk(d) is True


# --- CREATED / REMOVED -------------------------------------------------------

def test_created_and_removed():
    before = [
        _net("GND", "U1.4", "C1.2"),
        _net("SENSE1", "U2.1", "R4.1", "R5.1"),
    ]
    after = [
        _net("GND", "U1.4", "C1.2"),
        _net("BALL1_N", "U3.1", "U3.2", "U3.3", "U3.4", "U3.5"),
    ]
    d = diff(before, after)
    _only(d, "created", "removed")
    assert d.created[0].name == "BALL1_N"
    assert d.removed[0].name == "SENSE1"
    assert has_risk(d) is False
    assert format_summary(d) == [
        "+ NEW BALL1_N (5)",
        "- GONE SENSE1 (3)",
    ]


# --- simultaneous split + merge (pin reshuffle) --------------------------------

def test_reshuffle_reports_both_split_and_merge():
    before = [
        _net("THR", "A.1", "B.1", "C.1", "D.1"),
        _net("AUX", "E.1", "F.1"),
    ]
    after = [
        _net("THR", "A.1", "B.1"),
        _net("AUX", "C.1", "D.1", "E.1", "F.1"),
    ]
    d = diff(before, after)
    _only(d, "split", "merged")
    s = d.split[0]
    assert s.before.name == "THR"
    assert {v.name for v, _ in s.fragments} == {"THR", "AUX"}
    m = d.merged[0]
    assert {v.name for v in m.sources} == {"THR", "AUX"}
    assert m.after.name == "AUX"
    assert has_risk(d) is True
    # split line first (most severe grouping), then merge (survivor leads)
    lines = format_summary(d)
    assert lines[0].startswith("! SPLIT THR (4 pins) ->")
    assert lines[1] == "! MERGE AUX + THR -> AUX"


# --- MOVED (a minority pin group changes nets; both nets otherwise hold) -------

def test_move_single_pin_between_named_nets():
    """One pin changing nets is ONE line — not a split of A plus a merge into B."""
    before = [_net("A", "R1.1", "R2.1", "U1.3"), _net("B", "R3.1", "R4.1")]
    after = [_net("A", "R1.1", "R2.1"), _net("B", "R3.1", "R4.1", "U1.3")]
    d = diff(before, after)
    _only(d, "moved", "modified")          # no SPLIT, no MERGE
    (mv,) = d.moved
    assert isinstance(mv, Moved)
    assert mv.pins == frozenset({"U1.3"})
    assert (mv.before.name, mv.after.name) == ("A", "B")
    assert mv.before.pins == frozenset({"R1.1", "R2.1", "U1.3"})   # before side
    assert mv.after.pins == frozenset({"R3.1", "R4.1", "U1.3"})    # after side
    assert has_risk(d) is True             # a named net is touched: still gated
    assert format_summary(d) == ["! MOVE U1.3: A -> B"]


def test_pure_move_is_not_equivalent():
    """The arrange/relink-style equivalence gates must still refuse a move."""
    before = [_net(None, "R1.1", "R2.1", "U1.3"), _net(None, "R3.1", "R4.1")]
    after = [_net(None, "R1.1", "R2.1"), _net(None, "R3.1", "R4.1", "U1.3")]
    d = diff(before, after)
    assert d.moved and d.equivalent is False
    assert format_summary(d) != []         # "[] iff equivalent" still holds
    # `moved` counts on its own, not only through its MODIFIED hosts
    d.modified.clear()
    assert d.equivalent is False


def test_move_between_unnamed_nets_carries_no_risk():
    before = [_net(None, "R1.1", "R2.1", "U1.3"), _net(None, "R3.1", "R4.1")]
    after = [_net(None, "R1.1", "R2.1"), _net(None, "R3.1", "R4.1", "U1.3")]
    d = diff(before, after)
    _only(d, "moved", "modified")
    assert has_risk(d) is False            # ordinary wiring edit
    # not risk => "~", the module's marker for a plain membership change
    assert format_summary(d) == [
        "~ MOVE U1.3: <unnamed@R1.1> -> <unnamed@R3.1>"
    ]


@pytest.mark.parametrize("src, dst", [("VBUS", None), (None, "GND"),
                                      ("VBUS", "GND")])
def test_move_touching_a_named_net_is_risky(src, dst):
    before = [_net(src, "R1.1", "R2.1", "U1.3"), _net(dst, "R3.1", "R4.1")]
    after = [_net(src, "R1.1", "R2.1"), _net(dst, "R3.1", "R4.1", "U1.3")]
    d = diff(before, after)
    _only(d, "moved", "modified")
    assert has_risk(d) is True
    (line,) = format_summary(d)
    assert line.startswith("! MOVE U1.3: ")


def test_move_keeps_strict_gate_when_a_host_changes_name_in_the_same_edit():
    """GND loses its label WHILE absorbing a pin: every name on the line is
    unnamed, yet a named net was touched — exactly what SPLIT+MERGE gated."""
    before = [_net(None, "R1.1", "R2.1", "U1.3"),
              _net("GND", "C1.2", "C2.2", "C3.2")]
    after = [_net(None, "R1.1", "R2.1"),
             _net(None, "C1.2", "C2.2", "C3.2", "U1.3")]
    d = diff(before, after)
    _only(d, "moved", "modified")
    assert has_risk(d) is True
    assert format_summary(d) == [
        "! MOVE U1.3: <unnamed@R1.1> -> <unnamed@C1.2>",
        "~ GND-><unnamed@C1.2>: +U1.3 (3->4 pins)",   # the rename explains it
    ]
    # ... and the mirror image: the net the pin LEFT gains a name
    after = [_net("VREF", "R1.1", "R2.1"),
             _net(None, "C1.2", "C2.2", "C3.2", "U1.3")]
    d = diff([before[0], _net(None, "C1.2", "C2.2", "C3.2")], after)
    assert has_risk(d) is True
    assert format_summary(d)[0].startswith("! MOVE U1.3: ")


def test_move_needs_a_strict_minority_at_both_ends():
    """Pins leaving for a NEW net are a split; joining an equal-sized donor is
    a merge; a net absorbed whole is a merge. None of them is a move."""
    # no other source at the destination => plain SPLIT
    d = diff([_net("A", "R1.1", "R2.1", "U1.3")],
             [_net("A", "R1.1", "R2.1"), _net(None, "U1.3")])
    _only(d, "split")
    # destination's own source contributes no MORE than the mover (1 vs 1)
    d = diff([_net("A", "R1.1", "R2.1", "U1.3"), _net("B", "R3.1")],
             [_net("A", "R1.1", "R2.1"), _net("B", "R3.1", "U1.3")])
    _only(d, "split", "merged")
    assert format_summary(d) == [
        "! SPLIT A (3 pins) -> A(2) + B(1)",
        "! MERGE B + A -> B",
    ]
    # the source keeps nothing behind => plain MERGE
    d = diff([_net("A", "U1.3"), _net("B", "R3.1", "R4.1")],
             [_net("B", "R3.1", "R4.1", "U1.3")])
    _only(d, "merged")


def test_multi_pin_move_lists_every_pin_on_one_line():
    """2 pins out of a 6-pin net into a 5-pin net: 4 > 2 stay, 5 > 2 hosted."""
    before = [
        _net("BUS", "A.1", "B.1", "C.1", "D.1", "E.1", "F.1"),
        _net("AUX", "G.1", "H.1", "I.1", "J.1", "K.1"),
    ]
    after = [
        _net("BUS", "A.1", "B.1", "C.1", "D.1"),
        _net("AUX", "G.1", "H.1", "I.1", "J.1", "K.1", "E.1", "F.1"),
    ]
    d = diff(before, after)
    _only(d, "moved", "modified")
    (mv,) = d.moved                        # one pin GROUP, not one per pin
    assert mv.pins == frozenset({"E.1", "F.1"})
    # both hosts stay in `modified` — their membership did change
    aux, bus = d.modified
    assert (aux.after.name, aux.added, aux.removed) == (
        "AUX", frozenset({"E.1", "F.1"}), frozenset())
    assert (bus.after.name, bus.added, bus.removed) == (
        "BUS", frozenset(), frozenset({"E.1", "F.1"}))
    # ... but their lines are redundant with the MOVE and are not printed
    assert format_summary(d) == ["! MOVE E.1, F.1: BUS -> AUX"]


def test_move_long_pin_list_is_truncated():
    before = [
        _net("BIG", *(f"{c}.1" for c in "ABCDEFGHIJKL")),
        _net("OTH", *(f"{c}.2" for c in "ABCDEFG")),
    ]
    after = [
        _net("BIG", *(f"{c}.1" for c in "ABCDEFG")),
        _net("OTH", *(f"{c}.2" for c in "ABCDEFG"), *(f"{c}.1" for c in "HIJKL")),
    ]
    (line,) = format_summary(diff(before, after))
    # same 3-token cap as a MODIFIED delta, remainder collapsed
    assert line == "! MOVE H.1, I.1, J.1 (+2 more): BIG -> OTH"


def test_move_keeps_host_line_when_host_has_another_delta():
    before = [_net("A", "R1.1", "R2.1", "U1.3"), _net("B", "R3.1", "R4.1")]
    # A also gains C9.1 (a pin that was on no net before)
    after = [_net("A", "R1.1", "R2.1", "C9.1"),
             _net("B", "R3.1", "R4.1", "U1.3")]
    assert format_summary(diff(before, after)) == [
        "! MOVE U1.3: A -> B",
        "~ A: +C9.1 -U1.3 (3->3 pins)",    # B's line (move only) is dropped
    ]
    # a rename riding on a host is a second delta too
    after = [_net("A2", "R1.1", "R2.1"), _net("B", "R3.1", "R4.1", "U1.3")]
    assert format_summary(diff(before, after)) == [
        "! MOVE U1.3: A -> B",
        "~ A->A2: -U1.3 (3->2 pins)",
    ]


def test_move_combined_with_a_genuine_split_elsewhere():
    before = [
        _net("A", "R1.1", "R2.1", "U1.3"),
        _net("B", "R3.1", "R4.1"),
        _net("THR", "U2.1", "U2.2", "R7.1", "R7.2"),
    ]
    after = [
        _net("A", "R1.1", "R2.1"),
        _net("B", "R3.1", "R4.1", "U1.3"),
        _net("THR", "U2.1", "U2.2"),
        _net(None, "R7.1", "R7.2"),
    ]
    d = diff(before, after)
    _only(d, "split", "moved", "modified")
    assert d.split[0].before.name == "THR"
    assert format_summary(d) == [
        "! SPLIT THR (4 pins) -> THR(2) + <unnamed@R7.1>(2)",
        "! MOVE U1.3: A -> B",
    ]


def test_move_out_of_a_net_that_also_splits():
    """The moved pins leave the overlap: the SPLIT lists only real fragments."""
    before = [
        _net("A", "R1.1", "R2.1", "R3.1", "C1.1", "C2.1", "U1.3"),
        _net("B", "R7.1", "R8.1", "R9.1"),
    ]
    after = [
        _net("A", "R1.1", "R2.1", "R3.1"),
        _net(None, "C1.1", "C2.1"),            # broke off into a NEW net
        _net("B", "R7.1", "R8.1", "R9.1", "U1.3"),
    ]
    d = diff(before, after)
    _only(d, "split", "moved", "modified")
    assert [(v.display, n) for v, n in d.split[0].fragments] == [
        ("A", 3), ("<unnamed@C1.1>", 2)]
    assert format_summary(d) == [
        "! SPLIT A (6 pins) -> A(3) + <unnamed@C1.1>(2)",
        "! MOVE U1.3: A -> B",
    ]


def test_move_lines_sit_between_merge_and_modified():
    before = [
        _net("A", "R1.1", "R2.1", "U1.3"),
        _net("B", "R3.1", "R4.1"),
        _net("M1", "U5.1", "U5.2"),
        _net("M2", "U6.1"),
        _net("V", "U7.1", "U7.2"),
    ]
    after = [
        _net("A", "R1.1", "R2.1"),
        _net("B", "R3.1", "R4.1", "U1.3"),
        _net("M1", "U5.1", "U5.2", "U6.1"),
        _net("V", "U7.1", "U7.2", "U7.3"),
    ]
    assert format_summary(diff(before, after)) == [
        "! MERGE M1 + M2 -> M1",
        "! MOVE U1.3: A -> B",
        "~ V: +U7.3 (2->3 pins)",
    ]


def test_move_output_is_deterministic():
    """Same nets in any input order => the same entries and the same lines."""
    before = [
        _net("A", "R1.1", "R2.1", "R3.1", "U1.3", "U1.4"),
        _net("B", "R4.1", "R5.1", "R6.1", "U2.7"),
        _net(None, "R7.1", "R8.1", "R9.1"),
    ]
    after = [
        _net("A", "R1.1", "R2.1", "R3.1", "U2.7"),      # swap with B ...
        _net("B", "R4.1", "R5.1", "R6.1", "U1.3"),
        _net(None, "R7.1", "R8.1", "R9.1", "U1.4"),     # ... and one to unnamed
    ]
    d = diff(before, after)
    lines = format_summary(d)
    # sorted by the net the pins left, then the net they joined (named first)
    assert lines == [
        "! MOVE U1.3: A -> B",
        "! MOVE U1.4: A -> <unnamed@R7.1>",
        "! MOVE U2.7: B -> A",
    ]
    assert format_summary(diff(before, after)) == lines         # same twice
    again = diff(list(reversed(before)), list(reversed(after)))
    assert again.moved == d.moved and again.modified == d.modified
    assert format_summary(again) == lines


# --- model.Net coercion --------------------------------------------------------

def test_accepts_model_net_objects():
    before = [
        model.Net(name="STAT", members=[("R7", "1"), ("U2", "1")]),
        model.Net(name=None, members=[("R2", "1"), ("R3", "2")], is_named=False),
    ]
    after = [
        model.Net(name="STAT", members=[("R7", "1"), ("U2", "1")]),
        model.Net(name=None, members=[("R2", "1"), ("R3", "2")], is_named=False),
    ]
    d = diff(before, after)
    assert d.equivalent is True
    assert len(d.unchanged) == 2
    # unnamed model.Net renders with a member-pin anchor
    unnamed = [v for v in d.unchanged if v.name is None][0]
    assert unnamed.display == "<unnamed@R2.1>"


def test_model_net_rename_detected_by_membership():
    before = [model.Net(name="THR", members=[("U1", "1"), ("R1", "1")])]
    after = [model.Net(name="THERM", members=[("U1", "1"), ("R1", "1")])]
    d = diff(before, after)
    _only(d, "renamed")
    assert d.renamed[0].after.name == "THERM"


# --- format_summary limit / determinism ----------------------------------------

def test_format_summary_limit_truncates_with_marker():
    after = [_net(f"N{i}", f"U{i}.1") for i in range(10)]
    d = diff([], after)
    lines = format_summary(d, limit=3)
    assert len(lines) == 4
    assert lines[:3] == ["+ NEW N0 (1)", "+ NEW N1 (1)", "+ NEW N2 (1)"]
    assert lines[3] == "... (+7 more)"
    # limit=None disables truncation
    assert len(format_summary(d, limit=None)) == 10


def test_output_lists_are_deterministically_sorted():
    before = [_net("B", "X.1"), _net("A", "Y.1"), _net(None, "Z.1")]
    d = diff(before, [])
    # named first (alphabetical), unnamed last
    assert [v.display for v in d.removed] == ["A", "B", "<unnamed@Z.1>"]


def test_netview_display_named_and_unnamed():
    assert NetView(name="GND", pins=frozenset({"U1.4"})).display == "GND"
    v = NetView(name=None, pins=frozenset({"U1.10", "U1.2"}))
    # lexicographically smallest member pin anchors the placeholder
    assert v.display == "<unnamed@U1.10>"
