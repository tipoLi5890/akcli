"""S-expression atom-construction helpers (SPEC §3.5).

Builders for the surgical writer: ``quote`` (the ONE string-atom quoter, an
alias of :func:`akcli.kicad_escape.quote_atom`) and the ``atom_for_*`` family
(token vs quoted-string vs nanometre-float atoms), so quoting/escaping and
the nm -> mm-string conversion live in exactly one place, matching how the
parser decodes them.

The tree's ONE serializer is :func:`akcli.sexpr.dumps` (the parser module):
it reproduces untouched nodes byte-for-byte from their stored token text.
This module once carried a second, house-style ``serialize`` used only as a
test cross-check; it was deleted when the round-trip gate switched to
asserting ``dumps(parse(text)) == text`` directly.
"""

from __future__ import annotations

from ..kicad_escape import quote_atom
from .parser import SNode
from ..units import nm_to_mm_str

__all__ = [
    "INDENT",
    "nm_to_mm_str",
    "quote",
    "atom_for_value",
    "atom_for_nm",
    "atom_for_token",
]

# KiCad's serializer indents synthesized nodes with two spaces per nesting level.
# (Real KiCad-written files in the wild use a tab; SPEC §3.5 pins *our* synthesized
# output to two spaces.  Preserved nodes keep whatever the source used — tabs and
# all — because we emit their stored whitespace verbatim, so the GATE is unaffected
# by this constant.)
INDENT = "  "

# Characters that force a bare atom to be emitted as a quoted string, mirroring
# KiCad's own quoting decision: anything with whitespace, parens, quotes, or that
# is empty becomes a quoted token.
_NEEDS_QUOTE = frozenset(' \t\r\n\f\v()"')


# --- KiCad-faithful atom construction helpers -------------------------------
#
# The surgical writer builds new atoms through these so quoting/escaping and the
# nanometre -> mm-string conversion live in exactly one place (this module),
# matching how the parser decodes them.

# The escape implementation lives in kicad_escape.quote_atom (rank 10, the
# module that owns every KiCad string-representation transform); this module
# keeps the historical public name.
quote = quote_atom


def atom_for_token(value: str) -> SNode:
    """Build an atom node for a symbol/keyword, quoting only when KiCad would.

    A value that is empty or contains whitespace, parentheses, or a quote is
    emitted as a quoted string; everything else stays a bare token (e.g.
    ``yes``, ``20230121``, ``power_in``).
    """
    if value == "" or any(c in _NEEDS_QUOTE for c in value):
        return SNode.atom(quote(value))
    return SNode.atom(value)


def atom_for_value(value: str) -> SNode:
    """Build an always-quoted string atom (KiCad string fields are quoted)."""
    return SNode.atom(quote(value))


def atom_for_nm(nm: int) -> SNode:
    """Build a bare numeric atom for an integer-nanometre coordinate.

    The nanometres are rendered to a KiCad-style mm float string
    (trailing-zero/dot stripped) via :func:`units.nm_to_mm_str`.
    """
    return SNode.atom(nm_to_mm_str(nm))
