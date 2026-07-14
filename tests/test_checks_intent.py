"""Tests for design-intent assertions (checks/intent.py).

Covers the load() shape validation (actionable BAD_CONFIG / PROTOCOL_MISMATCH
errors), every run() finding code, a clean PASS in both modes, and the
snapshot -> load -> run round-trip (must yield no findings). Fixture builders
mirror test_erc.py.
"""

from __future__ import annotations

import json

import pytest

from akcli.checks import intent
from akcli.errors import AkcliError
from akcli.model import Component, Net, Pin, Schematic
from akcli.report import Severity


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _comp(designator: str, pin_numbers: list[str]) -> Component:
    pins = [Pin(number=n, name=None, x_mil=0.0, y_mil=0.0) for n in pin_numbers]
    return Component(
        designator=designator, library_ref="Device:U", x_mil=0.0, y_mil=0.0,
        pins=pins,
    )


def _net(name, members) -> Net:
    return Net(
        name=name,
        members=sorted(members),
        source_names=[name] if name else [],
        is_named=name is not None,
    )


def _sch(components, nets) -> Schematic:
    return Schematic(
        source_path="<test>", source_format="altium",
        components=components, nets=nets,
    )


def _two_net_sch() -> Schematic:
    """U1(1,2,3,4) / U2(1,2): SWCLK={U1.1,U2.1}, SWDIO={U1.2,U2.2}; U1.3/U1.4 free."""
    return _sch(
        [_comp("U1", ["1", "2", "3", "4"]), _comp("U2", ["1", "2"])],
        [
            _net("SWCLK", [("U1", "1"), ("U2", "1")]),
            _net("SWDIO", [("U1", "2"), ("U2", "2")]),
        ],
    )


def _write_intent(tmp_path, doc) -> str:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _spec(nets: dict, mode: str = "exact") -> intent.IntentSpec:
    """Build an IntentSpec straight from PinRef-tuple lists (test convenience).

    A value may also be an ``intent.NetSpec`` already, to exercise a per-net
    mode override without going through ``load()``.
    """
    out: dict[str, intent.NetSpec] = {}
    for name, val in nets.items():
        if isinstance(val, intent.NetSpec):
            out[name] = val
        else:
            out[name] = intent.NetSpec(
                members=[intent.Member(ref=r, pin=p) for r, p in val], mode=mode,
            )
    return intent.IntentSpec(nets=out, mode=mode)


def _codes(findings) -> list[str]:
    return [f.code for f in findings]


# ---------------------------------------------------------------------------
# load(): valid documents
# ---------------------------------------------------------------------------
def test_load_valid_defaults_to_exact(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"SWCLK": ["U1.1", "U2.1"]},
    })
    spec = intent.load(path)
    assert spec.mode == "exact"
    assert spec.protocol_version == 1
    assert spec.nets.keys() == {"SWCLK"}
    swclk = spec.nets["SWCLK"]
    assert swclk.mode == "exact"
    assert [m.token for m in swclk.members] == ["U1.1", "U2.1"]
    assert all(not m.is_wildcard for m in swclk.members)


def test_load_valid_subset_and_dotted_pin(tmp_path):
    # split on the FIRST dot: pin numbers may themselves contain dots
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "mode": "subset",
        "nets": {"AIN": ["U1.P0.25"]},
    })
    spec = intent.load(path)
    assert spec.mode == "subset"
    assert spec.nets["AIN"].mode == "subset"
    assert [m.token for m in spec.nets["AIN"].members] == ["U1.P0.25"]


def test_load_dedupes_repeated_members(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": ["U1.1", "U1.1", "U2.1"]},
    })
    spec = intent.load(path)
    assert [m.token for m in spec.nets["GND"].members] == ["U1.1", "U2.1"]


# ---------------------------------------------------------------------------
# load(): actionable errors
# ---------------------------------------------------------------------------
def test_load_invalid_json(tmp_path):
    p = tmp_path / "intent.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        intent.load(str(p))
    assert ei.value.code == "BAD_CONFIG"
    assert "invalid intent JSON" in ei.value.message


def test_load_root_must_be_object(tmp_path):
    p = tmp_path / "intent.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(AkcliError) as ei:
        intent.load(str(p))
    assert ei.value.code == "BAD_CONFIG"


def test_load_wrong_protocol_version(tmp_path):
    path = _write_intent(tmp_path, {"protocol_version": 2, "nets": {}})
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_load_missing_protocol_version(tmp_path):
    path = _write_intent(tmp_path, {"nets": {"A": ["U1.1"]}})
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_load_unknown_top_level_key(tmp_path):
    # typo "net" (vs "nets") must not silently assert nothing
    path = _write_intent(tmp_path, {"protocol_version": 1, "net": {}})
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "net" in ei.value.message


