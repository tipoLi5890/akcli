"""The refdes-prefix vocabulary has ONE home: ``classify``.

Three copies used to coexist (``classify`` canonical, ``bom_policy`` and
``ops`` unconverged); their only behavioral divergence was ``$``-prefixed
synthesized designators (Altium parts with no designator record), where the
copies returned "" and silently unclassified the part.  The 0.21 convergence
made all three names the same function object; the corpus below stays as
regression input, and the ``$`` case now asserts the FIXED behavior
everywhere.
"""

from __future__ import annotations

import itertools

import pytest

from akcli.bom_policy import refdes_prefix as bom_prefix
from akcli.checks.bom import _parse_refdes
from akcli.classify import refdes_prefix as classify_prefix
from akcli.classify import split_refdes
from akcli.ops import _refdes_prefix as ops_prefix

_PREFIXES = ["R", "C", "L", "U", "Q", "D", "J", "TP", "X", "Y", "K", "FB",
             "SW", "U_MAIN", "J_USB_C", "r", "u"]
_SUFFIXES = ["1", "12", "0", "?", "2A", "10B", ""]
_ODDBALLS = ["", "#PWR01", "#FLG02", "1N4148", "$", "?", "R1.2"]


def _corpus() -> list[str]:
    return [p + s for p, s in itertools.product(_PREFIXES, _SUFFIXES)] + _ODDBALLS


def test_all_names_are_one_function():
    assert bom_prefix is classify_prefix
    assert ops_prefix is classify_prefix
    assert _parse_refdes is split_refdes


def test_corpus_regression():
    """The corpus that mapped the old divergence keeps pinning behavior."""
    for ref in _corpus():
        assert isinstance(classify_prefix(ref), str)


@pytest.mark.parametrize("ref,want", [("$U7", "U"), ("$R12", "R")])
def test_synthesized_designators_classify_everywhere(ref, want):
    """The old copies returned "" here — the fix now applies at every seam."""
    assert classify_prefix(ref) == want
    assert bom_prefix(ref) == want
    assert ops_prefix(ref) == want


def test_split_refdes_cross_check():
    """Where a refdes splits, its split prefix agrees with refdes_prefix."""
    for ref in _corpus():
        parts = split_refdes(ref)
        if parts is not None:
            assert parts[0].upper() == classify_prefix(ref), ref
    assert split_refdes("R12") == ("R", 12)
    assert split_refdes("J_USB_C") is None
    assert split_refdes("$U7") is None
