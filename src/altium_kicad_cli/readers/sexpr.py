"""Iterative, depth/size-capped S-expression tokenizer + parser (SPEC §3.4).

Shared by the KiCad readers (``kicad.py``, ``kicad_lib.py``) and the future
KiCad writer.  Two hard requirements drive the whole design:

1. **No native recursion anywhere.**  A hostile ``.kicad_sch`` can nest parens
   ~200k deep; a recursive descent parser would raise an *uncatchable*
   ``RecursionError``/``SIGSEGV`` (and ``sys.setrecursionlimit`` is BANNED by the
   SPEC because it just trades the catchable error for a real stack overflow).
   Both :func:`parse` and :func:`dumps` walk an **explicit stack**.

2. **Byte-identical reserialization of untouched nodes.**  :class:`SNode`
   preserves the *exact original token text* of every atom (quotes/escapes
   intact) and every run of inter-token whitespace, so ``dumps(parse(text))``
   reproduces ``text`` byte-for-byte.  The writer only rewrites the nodes it
   actually edits; everything else round-trips unchanged.

All allocation/scan loops are bounded by constants in :mod:`..safety`
(``MAX_SEXPR_DEPTH`` / ``MAX_ATOM_BYTES`` / ``MAX_NODES``); malformed input
raises a structured :class:`~..errors.AkcliError` with one of
``KICAD_SEXPR_DEPTH`` / ``KICAD_SEXPR_UNTERMINATED`` / ``KICAD_SEXPR_TOOBIG``
rather than hanging, exhausting memory, or crashing the interpreter.
"""

from __future__ import annotations

import re

from ..errors import fail
from ..safety import MAX_ATOM_BYTES, MAX_NODES, MAX_SEXPR_DEPTH

__all__ = ["SNode", "parse", "dumps"]

# Whitespace that separates tokens.  KiCad S-expressions have no comment syntax,
# so trivia is whitespace only; every other character starts a token.
_WS = frozenset(" \t\r\n\f\v")

# A bare (unquoted) atom runs until whitespace, a paren, or a quote.  Matched at
# C speed so a multi-megabyte hostile atom is located in one pass (then rejected
# by the MAX_ATOM_BYTES guard) instead of a slow per-character Python loop.
# The character class spells out _WS explicitly: `\s` would additionally match
# UNICODE whitespace (NBSP, U+2028, ...) that the _WS scanner does not skip,
# and that mismatch turned such bytes into an unscannable crash (fuzz-found).
_BARE_RE = re.compile(r'[^ \t\r\n\f\v()"]+')

# A quoted atom: opening quote, then runs of (non-quote/non-backslash | escape),
# then the closing quote.  The two alternatives are mutually exclusive, so there
# is no catastrophic backtracking.  DOTALL lets ``\\<newline>`` escape a newline.
_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)

# Escape sequences understood by :pyattr:`SNode.value` (decode side only; the
# raw text is what gets reserialized).
_UNESCAPE = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


