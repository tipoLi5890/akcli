"""Invariant sweep over EVERY Altium schematic fixture (auto-discovering).

Without an Altium Designer install (or any independent Altium parser exposed
via CLI — KiCad's importer is GUI-only), the strongest offline verification of
the Altium reader is invariants + determinism over the whole fixture corpus:

* every net member references a real component pin,
* net membership is duplicate-free and deterministically ordered,
* reading the same file twice yields byte-identical netlists,
* the CSV/Protel exports agree with the inferred netlist,
* the malformed corpus fails LOUDLY (AkcliError), never crashes or half-parses.

New ``.SchDoc`` fixtures dropped into ``tests/fixtures/`` are swept
automatically. The true independent cross-check (Altium's own netlist export)
still needs a real AD install — documented in the altium-interop skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli import exporters
from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.readers import altium_sch

FIXROOT = Path(__file__).parent / "fixtures"

GOOD = sorted(p for p in FIXROOT.rglob("*.SchDoc") if "malformed" not in p.parts)
# zero_length_stream is by design a VALID-but-empty container (see
# test_cfbf.test_zero_length_stream_is_valid_but_empty) — not a parse failure.
BAD = sorted(
    p for p in (FIXROOT / "malformed").glob("*.SchDoc")
    if p.name != "zero_length_stream.SchDoc"
)

assert GOOD, "no Altium fixtures found — sweep would be vacuous"


def _netlist_snapshot(sch) -> list[tuple]:
    return [
        (net.name, tuple(tuple(m) for m in net.members), tuple(net.aliases))
        for net in sch.nets
    ]


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_members_reference_real_pins(path):
    sch = altium_sch.read(path)
    pins = {(c.designator, p.number) for c in sch.components for p in c.pins}
    for net in sch.nets:
        for member in net.members:
            assert tuple(member) in pins, (
                f"{path.name}: net {net.name!r} references {member} "
                f"which no component pin provides"
            )


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_membership_is_sorted_and_duplicate_free(path):
    sch = altium_sch.read(path)
    for net in sch.nets:
        members = [tuple(m) for m in net.members]
        assert members == sorted(set(members)), (
            f"{path.name}: net {net.name!r} membership not a sorted set"
        )


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_read_is_deterministic(path):
    first = altium_sch.read(path)
    second = altium_sch.read(path)
    assert _netlist_snapshot(first) == _netlist_snapshot(second)
    assert [c.designator for c in first.components] == [
        c.designator for c in second.components
    ]


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_pin_coordinates_are_finite(path):
    sch = altium_sch.read(path)
    for comp in sch.components:
        for pin in comp.pins:
            assert pin.x_mil == pin.x_mil and abs(pin.x_mil) < 1e9  # not NaN/inf
            assert pin.y_mil == pin.y_mil and abs(pin.y_mil) < 1e9


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_csv_export_matches_netlist(path):
    sch = altium_sch.read(path)
    csv_text = exporters.to_csv(sch)
    rows = {
        tuple(line.split(","))
        for line in csv_text.strip().splitlines()[1:]  # skip header
        if line
    }
    expected = {
        (exporters._net_name(net), ref, pin)
        for net in sch.nets
        for (ref, pin) in net.members
    }
    assert rows == expected, f"{path.name}: CSV rows diverge from inferred netlist"


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_protel_export_is_deterministic_and_complete(path):
    sch = altium_sch.read(path)
    a = exporters.to_protel(sch)
    b = exporters.to_protel(sch)
    assert a == b
    for net in sch.nets:
        for (ref, pin) in net.members:
            assert f"{ref}-{pin}" in a, (
                f"{path.name}: {ref}.{pin} missing from Protel export"
            )


@pytest.mark.parametrize("path", BAD, ids=lambda p: p.name)
def test_malformed_corpus_fails_loudly(path):
    with pytest.raises(AkcliError):
        altium_sch.read(path)
