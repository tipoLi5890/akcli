"""KiCad-faithful S-expression serializer (SPEC §3.5).

This is the writer-side counterpart to :func:`readers.sexpr.parse`.  It turns an
:class:`~..readers.sexpr.SNode` tree back into ``.kicad_sch`` / ``.kicad_sym``
text with two non-negotiable guarantees:

1. **THE GATE — byte-identical no-op round-trip.**  For any node produced by
   :func:`readers.sexpr.parse` and left untouched, ``serialize(node)`` reproduces
   the original source *byte-for-byte*.  The mechanism is the lossless
   :class:`SNode` contract: every list node stores the exact inter-token
   whitespace (``node.ws``) and the root carries leading/trailing document trivia
   (``node.prefix`` / ``node.suffix``).  When a node still holds a valid stored
   whitespace array we emit it verbatim — quotes, escapes, tabs and all — so the
   surgical KiCad writer only ever rewrites the handful of subtrees it actually
   edits while everything else round-trips unchanged.  This is verified against
   real KiCad 7 and KiCad 8 fixtures in ``test_roundtrip_byte_identity.py``.

2. **KiCad-faithful formatting of *synthesized* subtrees.**  Nodes the writer
   builds from scratch (via :meth:`SNode.make_list` or with an out-of-sync ``ws``
   array after an edit) carry no authentic whitespace.  For those we generate
   KiCad's house style: a list of only atoms stays on one line — ``(at 1 2 0)`` —
   while a list containing child *lists* breaks each child onto its own line with
   ``INDENT``-per-level indentation and a trailing ``)`` aligned under the head.

Like the parser, :func:`serialize` walks an **explicit stack** — never native
recursion — so a deeply nested tree can never raise an uncatchable
``RecursionError`` / stack overflow (``sys.setrecursionlimit`` stays BANNED).

All numeric geometry handled by the KiCad writer is integer **nanometres**; the
mm-string conversion happens only here, at serialize time, via
:func:`units.nm_to_mm_str` (re-exported and used by the atom-construction
helpers).  Callers must never pre-format floats themselves.
"""

from __future__ import annotations

from ..readers.sexpr import SNode
from ..units import nm_to_mm_str

__all__ = [
    "serialize",
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


def serialize(node: SNode) -> str:
    """Serialize ``node`` to KiCad S-expression text (iterative, no recursion).

    Untouched parsed nodes reproduce their source byte-for-byte (THE GATE);
    synthesized nodes (or nodes whose ``ws`` no longer matches their child count
    after an edit) are formatted in KiCad's house style with :data:`INDENT`.

    Parameters
    ----------
    node:
        Any :class:`SNode` — atom or list, root or sub-tree.  ``prefix`` /
        ``suffix`` are honoured so a parsed *root* round-trips exactly.

    Returns
    -------
    str
        The serialized text.  Encode with UTF-8 to compare against source bytes.
    """
    out: list[str] = [node.prefix or ""]

    if node.is_atom:
        out.append(node.text or "")
        out.append(node.suffix or "")
        return "".join(out)

    # Explicit work stack: each frame is
    #   [list_node, next_child_index, opened?, depth, ws_array_or_None].
    # ``ws`` is resolved (stored-or-synthesized) lazily, the first time the frame
    # is opened, so we compute it exactly once per list node.
    stack: list[list] = [[node, 0, False, 0, None]]
    while stack:
        frame = stack[-1]
        nd, idx, opened, depth, ws = frame
        if not opened:
            out.append("(")
            ws = _whitespace_for(nd, depth)
            frame[2] = True
            frame[4] = ws

        kids = nd.children or ()
        if idx < len(kids):
            out.append(ws[idx])                 # whitespace before this child
            child = kids[idx]
            frame[1] = idx + 1
            if child.is_atom:
                out.append(child.text or "")
            else:
                stack.append([child, 0, False, depth + 1, None])
        else:
            out.append(ws[len(kids)])           # whitespace before the closing ')'
            out.append(")")
            stack.pop()

    out.append(node.suffix or "")
    return "".join(out)


def _whitespace_for(nd: SNode, depth: int) -> list[str]:
    """Return the ``len(children)+1`` whitespace strings to emit around ``nd``.

    If the node still carries an *authentic* stored whitespace array (length
    exactly ``len(children) + 1`` — the invariant the parser establishes), it is
    returned verbatim, which is what makes an untouched tree byte-identical.

    Otherwise the node was synthesized or edited (``ws`` is ``None`` or stale),
    and KiCad-faithful whitespace is generated: atom-only lists on one line,
    list-bearing lists broken across indented lines.
    """
    kids = nd.children or ()
    stored = nd.ws
    if stored is not None and len(stored) == len(kids) + 1:
        return stored

    n = len(kids)
    if n == 0:
        return [""]                              # "()"

    if not any(c.is_list for c in kids):
        # All atoms -> single line: "(tag a b c)".
        return [""] + [" "] * (n - 1) + [""]

    # Contains child lists -> multi-line.  Leading atoms (e.g. the head tag and
    # inline scalars) stay on the opening line separated by a single space; each
    # child *list* starts a fresh indented line.  As in KiCad's own output, the
    # closing ')' follows the last child with NO whitespace, so nested closers
    # stack on one line ("...(unit 1)))))").
    child_indent = "\n" + INDENT * (depth + 1)
    ws: list[str] = []
    for i, child in enumerate(kids):
        if i == 0:
            ws.append("" if child.is_atom else child_indent)
        else:
            ws.append(child_indent if child.is_list else " ")
    ws.append("")                                # before the closing ')'
    return ws


# --- KiCad-faithful atom construction helpers -------------------------------
#
# The surgical writer builds new atoms through these so quoting/escaping and the
# nanometre -> mm-string conversion live in exactly one place (this module),
# matching how the parser decodes them.

def quote(value: str) -> str:
    """Return the KiCad source *text* for a quoted-string atom holding ``value``.

    Backslashes and double-quotes are escaped (``\\`` -> ``\\\\``, ``"`` ->
    ``\\"``), matching the inverse of the parser's unescape table.  The result
    includes the surrounding quotes and is suitable as :pyattr:`SNode.text`.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
