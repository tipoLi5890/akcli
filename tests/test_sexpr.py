"""Tests for the iterative S-expression parser (readers/sexpr.py, SPEC §3.4).

Key requirements:
* a non-trivial KiCad sample round-trips **byte-for-byte** (``dumps(parse(s)) == s``);
* every committed malformed fixture
  ``tests/fixtures/malformed/{deeply_nested,huge_atom,unterminated_quote}.kicad_sch``
  raises a STRUCTURED error (``AkcliError``) with the right code -- never a hang,
  a ``RecursionError``, or an OOM -- within a small time budget (``signal.alarm``).
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from pathlib import Path

import pytest

from akcli.errors import ERROR_CODES, AkcliError
from akcli.readers import sexpr
from akcli.readers.sexpr import SNode, dumps, parse
from akcli.safety import MAX_ATOM_BYTES, MAX_SEXPR_DEPTH

FIX = Path(__file__).resolve().parent / "fixtures"
MALFORMED = FIX / "malformed"


@contextmanager
def time_budget(seconds: int = 10):
    """Fail (rather than hang) if the body runs longer than ``seconds``.

    Uses ``signal.alarm`` where available (POSIX); a no-op on platforms without
    it (e.g. Windows), where the structured-error guards are still asserted.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):  # pragma: no cover - only fires on a real hang
        raise TimeoutError("operation exceeded time budget (possible infinite loop)")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# A representative .kicad_sch slice: nested lists, varied indentation (tabs),
# blank line, quoted strings WITH escapes, an empty list, numbers, trailing NL.
SAMPLE = (
    "(kicad_sch\n"
    "\t(version 20230121)\n"
    '\t(generator "eeschema")\n'
    "\n"
    '\t(uuid "0a1b2c3d-0000-4000-8000-000000000000")\n'
    '\t(paper "A4")\n'
    "\t(lib_symbols\n"
    '\t\t(symbol "Device:R"\n'
    '\t\t\t(property "Reference" "R" (at 0 0 0))\n'
    '\t\t\t(property "Value" "R \\"big\\" \\\\ \xce\xa9")\n'  # escaped quote + backslash + bytes
    "\t\t\t(pin passive line (at 0 3.81 270) (length 1.27))\n"
    "\t\t)\n"
    "\t)\n"
    '\t(symbol (lib_id "Device:R") (at 100 100 0) (unit 1)\n'
    '\t\t(property "Reference" "R1" (at 102 99 0))\n'
    "\t\t(no_children ())\n"
    "\t)\n"
    "\t(wire (pts (xy 100 100) (xy 120 100)))\n"
    ")\n"
)


# --- byte-identical round-trip (the headline gate) --------------------------
def test_round_trip_byte_identical():
    node = parse(SAMPLE)
    assert dumps(node) == SAMPLE


@pytest.mark.parametrize(
    "text",
    [
        "(a)",
        "(a b c)",
        "()",
        "  (a)  ",          # document prefix + suffix trivia
        "(a)\n",            # trailing newline suffix
        "(outer (inner 1 2) (other))",
        '(q "with spaces" "esc \\" end")',
        "(a\n  (b\n    (c))\n)\n",   # mixed indentation
        "atom",             # bare top-level atom
        '"just a string"',  # quoted top-level atom
        "(a)\ntrailing junk here",   # second top-level form -> kept as suffix
    ],
)
def test_round_trip_variants_are_byte_identical(text):
    assert dumps(parse(text)) == text


# --- structure / accessors --------------------------------------------------
def test_atom_text_preserved_and_value_decoded():
    node = parse('(prop "a \\"b\\" \\\\ c" bare 42)')
    children = list(node)
    # children[0] is the head atom "prop"
    quoted = children[1]
    assert quoted.is_atom
    assert quoted.text == '"a \\"b\\" \\\\ c"'        # raw text intact
    assert quoted.value == 'a "b" \\ c'               # decoded
    assert children[2].value == "bare"
    assert children[3].value == "42"


def test_tag_find_and_iteration_order():
    node = parse('(symbol (a 1) (b 2) (a 3))')
    assert node.tag == "symbol"
    assert node.find("b").tag == "b"
    assert [c.children[1].value for c in node.find_all("a")] == ["1", "3"]
    # child order preserved exactly
    assert [c.tag for c in node if c.is_list] == ["a", "b", "a"]


def test_make_list_synthesized_node_serializes_with_single_spaces():
    syn = SNode.make_list([SNode.atom("foo"), SNode.atom("1"), SNode.atom('"x"')])
    assert dumps(syn) == '(foo 1 "x")'
    assert dumps(SNode.make_list([])) == "()"


def test_empty_list_round_trip():
    assert dumps(parse("()")) == "()"


# --- malformed: synthetic (small, fast) -------------------------------------
def test_unterminated_string_raises_structured():
    with pytest.raises(AkcliError) as ei:
        parse('(a "boom)')
    assert ei.value.code == "KICAD_SEXPR_UNTERMINATED"


def test_unterminated_list_raises_structured():
    with pytest.raises(AkcliError) as ei:
        parse("(a (b c)")
    assert ei.value.code == "KICAD_SEXPR_UNTERMINATED"


def test_stray_close_paren_raises_structured():
    with pytest.raises(AkcliError) as ei:
        parse(")")
    assert ei.value.code == "KICAD_SEXPR_UNTERMINATED"


def test_empty_document_raises_structured():
    with pytest.raises(AkcliError) as ei:
        parse("   \n  ")
    assert ei.value.code == "KICAD_SEXPR_UNTERMINATED"


def test_over_deep_synthetic_raises_depth_not_recursionerror():
    # One past the cap; must be a catchable structured error (no RecursionError).
    deep = "(" * (MAX_SEXPR_DEPTH + 1)
    with pytest.raises(AkcliError) as ei:
        parse(deep)
    assert ei.value.code == "KICAD_SEXPR_DEPTH"


def test_over_big_atom_synthetic_raises_toobig():
    big = "(" + "a" * (MAX_ATOM_BYTES + 1) + ")"
    with pytest.raises(AkcliError) as ei:
        parse(big)
    assert ei.value.code == "KICAD_SEXPR_TOOBIG"


# --- malformed: committed fixtures, each within a time budget ---------------
@pytest.mark.parametrize(
    ("name", "expected_code"),
    [
        ("deeply_nested.kicad_sch", "KICAD_SEXPR_DEPTH"),
        ("huge_atom.kicad_sch", "KICAD_SEXPR_TOOBIG"),
        ("unterminated_quote.kicad_sch", "KICAD_SEXPR_UNTERMINATED"),
    ],
)
def test_malformed_corpus_structured_within_budget(name, expected_code):
    text = (MALFORMED / name).read_text(encoding="utf-8", errors="surrogateescape")
    with time_budget(10):
        with pytest.raises(AkcliError) as ei:
            parse(text)
    assert ei.value.code in ERROR_CODES
    assert ei.value.code == expected_code


def test_module_exports():
    assert set(sexpr.__all__) == {"SNode", "parse", "dumps"}
