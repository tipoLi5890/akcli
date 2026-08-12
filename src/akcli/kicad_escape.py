"""KiCad's ``{token}`` name escaping (``EscapeString``/``UnescapeString``).

KiCad escapes a fixed set of characters inside symbol names, ``lib_id``s and
footprint ids into ``{token}`` sequences — a layer **on top of** S-expression
quoting (``\\"`` etc). So ``19-237/R6GHBHC`` is stored in a ``.kicad_sym`` as
``19-237{slash}R6GHBHC``. KiCad unescapes both sides of any name comparison
before matching, which is why a library symbol stored escaped and a schematic
``lib_id`` written with a raw ``/`` still refer to the same symbol (and KiCad's
own ERC ``lib_symbol_issues`` reports nothing).

akcli therefore normalizes names to the **unescaped** form at the reader layer,
so the normalized model holds exactly one representation and every comparison
(``library audit``, ``relink-symbols``, ``resolve``) agrees with KiCad. The
writer uses :func:`escape_string` to emit the escaped form KiCad expects, so a
round-trip through KiCad's save does not rewrite the file.

Token set mirrors ``common/string.cpp``. Unescaping is context-free (any known
``{token}`` maps back to its character); an unrecognized ``{...}`` is left as
literal text.
"""

from __future__ import annotations

__all__ = ["escape_string", "unescape_string", "escape_lib_id",
           "unescape_lib_id", "quote_atom"]

# {token} -> character (common/string.cpp UnescapeString)
_UNESCAPE: dict[str, str] = {
    "slash": "/",
    "backslash": "\\",
    "brace": "{",
    "lt": "<",
    "gt": ">",
    "colon": ":",
    "dblquote": '"',
    "tab": "\t",
    "return": "\r",
    "newline": "\n",
}
# character -> {token} (the inverse; ``{`` handled first when escaping so the
# introducer itself is never double-mapped — see escape_string).
_ESCAPE: dict[str, str] = {ch: "{" + tok + "}" for tok, ch in _UNESCAPE.items()}


def unescape_string(s: str | None) -> str | None:
    """Reverse KiCad's ``{token}`` escaping. ``None`` and token-free input pass through."""
    if not s or "{" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "{":
            j = s.find("}", i + 1)
            if j != -1:
                token = s[i + 1:j]
                repl = _UNESCAPE.get(token)
                if repl is not None:
                    out.append(repl)
                    i = j + 1
                    continue
            out.append(c)          # unknown/unterminated: keep the literal '{'
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_string(s: str | None) -> str | None:
    """Apply KiCad's ``{token}`` escaping to a bare name (no ``:`` separator handling).

    Iterating character-by-character is order-independent because ``{`` maps to
    ``{brace}`` in the same pass, so the introducer is never re-escaped.
    """
    if not s:
        return s
    if not any(ch in _ESCAPE for ch in s):
        return s
    return "".join(_ESCAPE.get(ch, ch) for ch in s)


def unescape_lib_id(lib_id: str | None) -> str | None:
    """Unescape a ``nick:name`` lib_id (the ``:`` separator is preserved)."""
    return unescape_string(lib_id)


def escape_lib_id(lib_id: str | None) -> str | None:
    """Escape the ``nick`` and ``name`` of a ``nick:name`` lib_id separately.

    The ``:`` that separates nickname from name stays literal; a ``:`` *inside*
    either part becomes ``{colon}`` (KiCad's behaviour), so splitting on the
    FIRST ``:`` is correct.
    """
    if not lib_id:
        return lib_id
    if ":" in lib_id:
        nick, name = lib_id.split(":", 1)
        return f"{escape_string(nick)}:{escape_string(name)}"
    return escape_string(lib_id)


def quote_atom(text: str) -> str:
    """The ONE s-expression string-atom quoter (escape ``\\``, ``"``, controls).

    Returns the KiCad source text INCLUDING the surrounding quotes.  Control
    characters are not optional: akcli's own lexer tolerates a raw newline
    inside a quoted atom, but eeschema does not — an unescaped one yields a
    file KiCad refuses to open while every akcli gate passes.  The tree once
    grew six hand-rolled copies of this; ``tests/test_sexpr_quoting.py`` pins
    every public alias to THIS function object and fails on a seventh copy.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return ('"' + escaped.replace("\n", "\\n").replace("\r", "\\r")
                        .replace("\t", "\\t") + '"')
