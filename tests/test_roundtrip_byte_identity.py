"""THE GATE for the KiCad writer (SPEC §3.5).

``dumps(parse(bytes)) == bytes`` for real ``.kicad_sch`` files.  If this ever
regresses, the surgical writer can silently corrupt a user's schematic on save,
so these are the highest-priority writer tests.  Two real fixtures cover both the
KiCad 7 and KiCad 8 on-disk formats (different generator/version tokens and
title-block layouts), and the fixtures contain a non-ASCII em-dash so UTF-8
encode/decode is exercised end-to-end.

We also pin the *synthesized*-node formatting (KiCad house style) and the atom
construction / quoting helpers, since those paths are not exercised by the
byte-identical round-trip (which only ever replays stored whitespace).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akcli.sexpr import parser as sexpr
from akcli.sexpr.parser import SNode
from akcli.sexpr import writer as sexpr_writer

FIXTURES = Path(__file__).parent / "fixtures" / "kicad"


@pytest.mark.parametrize("name", ["board_v7.kicad_sch", "board_v8.kicad_sch"])
def test_roundtrip_byte_identical(name: str) -> None:
    """parse -> dumps reproduces the source file byte-for-byte."""
    raw = (FIXTURES / name).read_bytes()
    node = sexpr.parse(raw.decode("utf-8"))
    out = sexpr.dumps(node).encode("utf-8")
    assert out == raw, f"{name} not byte-identical (len {len(out)} vs {len(raw)})"


@pytest.mark.parametrize("name", ["board_v7.kicad_sch", "board_v8.kicad_sch"])
def test_roundtrip_matches_reader_dumps(name: str) -> None:
    """dumps() is stable under reparse (the ONE serializer, self-consistent)."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    node = sexpr.parse(text)
    assert sexpr.dumps(sexpr.parse(sexpr.dumps(node))) == sexpr.dumps(node)


def test_subtree_roundtrip_preserves_internal_whitespace() -> None:
    """Serializing a *sub*-node (not the root) still replays its stored ws."""
    text = (FIXTURES / "board_v7.kicad_sch").read_text(encoding="utf-8")
    root = sexpr.parse(text)
    # Pick a nested list node that carries authentic whitespace.
    child = next(c for c in root.children if c.is_list)
    # The sub-node has no document prefix/suffix, so dumps is its stored form.
    assert sexpr.dumps(sexpr.parse(sexpr.dumps(child))) == sexpr.dumps(child)


def test_atom_node_roundtrips() -> None:
    node = SNode.atom("20230121")
    assert sexpr.dumps(node) == "20230121"
    qnode = SNode.atom('"eeschema"')
    assert sexpr.dumps(qnode) == '"eeschema"'


def test_make_list_default_ws_is_honoured_inline() -> None:
    # make_list installs a valid single-space ws array, replayed verbatim.
    node = SNode.make_list(
        [SNode.atom("at"), SNode.atom("1"), SNode.atom("2"), SNode.atom("0")]
    )
    assert sexpr.dumps(node) == "(at 1 2 0)"


def test_synthesized_empty_list() -> None:
    assert sexpr.dumps(SNode.make_list([])) == "()"


def test_quote_escapes_backslash_and_quote() -> None:
    assert sexpr_writer.quote('a"b') == '"a\\"b"'
    assert sexpr_writer.quote("a\\b") == '"a\\\\b"'
    assert sexpr_writer.quote("plain") == '"plain"'


def test_atom_for_token_quotes_only_when_needed() -> None:
    assert sexpr_writer.atom_for_token("power_in").text == "power_in"
    assert sexpr_writer.atom_for_token("20230121").text == "20230121"
    assert sexpr_writer.atom_for_token("has space").text == '"has space"'
    assert sexpr_writer.atom_for_token("").text == '""'


def test_atom_for_value_is_always_quoted() -> None:
    assert sexpr_writer.atom_for_value("R1").text == '"R1"'


def test_atom_for_nm_renders_mm_string() -> None:
    # 1.27 mm = 1_270_000 nm -> "1.27"; 1 mm -> "1"; 0 -> "0".
    assert sexpr_writer.atom_for_nm(1_270_000).text == "1.27"
    assert sexpr_writer.atom_for_nm(1_000_000).text == "1"
    assert sexpr_writer.atom_for_nm(0).text == "0"


def test_serialized_synthesized_tree_reparses_equal() -> None:
    """A synthesized tree, once serialized, parses back to the same structure."""
    inner = SNode.make_list([SNode.atom("at"), SNode.atom("1"), SNode.atom("2")])
    outer = SNode.make_list([SNode.atom("symbol"), inner])
    reparsed = sexpr.parse(sexpr.dumps(outer))
    assert reparsed.tag == "symbol"
    assert reparsed.find("at") is not None
