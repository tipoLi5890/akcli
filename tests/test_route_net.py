"""``route_net`` — deterministic orthogonal auto-route with pin-safe corners.

The non-coaxial companion of ``connect_and_label``: resolves two endpoints,
synthesizes L/Z vertices whose corner never lands on a placed pin tip (a
coincident corner silently merges nets — the trap docs used to teach by
hand), and optionally names the net once at the longest segment's midpoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli import netdiff, ops
from akcli.drivers import kicad_cli
from akcli.readers import kicad as kreader
from akcli.writers import kicad as kw

SYMBOLS = Path(__file__).parent / "fixtures" / "kicad" / "symbols"
DEVICE = str(SYMBOLS / "Device.kicad_sym")


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


def _doc(*ops_list, **envelope):
    return {"protocol_version": 1, "target_format": "kicad",
            "ops": list(ops_list), **envelope}


def _net_members(tgt: Path, name: str):
    for n in kreader.read_sch(str(tgt)).nets:
        if n.name == name or name in n.aliases:
            return set(n.members)
    return set()


# --------------------------------------------------------------------------- #
# corner geometry (pure)
# --------------------------------------------------------------------------- #
def test_route_points_coaxial_is_straight():
    assert kw._route_points((0, 0), (100, 0), "auto", set()) == [(0, 0), (100, 0)]
    assert kw._route_points((0, 0), (0, 100), "auto", set()) == [(0, 0), (0, 100)]


def test_route_points_styles():
    a, b = (0, 0), (100, 200)
    assert kw._route_points(a, b, "hv", set()) == [a, (100, 0), b]
    assert kw._route_points(a, b, "vh", set()) == [a, (0, 200), b]
    z = kw._route_points(a, b, "z", set())
    assert len(z) == 4 and z[0] == a and z[-1] == b
    # z splits the LONGER axis (y here): both corners share a mid y
    assert z[1][1] == z[2][1]


def test_route_points_auto_avoids_pin_corners():
    a, b = (0, 0), (100, 200)
    # default prefers hv
    assert kw._route_points(a, b, "auto", set())[1] == (100, 0)
    # hv corner occupied by a pin -> vh
    assert kw._route_points(a, b, "auto", {(100, 0)})[1] == (0, 200)
    # both L-corners occupied -> z fallback
    z = kw._route_points(a, b, "auto", {(100, 0), (0, 200)})
    assert len(z) == 4


# --------------------------------------------------------------------------- #
# end-to-end
# --------------------------------------------------------------------------- #
def test_route_connects_non_coaxial_pins_with_label(tmp_path):
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "route_net", "from": "R1.2", "to": "R2.1", "label": "MID"},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    # both pins are on the same net, named by the single mid-wire label
    assert _net_members(tgt, "MID") == {("R1", "2"), ("R2", "1")}


def test_route_is_idempotent_and_exact_net_delta(tmp_path):
    tgt = _blank_sch(tmp_path)
    seed = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "add_net_label", "name": "A", "at": "R1.1"},
        {"op": "add_net_label", "name": "B", "at": "R2.2"},
    )
    rs = kw.apply(seed, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    before = kreader.read_sch(str(tgt)).nets

    route = _doc({"op": "route_net", "from": "R1.2", "to": "R2.1",
                  "label": "MID"})
    rs = kw.apply(route, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    first = tgt.read_bytes()
    after = kreader.read_sch(str(tgt)).nets

    # exactly ONE new net; the seeded nets are untouched
    d = netdiff.diff(before, after)
    assert not d.equivalent
    assert _net_members(tgt, "A") == {("R1", "1")}
    assert _net_members(tgt, "B") == {("R2", "2")}
    assert _net_members(tgt, "MID") == {("R1", "2"), ("R2", "1")}

    rs = kw.apply(route, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    assert tgt.read_bytes() == first          # byte-identical re-apply


def test_route_auto_corner_avoids_third_pin(tmp_path):
    """A part parked exactly on the hv corner forces the vh route."""
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        # R3's pin 1 tip lands exactly on the hv corner (2000, 1150) of
        # R1.2 (1000,1150) -> R2.1 (2000,1850): body at (2000,1300), pin1 up.
        {"op": "place_component", "lib_id": "Device:R", "designator": "R3",
         "x_mil": 2000, "y_mil": 1300},
        {"op": "route_net", "from": "R1.2", "to": "R2.1", "label": "MID"},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs), [r.message for r in rs]
    # R3 must NOT be swallowed into MID
    assert _net_members(tgt, "MID") == {("R1", "2"), ("R2", "1")}


def test_route_point_endpoints_translate_in_groups():
    d = _doc({"op": "route_net", "group": "G", "from": [0, 0], "to": [400, 200]},
             groups={"G": {"origin": [1000, 1000]}})
    r = ops.resolve_groups(d)
    assert r["ops"][0]["from"] == [1000, 1000]
    assert r["ops"][0]["to"] == [1400, 1200]
    # pin-ref endpoints stay untouched
    d = _doc({"op": "route_net", "group": "G", "from": "R1.1", "to": [0, 0]},
             groups={"G": {"origin": [1000, 1000]}})
    assert ops.resolve_groups(d)["ops"][0]["from"] == "R1.1"


def test_route_same_point_fails():
    errs = ops.validate_oplist(_doc({"op": "route_net", "from": [0, 0],
                                     "to": [0, 0], "style": "diag"}))
    assert any('"auto", "hv", "vh", "z"' in e.message for e in errs)


def test_route_accepted_by_kicad_cli(tmp_path):
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    tgt = _blank_sch(tmp_path)
    d = _doc(
        {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
         "x_mil": 1000, "y_mil": 1000},
        {"op": "place_component", "lib_id": "Device:R", "designator": "R2",
         "x_mil": 2000, "y_mil": 2000},
        {"op": "route_net", "from": "R1.2", "to": "R2.1", "label": "MID"},
    )
    rs = kw.apply(d, str(tgt), apply=True, sources=[DEVICE])
    assert all(r.status == "ok" for r in rs)
    assert kicad_cli.erc(str(tgt)) is not None
