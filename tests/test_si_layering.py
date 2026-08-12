"""Pins the CURRENT relationship between the two SI-notation parsers.

``calc/si.py`` (engineering-notation values for calculators/sim/review) and
``parts/value_parse.py`` (BOM value normalization) each carry a hand-written
prefix→multiplier table.  The tables encode the same physics; a one-keystroke
divergence (``1e-12`` vs ``1e-11``) would be a silent physical error with no
cross-check.  Their TOLERANCE policies, however, are deliberately different
layers — strict-raise vs permissive-None — and must NOT be merged.

This file freezes both facts before the 0.21 consolidation (single base table
in a leaf module, both facades keep their own policy):

* shared prefixes agree on the multiplier value;
* the deliberate differences are POSITIVE assertions, not folklore:
  - ``calc/si`` raises on ``1_000`` (underscores are a classic ohm/kilo-ohm
    trap), ``value_parse`` returns None ("cannot verify");
  - ``value_parse`` does NOT recognize ``T`` (tera) — consolidating the
    tables must not silently grant it (``4T7`` = 4.7e12 would be absurd as a
    BOM part value);
  - ``value_parse`` rejects scientific notation and bare unitless numbers.
"""

from __future__ import annotations

import pytest

from akcli.calc import si
from akcli.calc.si import parse_value
from akcli.parts import value_parse as vp


def test_shared_prefix_multipliers_agree():
    shared = set(si._MULT) & set(vp._MULTIPLIERS)
    assert shared >= {"p", "n", "u", "µ", "μ", "m", "k", "K", "M", "G", "R"}
    diverged = {c: (si._MULT[c], vp._MULTIPLIERS[c])
                for c in sorted(shared) if si._MULT[c] != vp._MULTIPLIERS[c]}
    assert not diverged, f"multiplier tables disagree: {diverged}"


@pytest.mark.parametrize("token", ["4.7k", "10u", "1M", "1m", "3n3", "22p"])
def test_common_tokens_parse_identically(token):
    assert vp.parse(token) is not None, token
    assert vp.parse(token).value == pytest.approx(parse_value(token))


def test_si_is_strict_where_value_parse_is_permissive():
    from akcli.calc import CalcError

    with pytest.raises(CalcError):
        parse_value("1_000")            # rejected: 1_000 reads as 1 ohm trap
    assert vp.parse("1_000") is None    # BOM side: "cannot verify", not crash


def test_value_parse_has_no_tera():
    assert parse_value("4T7") == pytest.approx(4.7e12)   # si: full SI ladder
    assert vp.parse("4T7") is None                       # BOM: T is absurd
    assert "T" in si._MULT and "T" not in vp._MULTIPLIERS


def test_value_parse_rejects_scientific_and_bare_numbers():
    assert parse_value("2.2e-6") == pytest.approx(2.2e-6)
    assert vp.parse("2.2e-6") is None
    assert parse_value("470") == pytest.approx(470.0)
    assert vp.parse("470") is None
