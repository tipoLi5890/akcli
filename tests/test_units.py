"""Tests for coordinate/unit conversions (units.py)."""

from __future__ import annotations

from akcli import units


def test_locked_constants():
    assert units.ALTIUM_SCH_MIL_PER_UNIT == 10.0
    assert units.NM_PER_MIL == 25_400
    assert units.NM_PER_MM == 1_000_000
    assert abs(units.MIL_PER_MM - 1 / 0.0254) < 1e-9


def test_altium_to_mil_known_pin_pitch():
    # A standard 200-mil pin has PinLength=20 Altium units -> 200 mil.
    assert units.altium_to_mil(20) == 200.0
    assert units.altium_to_mil(1150) == 11500.0  # ~11.5 in sheet extent


def test_altium_to_mil_with_frac():
    # frac is 1/100000 of a unit; 5 units + half a unit = 5.5 units * 10 = 55 mil
    assert units.altium_to_mil(5, 50000) == 55.0


def test_mil_to_nm_exact():
    assert units.mil_to_nm(1) == 25_400
    assert units.mil_to_nm(50) == 1_270_000
    assert units.nm_to_mil(25_400) == 1.0


def test_nm_to_mm_str_strips_zeros():
    assert units.nm_to_mm_str(1_000_000) == "1"
    assert units.nm_to_mm_str(1_270_000) == "1.27"
    assert units.nm_to_mm_str(0) == "0"
    assert units.nm_to_mm_str(-1_270_000) == "-1.27"
    assert units.nm_to_mm_str(500_000) == "0.5"


def test_snap_mil_to_grid():
    assert units.snap_mil(123, 50) == 100
    assert units.snap_mil(126, 50) == 150
    assert units.snap_mil(75, 50) == 100  # round-half handled by python round
    assert units.snap_mil(40, 0) == 40    # grid <= 0 is a no-op


def test_approx_eq_tolerance():
    assert units.approx_eq(0, 100, 100) is True
    assert units.approx_eq(0, 101, 100) is False
    assert units.approx_eq(25_400, 25_399, 5) is True
