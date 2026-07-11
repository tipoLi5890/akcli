"""Tests for netdiff — connectivity delta between two inferred netlists.

Fixtures are built DIRECTLY as (name, pin-set) pairs (the coercion netdiff
accepts alongside real ``model.Net`` objects), so every classification —
UNCHANGED / RENAMED / MODIFIED (incl. rename+modify combined) / SPLIT /
MERGED / CREATED / REMOVED — is exercised without touching any reader.
One test feeds genuine ``model.Net`` inputs to pin down the duck-typed
coercion (PinRef tuples, is_named=False => unnamed).
"""

from __future__ import annotations

from altium_kicad_cli import model
from altium_kicad_cli.netdiff import NetView, diff, format_summary, has_risk


# --- helpers -----------------------------------------------------------------

def _net(name, *pins):
    """Fixture net: name (None => unnamed) + 'REF.PIN' member strings."""
    return (name, set(pins))


def _only(d, *buckets):
    """Assert exactly the given NetDiff buckets are non-empty ('unchanged' free)."""
    all_buckets = ("renamed", "modified", "split", "merged", "created", "removed")
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