def test_load_bad_mode(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1, "mode": "strict", "nets": {},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "strict" in ei.value.message


def test_load_nets_must_be_object(tmp_path):
    path = _write_intent(tmp_path, {"protocol_version": 1, "nets": ["U1.1"]})
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"


@pytest.mark.parametrize("bad_member", ["U1", "U1.", ".1", 7])
def test_load_bad_member_shape(tmp_path, bad_member):
    path = _write_intent(tmp_path, {
        "protocol_version": 1, "nets": {"X": [bad_member]},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "'X'" in ei.value.message  # names the offending net


def test_load_empty_member_list(tmp_path):
    path = _write_intent(tmp_path, {"protocol_version": 1, "nets": {"X": []}})
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"


def test_load_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        intent.load(str(tmp_path / "nope.json"))


# ---------------------------------------------------------------------------
# run(): clean pass
# ---------------------------------------------------------------------------
def test_clean_pass_exact_mode():
    sch = _two_net_sch()
    spec = _spec({
        "SWCLK": [("U1", "1"), ("U2", "1")],
        "SWDIO": [("U1", "2"), ("U2", "2")],
    })
    assert intent.run(sch, spec) == []


def test_clean_pass_subset_mode_ignores_extra_actual_members():
    sch = _two_net_sch()
    # intent lists only one of SWCLK's two pins; subset mode accepts containment
    spec = _spec({"SWCLK": [("U1", "1")]}, mode="subset")
    assert intent.run(sch, spec) == []


# ---------------------------------------------------------------------------
# run(): each finding code
# ---------------------------------------------------------------------------
def test_pin_unknown_is_error():
    sch = _two_net_sch()
    spec = _spec({"SWCLK": [("U1", "1"), ("U9", "1"), ("U2", "1")]})
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_PIN_UNKNOWN]
    f = out[0]
    assert f.severity is Severity.ERROR
    assert f.refs == ["U9.1"]
    assert "U9.1" in f.message


def test_net_not_found():
    sch = _two_net_sch()
    # U1.3 / U1.4 exist but sit on no net at all
    spec = _spec({"NC_BUS": [("U1", "3"), ("U1", "4")]})
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_NET_NOT_FOUND]
    assert out[0].severity is Severity.ERROR
    assert out[0].refs == ["U1.3", "U1.4"]


def test_all_pins_unknown_reports_unknown_and_not_found():
    sch = _two_net_sch()
    spec = _spec({"GHOST": [("U9", "1")]})
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_PIN_UNKNOWN, intent.INTENT_NET_NOT_FOUND]


def test_missing_member():
    sch = _two_net_sch()
    # U1.3 exists in the schematic but is NOT on SWCLK
    spec = _spec({"SWCLK": [("U1", "1"), ("U2", "1"), ("U1", "3")]})
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_MISSING_MEMBER]
    f = out[0]
    assert f.severity is Severity.ERROR
    assert f.refs == ["U1.3"]
    assert "SWCLK" in f.message  # names both the intent net and the actual net


def test_extra_member_exact_mode():
    sch = _two_net_sch()
    spec = _spec({"SWCLK": [("U1", "1")]})  # actual SWCLK also has U2.1
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_EXTRA_MEMBER]
    assert out[0].severity is Severity.ERROR
    assert out[0].refs == ["U2.1"]


def test_subset_mode_still_reports_missing():
    sch = _two_net_sch()
    spec = _spec({"SWCLK": [("U1", "1"), ("U1", "3")]}, mode="subset")
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_MISSING_MEMBER]


def test_nets_shorted():
    # both intent nets resolve to the one actual net -> shorted against intent
    sch = _sch(
        [_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])],
        [_net("BLOB", [("U1", "1"), ("U1", "2"), ("U2", "1"), ("U2", "2")])],
    )
    spec = _spec({
        "SWCLK": [("U1", "1"), ("U2", "1")],
        "SWDIO": [("U1", "2"), ("U2", "2")],
    }, mode="subset")
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_NETS_SHORTED]
    f = out[0]
    assert f.severity is Severity.ERROR
    assert f.refs == ["SWCLK", "SWDIO"]
    assert "BLOB" in f.message


def test_membership_match_beats_display_name():
    # an actual net NAMED "SWCLK" exists, but the intent's pins live elsewhere;
    # matching is by membership, so the mismatch surfaces as MISSING/EXTRA
    sch = _sch(
        [_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])],
        [
            _net("SWCLK", [("U1", "2"), ("U2", "2")]),
            _net("OTHER", [("U1", "1"), ("U2", "1")]),
        ],
    )
    spec = _spec({"SWCLK": [("U1", "1"), ("U2", "1")]})
    assert intent.run(sch, spec) == []  # membership set matches net "OTHER"


