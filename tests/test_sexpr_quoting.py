"""One quoting rule for every s-expression akcli writes.

The tree grew SIX hand-rolled "escape backslash and double-quote" helpers, and
each one that forgot the control characters is a file KiCad refuses to open
while every akcli gate reports success — ``writers.kicad._q``'s docstring
records the first occurrence, ``commands.drawing`` (``akcli new --title``) was
the fourth. So: pin them all against one corpus, and fail the build when a
seventh appears un-pinned.

Why control characters must escape: akcli's own lexer is deliberately tolerant
(``readers.sexpr._QUOTED_RE`` is DOTALL, so a raw newline inside a quoted atom
round-trips through akcli fine) — eeschema's is not.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from akcli import exporters
from akcli.drivers import kicad_cli
from akcli.readers import sexpr
from akcli.writers import connectivity as wconn
from akcli.writers import instances as winst
from akcli.writers import kicad as wkicad
from akcli.writers import lib_cache as wlib
from akcli.writers import sexpr_writer

from ._cli_harness import run_cli

SRC = Path(__file__).resolve().parents[1] / "src" / "akcli"

# Every quoter that emits a KiCad s-expression string atom. They must agree
# BYTE-FOR-BYTE: a value crosses several of them on one write path (op-list ->
# writer -> lib cache -> netlist export) and a disagreement is a silent
# corruption at the seam.
QUOTERS = {
    "writers.sexpr_writer.quote": sexpr_writer.quote,
    "writers.kicad._q": wkicad._q,
    "writers.connectivity._q": wconn._q,
    "writers.instances._q": winst._q,
    "writers.lib_cache._quote": wlib._quote,
    "exporters._kicad_quote": exporters._kicad_quote,
}

CORPUS = [
    "plain",
    "",
    'has "quotes"',
    "back\\slash",
    "line\nbreak",
    "carriage\rreturn",
    "tab\tseparated",
    'all\\of\t"them"\r\n',
    "trailing backslash\\",
    "\\n literal-looking",
    "unicode ohm Ω / 中文",
]


@pytest.mark.parametrize("value", CORPUS)
def test_every_quoter_agrees(value):
    rendered = {name: q(value) for name, q in QUOTERS.items()}
    assert len(set(rendered.values())) == 1, rendered


@pytest.mark.parametrize("value", CORPUS)
def test_quoted_atoms_carry_no_raw_control_characters(value):
    text = sexpr_writer.quote(value)
    assert text[0] == '"' and text[-1] == '"'
    assert not any(c in text for c in "\n\r\t"), repr(text)


@pytest.mark.parametrize("value", CORPUS)
def test_quoting_round_trips_through_the_parser(value):
    """The escape is the exact inverse of the reader's unescape table."""
    node = sexpr.parse(f"(title {sexpr_writer.quote(value)})")
    assert node.children[1].value == value


# --------------------------------------------------------------------------- #
# drift gate: no seventh hand-rolled escaper
# --------------------------------------------------------------------------- #
# Functions/expressions that escape a double-quote for an s-expression. Anything
# NOT listed here that does `.replace('"', '\\"')` is a new un-pinned quoter.
_KNOWN_ESCAPE_SITES = {
    ("writers/sexpr_writer.py", "quote"),
    ("writers/kicad.py", "_q"),
    ("writers/connectivity.py", "_q"),
    ("writers/instances.py", "_q"),
    ("writers/lib_cache.py", "_quote"),
    ("exporters.py", "_kicad_quote"),
}


def _is_quote_escape(node: ast.AST) -> bool:
    """``<x>.replace('"', '\\"')`` — the tell-tale of a hand-rolled quoter."""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and len(node.args) == 2
            and all(isinstance(a, ast.Constant) and isinstance(a.value, str)
                    for a in node.args)
            and node.args[0].value == '"'          # type: ignore[attr-defined]
            and node.args[1].value == '\\"')       # type: ignore[attr-defined]


def _escape_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue                      # vendored code keeps its own rules
        text = path.read_text(encoding="utf-8")
        if '\\\\"' not in text:
            continue
        rel = path.relative_to(SRC).as_posix()
        # explicit stack (never native recursion), carrying the innermost
        # enclosing function name down to each node
        stack: list[tuple[ast.AST, str]] = [(ast.parse(text), "<module>")]
        while stack:
            node, owner = stack.pop()
            for child in ast.iter_child_nodes(node):
                if _is_quote_escape(child):
                    found.add((rel, owner))
                inner = (child.name
                         if isinstance(child, (ast.FunctionDef,
                                               ast.AsyncFunctionDef))
                         else owner)
                stack.append((child, inner))
    return found


def test_no_unpinned_sexpr_escaper():
    """A new escaper must join QUOTERS above (or explain itself here)."""
    extra = _escape_sites() - _KNOWN_ESCAPE_SITES
    assert not extra, (
        f"un-pinned s-expression escaper(s): {sorted(extra)} — route them "
        "through writers.sexpr_writer.quote or add them to QUOTERS")


def test_drawing_has_no_local_escaper():
    """`akcli new` delegates to the writer's quoter (regression: it did not)."""
    text = (SRC / "commands" / "drawing.py").read_text(encoding="utf-8")
    assert "_sexpr_escape" not in text
    assert "from ..writers.kicad import _q" in text


# --------------------------------------------------------------------------- #
# `akcli new --title` end to end
# --------------------------------------------------------------------------- #
_NASTY_TITLE = 'A\nB\tC\r"D"\\E'


def _new_with_nasty_title(tmp_path: Path) -> Path:
    target = tmp_path / "board.kicad_sch"
    rc, _, err = run_cli(["new", str(target), "--title", _NASTY_TITLE])
    assert rc == 0, err
    return target


def test_new_escapes_control_characters_in_the_title(tmp_path):
    target = _new_with_nasty_title(tmp_path)
    text = target.read_text(encoding="utf-8")
    title_line = [ln for ln in text.splitlines() if ln.lstrip().startswith("(title ")]
    assert len(title_line) == 1, text          # NOT split across lines
    assert f"(title {wkicad._q(_NASTY_TITLE)})" in text
    # and akcli reads its own file back losslessly
    doc = sexpr.parse(text)
    tb = doc.find("title_block")
    assert tb is not None
    assert tb.children[1].children[1].value == _NASTY_TITLE


def test_kicad_cli_opens_a_file_with_a_multiline_title(tmp_path):
    """Ground truth: a real KiCad must accept what `new --title` wrote."""
    if not kicad_cli.available():
        pytest.skip("kicad-cli not installed (runs in the CI KiCad job)")
    target = _new_with_nasty_title(tmp_path)
    exe = kicad_cli.find()
    assert exe is not None
    # `sch export netlist` parses the whole document and fails loudly on a
    # malformed one (ERC on an empty sheet can exit non-zero for other reasons).
    out = tmp_path / "net.net"
    proc = subprocess.run(
        [exe, "sch", "export", "netlist", "--output", str(out), str(target)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()