class SNode:
    """A node in a lossless S-expression tree.

    An :class:`SNode` is either an **atom** (a leaf token) or a **list**
    (``( ... )``).  Lists keep their children in order plus the whitespace runs
    between them, so they reserialize byte-for-byte.

    Atom nodes:
        * ``is_atom`` is ``True``
        * ``text`` is the *exact* source token, e.g. ``kicad_sch``, ``1.27``, or
          ``'"a \\"quoted\\" value"'`` (quotes + escapes preserved verbatim)

    List nodes:
        * ``is_atom`` is ``False``
        * ``children`` is the ordered list of child :class:`SNode`
        * ``ws`` holds ``len(children) + 1`` whitespace strings: ``ws[i]`` is the
          whitespace before ``children[i]`` and ``ws[-1]`` is the whitespace
          before the closing ``)``.

    ``prefix`` / ``suffix`` carry document-level trivia (text before the root
    ``(`` and after the root ``)``); they are empty on every non-root node.
    """

    __slots__ = ("is_atom", "text", "children", "ws", "prefix", "suffix")

    def __init__(
        self,
        is_atom: bool,
        text: str | None = None,
        children: list[SNode] | None = None,
        ws: list[str] | None = None,
    ) -> None:
        self.is_atom = is_atom
        self.text = text
        self.children = children
        self.ws = ws
        self.prefix = ""
        self.suffix = ""

    # --- constructors -------------------------------------------------------
    @classmethod
    def atom(cls, text: str) -> SNode:
        """Build an atom node from its exact source ``text``."""
        return cls(True, text=text)

    @classmethod
    def make_list(cls, children: list[SNode] | None = None) -> SNode:
        """Build a fresh list node with single-space default whitespace.

        Convenience for the writer when *synthesizing* new nodes (parsed nodes
        carry their original whitespace instead).
        """
        kids = list(children) if children else []
        # One space before each child, none before the closing paren -> "(a b c)".
        ws = [""] + [" "] * (len(kids) - 1) + [""] if kids else [""]
        return cls(False, children=kids, ws=ws)

    # --- predicates / accessors --------------------------------------------
    @property
    def is_list(self) -> bool:
        return not self.is_atom

    @property
    def value(self) -> str | None:
        """Decoded atom value (quotes stripped, escapes resolved); ``None`` for lists."""
        if not self.is_atom or self.text is None:
            return None
        t = self.text
        if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
            return _unescape(t[1:-1])
        return t

    @property
    def tag(self) -> str | None:
        """Head symbol of a list (``children[0]``'s value), else ``None``."""
        if self.is_atom or not self.children:
            return None
        head = self.children[0]
        return head.value if head.is_atom else None

    def __iter__(self):
        return iter(self.children or ())

    def __len__(self) -> int:
        return len(self.children or ())

    def __getitem__(self, idx: int) -> SNode:
        if self.children is None:
            raise TypeError("atom node is not subscriptable")
        return self.children[idx]

    def find(self, tag: str) -> SNode | None:
        """First child list whose head symbol equals ``tag``."""
        for child in self.children or ():
            if child.is_list and child.tag == tag:
                return child
        return None

    def find_all(self, tag: str) -> list[SNode]:
        """All child lists whose head symbol equals ``tag`` (order preserved)."""
        return [c for c in (self.children or ()) if c.is_list and c.tag == tag]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.is_atom:
            return f"SNode.atom({self.text!r})"
        return f"SNode.list(tag={self.tag!r}, n={len(self)})"