def test_unnamed_actual_net_labelled_by_stable_id():
    unnamed = Net(name=None, members=[("U1", "1"), ("U2", "1")], is_named=False)
    sch = _sch([_comp("U1", ["1"]), _comp("U2", ["1", "2"])], [unnamed])
    spec = _spec({"SWCLK": [("U1", "1")]})
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_EXTRA_MEMBER]
    assert unnamed.stable_id in out[0].message


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------
def test_snapshot_named_only_by_default():
    sch = _sch(
        [_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])],
        [
            _net("SWCLK", [("U1", "1"), ("U2", "1")]),
            Net(name=None, members=[("U1", "2"), ("U2", "2")], is_named=False),
        ],
    )
    doc = intent.snapshot(sch)
    assert doc["protocol_version"] == intent.PROTOCOL_VERSION
    assert doc["mode"] == "exact"
    assert doc["nets"] == {"SWCLK": ["U1.1", "U2.1"]}


def test_snapshot_include_unnamed_keys_by_stable_id():
    unnamed = Net(name=None, members=[("U1", "2"), ("U2", "2")], is_named=False)
    sch = _sch(
        [_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])],
        [_net("SWCLK", [("U1", "1"), ("U2", "1")]), unnamed],
    )
    doc = intent.snapshot(sch, include_unnamed=True)
    assert doc["nets"][unnamed.stable_id] == ["U1.2", "U2.2"]


def test_snapshot_disambiguates_duplicate_display_names():
    # same local label on two sheets -> two distinct nets, one display name
    a = _net("STAT", [("U1", "1"), ("U2", "1")])
    b = _net("STAT", [("U1", "2"), ("U2", "2")])
    sch = _sch([_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])], [a, b])
    doc = intent.snapshot(sch)
    assert len(doc["nets"]) == 2
    assert sorted(doc["nets"]) == sorted(["STAT", f"STAT@{b.stable_id}"])


def test_snapshot_roundtrip_yields_no_findings(tmp_path):
    sch = _two_net_sch()
    path = _write_intent(tmp_path, intent.snapshot(sch))
    spec = intent.load(path)
    assert intent.run(sch, spec) == []


def test_snapshot_roundtrip_include_unnamed(tmp_path):
    sch = _sch(
        [_comp("U1", ["1", "2"]), _comp("U2", ["1", "2"])],
        [
            _net("SWCLK", [("U1", "1"), ("U2", "1")]),
            Net(name=None, members=[("U1", "2"), ("U2", "2")], is_named=False),
        ],
    )
    path = _write_intent(tmp_path, intent.snapshot(sch, include_unnamed=True))
    spec = intent.load(path)
    assert intent.run(sch, spec) == []


# ---------------------------------------------------------------------------
# load(): object net-value form (members + per-net mode override)
# ---------------------------------------------------------------------------
def test_load_object_net_value_defaults_to_document_mode(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1, "mode": "exact",
        "nets": {"GND": {"members": ["U1.1", "U2.1"]}},
    })
    spec = intent.load(path)
    assert spec.nets["GND"].mode == "exact"
    assert [m.token for m in spec.nets["GND"].members] == ["U1.1", "U2.1"]


def test_load_object_net_value_mode_overrides_document_mode(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1, "mode": "exact",
        "nets": {
            "GND": {"members": ["U1.1"], "mode": "subset"},
            "SWCLK": ["U2.1"],  # plain list still uses the document mode
        },
    })
    spec = intent.load(path)
    assert spec.nets["GND"].mode == "subset"
    assert spec.nets["SWCLK"].mode == "exact"


