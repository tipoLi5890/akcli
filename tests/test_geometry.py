"""Tests for ``writers/geometry.py`` (SPEC §3.5).

Covers the integer-nm point transform (rotations + mirrors, rotate-then-mirror
order), the grid snap, the re-exports, and — the load-bearing bit — pin world
coordinates for a real symbol + instance loaded from the KiCad fixtures via
``readers.kicad_lib``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altium_kicad_cli import units
from altium_kicad_cli.model import Component
from altium_kicad_cli.readers import kicad, kicad_lib
from altium_kicad_cli.writers import geometry as g

FIXTURES = Path(__file__).parent / "fixtures" / "kicad"
DEVICE_SYM = FIXTURES / "symbols" / "Device.kicad_sym"
POWER_SYM = FIXTURES / "symbols" / "power.kicad_sym"
BOARD_V7 = FIXTURES / "board_v7.kicad_sch"


# ---------------------------------------------------------------------------
# transform_point: rotations
# ---------------------------------------------------------------------------
def test_rotation_matrices():
    # eeschema convention (kicad-cli-verified, see tests/test_kicad_parity.py):
    # +90 rotates counter-clockwise ON SCREEN = (x,y)->(y,-x) in +Y-down nm.
    pt = (100, 50)
    assert g.transform_point(pt, 0) == (100, 50)
    assert g.transform_point(pt, 90) == (50, -100)
    assert g.transform_point(pt, 180) == (-100, -50)
    assert g.transform_point(pt, 270) == (-50, 100)


def test_rotation_normalised_and_360():
    pt = (7, 3)
    assert g.transform_point(pt, 360) == g.transform_point(pt, 0)
    assert g.transform_point(pt, 450) == g.transform_point(pt, 90)
    assert g.transform_point(pt, -90) == g.transform_point(pt, 270)


def test_rotation_full_circle_returns_to_origin_value():
    pt = (123, -456)
    p = pt
    for _ in range(4):
        p = g.transform_point(p, 90)
    assert p == pt


def test_bad_rotation_raises():
    with pytest.raises(ValueError):
        g.transform_point((1, 1), 45)


# ---------------------------------------------------------------------------
# transform_point: mirrors
# ---------------------------------------------------------------------------
def test_mirror_x_negates_y():
    assert g.transform_point((100, 50), 0, "x") == (100, -50)


def test_mirror_y_negates_x():
    assert g.transform_point((100, 50), 0, "y") == (-100, 50)


def test_mirror_none_is_identity():
    assert g.transform_point((100, 50), 0, "none") == (100, 50)


def test_bad_mirror_raises():
    with pytest.raises(ValueError):
        g.transform_point((1, 1), 0, "z")


# ---------------------------------------------------------------------------
# rotate-then-mirror ORDER (must differ from mirror-then-rotate)
# ---------------------------------------------------------------------------
def test_rotate_then_mirror_order():
    pt = (100, 50)
    # rotate 90 -> (50, -100); then mirror x (negate y) -> (50, 100)
    assert g.transform_point(pt, 90, "x") == (50, 100)
    # mirror-then-rotate would give: mirror x -> (100, -50); rot 90 -> (-50, -100)
    assert g.transform_point(pt, 90, "x") != (-50, -100)


def test_rotate_then_mirror_y_order():
    pt = (100, 50)
    # rotate 90 -> (50, -100); then mirror y (negate x) -> (-50, -100)
    assert g.transform_point(pt, 90, "y") == (-50, -100)


# ---------------------------------------------------------------------------
# origin translation
# ---------------------------------------------------------------------------
def test_origin_translation():
    assert g.transform_point((10, 20), 0, "none", (1000, 2000)) == (1010, 2020)


def test_origin_applied_after_transform():
    # rotate 180 -> (-10, -20), then + origin
    assert g.transform_point((10, 20), 180, "none", (100, 100)) == (90, 80)


# ---------------------------------------------------------------------------
# transform_angle
# ---------------------------------------------------------------------------
def test_transform_angle_rotation():
    assert g.transform_angle(0, 90) == 90.0
    assert g.transform_angle(90, 90) == 180.0
    assert g.transform_angle(270, 90) == 0.0


def test_transform_angle_mirror():
    assert g.transform_angle(30, 0, "x") == 330.0   # -30 % 360
    assert g.transform_angle(30, 0, "y") == 150.0   # 180 - 30


# ---------------------------------------------------------------------------
# transform_child (position Y-flip + angle)
# ---------------------------------------------------------------------------
def test_transform_child_position_yflip():
    # +Y-up local (0, 100) -> +Y-down (0, -100), no transform, + origin
    (x, y), _ = g.transform_child((0, 100), 0, 0, "none", (5, 5))
    assert (x, y) == (5, -95)


def test_transform_child_angle():
    # angle: -local_angle then rotate/mirror; local 0 stays 0
    _, a = g.transform_child((0, 0), 0, 0, "none")
    assert a == 0.0


# ---------------------------------------------------------------------------
# grid_snap_nm
# ---------------------------------------------------------------------------
def test_grid_snap_basic():
    assert g.grid_snap_nm((2400, 2600), 1000) == (2000, 3000)


def test_grid_snap_default_50mil():
    assert g.DEFAULT_GRID_NM == 50 * units.NM_PER_MIL == 1_270_000
    # exactly on grid stays put
    assert g.grid_snap_nm((1_270_000, 2_540_000)) == (1_270_000, 2_540_000)
    # just off snaps to nearest
    assert g.grid_snap_nm((1_280_000, 2_530_000)) == (1_270_000, 2_540_000)


def test_grid_snap_negative():
    assert g.grid_snap_nm((-2400, -2600), 1000) == (-2000, -3000)


def test_grid_snap_disabled():
    assert g.grid_snap_nm((1234, 5678), 0) == (1234, 5678)


# ---------------------------------------------------------------------------
# re-exports
# ---------------------------------------------------------------------------
def test_reexports_are_units():
    assert g.mil_to_nm is units.mil_to_nm
    assert g.nm_to_mm_str is units.nm_to_mm_str
    assert g.mil_to_nm(1) == 25_400
    assert g.nm_to_mm_str(1_270_000) == "1.27"


# ---------------------------------------------------------------------------
# pin_world — symbol + instance from the KiCad fixtures
# ---------------------------------------------------------------------------
def _resolve(lib_id: str):
    lib = kicad_lib.read(DEVICE_SYM)
    return kicad_lib.resolve(lib_id, [lib])


def test_pin_world_resistor_unrotated():
    """R placed at (50.8, 63.5) mm: pin 1 -> (50.8, 59.69), pin 2 -> (50.8, 67.31)."""
    sym = _resolve("R")
    inst = Component(
        designator="R2",
        library_ref="R",
        x_mil=units.nm_to_mil(units.mm_to_nm(50.8)),   # 2000 mil
        y_mil=units.nm_to_mil(units.mm_to_nm(63.5)),   # 2500 mil
    )
    by_num = {p.number: p for p in sym.pins}

    wx1, wy1 = g.pin_world(sym, inst, by_num["1"])
    assert (wx1, wy1) == (units.mm_to_nm(50.8), units.mm_to_nm(59.69))

    wx2, wy2 = g.pin_world(sym, inst, by_num["2"])
    assert (wx2, wy2) == (units.mm_to_nm(50.8), units.mm_to_nm(67.31))


def test_pin_world_rotation_180():
    """A 180-deg rotation swaps which physical end pin 1 / pin 2 land on."""
    sym = _resolve("R")
    inst = Component(
        designator="R9",
        library_ref="R",
        x_mil=0,
        y_mil=0,
        rotation=180,
    )
    by_num = {p.number: p for p in sym.pins}
    # pin 1 local +Y-up (0, +150 mil) -> flip (0,-150) -> rot180 (0, +150)
    assert g.pin_world(sym, inst, by_num["1"]) == (0, units.mil_to_nm(150))
    assert g.pin_world(sym, inst, by_num["2"]) == (0, units.mil_to_nm(-150))


def test_pin_world_mirror_y():
    """Mirror y negates X; pins are on the Y axis so X stays 0 but check no crash."""
    sym = _resolve("R")
    inst = Component(
        designator="R5", library_ref="R", x_mil=100, y_mil=200, mirror="y"
    )
    by_num = {p.number: p for p in sym.pins}
    # local (0,150) -> flip (0,-150) -> mirror y negate x (0,-150) -> +origin
    wx, wy = g.pin_world(sym, inst, by_num["1"])
    assert wx == units.mil_to_nm(100)
    assert wy == units.mil_to_nm(200) - units.mil_to_nm(150)


def test_pin_world_matches_reader_world_coords():
    """Cross-check: geometry.pin_world == coords the reader computed.

    The reader stores already-transformed world pin coords on each placed
    component; recomputing them from the resolved symbol + instance must agree.
    (The reader now delegates to geometry.transform_point, so this guards the
    delegation plumbing — mil/nm conversions and the library Y-flip.)
    """
    lib = kicad_lib.read(DEVICE_SYM)
    sch = kicad.read_sch(BOARD_V7)

    checked = 0
    for comp in sch.components:
        if comp.library_ref != "Device:R":
            continue
        sym = kicad_lib.resolve(comp.library_ref, [lib])
        sympins = {p.number: p for p in sym.pins}
        for cpin in comp.pins:
            sp = sympins.get(cpin.number)
            assert sp is not None
            wx, wy = g.pin_world(sym, comp, sp)
            # reader keeps world coords in mils; compare in nm within rounding tol
            assert units.approx_eq(wx, units.mil_to_nm(cpin.x_mil), 50)
            assert units.approx_eq(wy, units.mil_to_nm(cpin.y_mil), 50)
            checked += 1
    assert checked >= 2  # board_v7 has multiple resistors with 2 pins each