def _unescape(s: str) -> str:
    """Resolve KiCad backslash escapes inside a quoted atom's body."""
    if "\\" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            out.append(_UNESCAPE.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse(text: str) -> SNode:
    """Parse ``text`` into a lossless :class:`SNode` tree (iterative, bounded).

    Returns the single top-level node.  Leading/trailing document trivia is
    stored on ``root.prefix`` / ``root.suffix`` so ``dumps(parse(text))`` is
    byte-identical.

    Raises :class:`~..errors.AkcliError`:
        * ``KICAD_SEXPR_DEPTH`` — nesting exceeds ``MAX_SEXPR_DEPTH``
        * ``KICAD_SEXPR_TOOBIG`` — an atom exceeds ``MAX_ATOM_BYTES`` or the node
          count exceeds ``MAX_NODES``
        * ``KICAD_SEXPR_UNTERMINATED`` — EOF inside a string or list, an empty
          document, or a stray closing ``)``
    """
    n = len(text)
    i = 0
    stack: list[SNode] = []          # open list nodes, innermost last
    root: SNode | None = None
    doc_prefix = ""
    node_count = 0

    while i < n:
        # Consume the leading whitespace run; it becomes trivia attributed below.
        ws_start = i
        while i < n and text[i] in _WS:
            i += 1
        ws = text[ws_start:i]
        if i >= n:
            # Trailing whitespace at EOF; no open lists may remain (checked below).
            break

        c = text[i]

        if c == "(":
            i += 1
            node = SNode(False, children=[], ws=[])
            node_count += 1
            if node_count > MAX_NODES:
                fail("KICAD_SEXPR_TOOBIG", f"node count exceeds {MAX_NODES}")
            if stack:
                parent = stack[-1]
                parent.ws.append(ws)        # whitespace before this child
                parent.children.append(node)
            elif root is None:
                root = node
                doc_prefix = ws
            else:
                # A second top-level form: keep everything from here as suffix so
                # the document still round-trips, and stop.
                root.suffix = text[ws_start:]
                return root
            stack.append(node)
            if len(stack) > MAX_SEXPR_DEPTH:
                fail("KICAD_SEXPR_DEPTH", f"nesting exceeds {MAX_SEXPR_DEPTH}")

        elif c == ")":
            i += 1
            if not stack:
                fail("KICAD_SEXPR_UNTERMINATED", f"unexpected ')' at offset {i - 1}")
            node = stack.pop()
            node.ws.append(ws)              # whitespace before the closing paren
            if not stack:
                # Root list closed; the remainder of the document is trailing trivia.
                root = node if root is None else root
                root.prefix = doc_prefix
                root.suffix = text[i:]
                return root

        else:
            # An atom token (quoted string or bare symbol/number).
            atom_text, i = _scan_atom(text, i, n)
            node_count += 1
            if node_count > MAX_NODES:
                fail("KICAD_SEXPR_TOOBIG", f"node count exceeds {MAX_NODES}")
            anode = SNode(True, text=atom_text)
            if stack:
                parent = stack[-1]
                parent.ws.append(ws)
                parent.children.append(anode)
            elif root is None:
                # A bare top-level atom (no enclosing list); rest is trailing trivia.
                anode.prefix = ws
                anode.suffix = text[i:]
                return anode
            else:
                root.suffix = text[ws_start:]
                return root

    if stack:
        fail("KICAD_SEXPR_UNTERMINATED", "end of input inside an open '('")
    if root is None:
        fail("KICAD_SEXPR_UNTERMINATED", "empty document")
    # Reached only if the root closed exactly at EOF with no trailing char; the
    # in-loop ')' branch returns in the common case, so this is the no-suffix path.
    root.prefix = doc_prefix
    return root


def _scan_atom(text: str, i: int, n: int) -> tuple[str, int]:
    """Return ``(token_text, new_index)`` for the atom starting at ``i``.

    Quoted atoms are scanned with a non-backtracking regex and must terminate;
    bare atoms run to the next delimiter.  Both enforce ``MAX_ATOM_BYTES``.
    """
    if text[i] == '"':
        m = _QUOTED_RE.match(text, i)
        if m is None:
            fail("KICAD_SEXPR_UNTERMINATED", f"unterminated string at offset {i}")
        tok = m.group()
        if len(tok) > MAX_ATOM_BYTES:
            fail("KICAD_SEXPR_TOOBIG", f"quoted atom exceeds {MAX_ATOM_BYTES} bytes")
        return tok, m.end()

    m = _BARE_RE.match(text, i)
    if m is None:
        # unreachable while _WS and _BARE_RE agree; never crash on drift
        fail("KICAD_SEXPR_UNTERMINATED",
             f"unscannable character {text[i]!r} at offset {i}")
    tok = m.group()
    if len(tok) > MAX_ATOM_BYTES:
        fail("KICAD_SEXPR_TOOBIG", f"bare atom exceeds {MAX_ATOM_BYTES} bytes")
    return tok, m.end()


def dumps(node: SNode) -> str:
    """Serialize ``node`` back to S-expression text (iterative, no recursion).

    For a node produced by :func:`parse` and left untouched, ``dumps`` reproduces
    the original source byte-for-byte.  Synthesized/edited subtrees serialize
    using their stored whitespace (single spaces for :meth:`SNode.make_list`).
    """
    out: list[str] = [node.prefix]
    if node.is_atom:
        out.append(node.text or "")
    else:
        # Explicit work stack: each frame is [list_node, next_child_index, opened?].
        stack: list[list] = [[node, 0, False]]
        while stack:
            frame = stack[-1]
            nd, idx, opened = frame
            if not opened:
                out.append("(")
                frame[2] = True
            kids = nd.children or ()
            if idx < len(kids):
                out.append(nd.ws[idx])
                child = kids[idx]
                frame[1] = idx + 1
                if child.is_atom:
                    out.append(child.text or "")
                else:
                    stack.append([child, 0, False])
            else:
                out.append(nd.ws[len(kids)])   # whitespace before ')'
                out.append(")")
                stack.pop()
    out.append(node.suffix)
    return "".join(out)