def test_load_object_net_value_bad_mode(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": {"members": ["U1.1"], "mode": "strict"}},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "strict" in ei.value.message


def test_load_object_net_value_unknown_key(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": {"members": ["U1.1"], "typo": "x"}},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "typo" in ei.value.message


def test_load_object_net_value_members_not_list(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": {"members": "U1.1"}},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"


def test_load_net_value_wrong_type(tmp_path):
    # neither a list nor an object -> same BAD_CONFIG load-error path
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": 7},
    })
    with pytest.raises(AkcliError) as ei:
        intent.load(path)
    assert ei.value.code == "BAD_CONFIG"
    assert "'GND'" in ei.value.message


def test_load_wildcard_member_parses(tmp_path):
    path = _write_intent(tmp_path, {
        "protocol_version": 1,
        "nets": {"GND": ["R*.1", "U?.2"]},
    })
    spec = intent.load(path)
    members = spec.nets["GND"].members
    assert [m.token for m in members] == ["R*.1", "U?.2"]
    assert all(m.is_wildcard for m in members)


# ---------------------------------------------------------------------------
# run(): wildcard members
# ---------------------------------------------------------------------------
def _wildcard_sch() -> Schematic:
    """R1(1)/R2(1)/R3(1)/C1(1) all on GND; U1(1,2) free-floating."""
    return _sch(
        [_comp("R1", ["1"]), _comp("R2", ["1"]), _comp("R3", ["1"]),
         _comp("C1", ["1"]), _comp("U1", ["1", "2"])],
        [_net("GND", [("R1", "1"), ("R2", "1"), ("R3", "1"), ("C1", "1")])],
    )


def test_wildcard_hit_subset_mode_clean_pass():
    sch = _wildcard_sch()
    spec = _spec({"GND": [("R*", "1")]}, mode="subset")
    assert intent.run(sch, spec) == []


def test_wildcard_miss_reports_missing_member_naming_the_pattern():
    sch = _wildcard_sch()
    # no designator starting with "X" exists anywhere -> the pattern itself
    # is reported, never an expansion
    spec = _spec({
        "GND": [("R1", "1"), ("X*", "1")],
    }, mode="subset")
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_MISSING_MEMBER]
    assert out[0].refs == ["X*.1"]
    assert "X*.1" in out[0].message


def test_wildcard_does_not_trigger_pin_unknown():
    # a wildcard matching zero pins is MISSING, never PIN_UNKNOWN
    sch = _wildcard_sch()
    spec = _spec({"GND": [("R1", "1"), ("X*", "1")]}, mode="subset")
    out = intent.run(sch, spec)
    assert intent.INTENT_PIN_UNKNOWN not in _codes(out)


def test_wildcard_ignored_for_extra_member_computation_in_exact_mode():
    # exact mode: only literal members are subtracted from the actual net's
    # membership, so pins that match ONLY via the wildcard still show as extra
    sch = _wildcard_sch()
    spec = _spec({"GND": [("R*", "1")]})  # exact mode (default)
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_EXTRA_MEMBER]
    assert out[0].refs == ["C1.1", "R1.1", "R2.1", "R3.1"]


def test_wildcard_plus_literal_exact_mode_extras_exclude_only_literal():
    sch = _wildcard_sch()
    spec = _spec({"GND": [("R*", "1"), ("C1", "1")]})  # C1.1 named explicitly
    out = intent.run(sch, spec)
    assert _codes(out) == [intent.INTENT_EXTRA_MEMBER]
    # R1/R2/R3 matched only via the wildcard -> still "extra"; C1.1 is literal -> excluded
    assert out[0].refs == ["R1.1", "R2.1", "R3.1"]


def test_wildcard_per_net_mode_override_beats_document_mode(tmp_path):
    # document mode is exact, but this net overrides to subset via the
    # object net-value form -> wildcard-matched extras are not reported
    path = _write_intent(tmp_path, {
        "protocol_version": 1, "mode": "exact",
        "nets": {"GND": {"members": ["R*.1"], "mode": "subset"}},
    })
    sch = _wildcard_sch()
    spec = intent.load(path)
    assert intent.run(sch, spec) == []


# --------------------------------------------------------------------------- #
# review-round regression: wildcard votes must not outvote literal members
# --------------------------------------------------------------------------- #
def test_wildcard_cannot_drag_net_onto_wrong_actual_net():
    # GND = {U1.4, R1.2}; VCC = {R2.2..R5.2}. Intent 'GND' lists the literal
    # U1.4 plus wildcard R*.2 — the wildcard matches four VCC pins, but the
    # literal must dominate net selection: GND is checked, not VCC.
    sch = _sch(
        [_comp("U1", ["4"]), *[_comp(f"R{i}", ["2"]) for i in range(1, 6)]],
        [_net("GND", [("U1", "4"), ("R1", "2")]),
         _net("VCC", [(f"R{i}", "2") for i in range(2, 6)])],
    )
    spec = _spec({"GND": intent.NetSpec(
        members=[intent.Member(ref="U1", pin="4"),
                 intent.Member(ref="R*", pin="2")],
        mode="subset")}, mode="subset")
    findings = intent.run(sch, spec)
    # subset mode against the REAL GND net: both members present -> clean
    assert findings == []


def test_all_wildcard_intent_still_resolves():
    sch = _sch([_comp("R1", ["1"]), _comp("R2", ["1"])],
               [_net("BUS", [("R1", "1"), ("R2", "1")])])
    spec = _spec({"BUS": intent.NetSpec(
        members=[intent.Member(ref="R*", pin="1")],
        mode="subset")}, mode="subset")
    assert intent.run(sch, spec) == []
