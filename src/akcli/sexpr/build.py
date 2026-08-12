"""SNode construction helpers — the writers' node factory (rank 30).

Every ``.kicad_sch`` subtree akcli synthesizes is assembled from these
primitives: quoted atoms (:func:`_q`), millimetre-rendered numbers
(:func:`mm`), the shared ``(at)`` / ``(effects)`` / ``(stroke)`` / ``(fill)``
/ ``(uuid)`` shapes, and the idempotent top-level append
(:func:`append_top_idempotent`) that makes a replayed op-list converge
instead of duplicating nodes.

Lifted VERBATIM out of ``writers/kicad.py`` (0.21 batch R3b); the public
names lost their leading underscore, and the underscore aliases at the
bottom of the module keep the relocated writer bodies byte-identical.
"""

from __future__ import annotations

from .. import units
from ..kicad_escape import quote_atom
from .parser import SNode


def _q(value: object) -> str:
    """Coerce-and-quote: ``str(value)`` through the ONE quoter.

    The escape itself lives in :func:`akcli.kicad_escape.quote_atom`; this
    wrapper only adds the writer-side ``str()`` coercion (ints, floats and
    Paths flow through op handlers).
    """
    return quote_atom(str(value))


def atom(text: str) -> SNode:
    return SNode.atom(text)


def list_(*children: SNode) -> SNode:
    return SNode.make_list(list(children))


def mm(nm: int) -> SNode:
    """A bare numeric atom rendering integer nm as a KiCad mm string."""
    return _atom(units.nm_to_mm_str(int(nm)))


def xy(pt: tuple[int, int]) -> SNode:
    return _list(_atom("xy"), _mm(pt[0]), _mm(pt[1]))


def pts(points: list[tuple[int, int]]) -> SNode:
    return _list(_atom("pts"), *[_xy(p) for p in points])


def stroke(width_nm: int = 0) -> SNode:
    # width 0 keeps the historical bare "0" atom so existing files replay
    # byte-identically; a non-zero width renders as a KiCad mm string.
    width = _atom("0") if not width_nm else _mm(width_nm)
    return _list(
        _atom("stroke"),
        _list(_atom("width"), width),
        _list(_atom("type"), _atom("default")),
    )


def fill(type_: str = "none") -> SNode:
    return _list(_atom("fill"), _list(_atom("type"), _atom(type_)))


def effects(hide: bool = False, justify: str | None = None) -> SNode:
    kids = [_atom("effects"),
            _list(_atom("font"), _list(_atom("size"), _atom("1.27"), _atom("1.27")))]
    if justify:
        kids.append(_list(_atom("justify"), *[_atom(t) for t in justify.split()]))
    if hide:
        kids.append(_list(_atom("hide"), _atom("yes")))
    return _list(*kids)


def label_justify(tag: str, orientation: int) -> str:
    """The ``(justify ...)`` eeschema pairs with a label's angle.

    KiCad renders label text horizontal-or-vertical only (never upside-down):
    the side it extends to comes from the JUSTIFICATION, not the angle — a
    180° label without ``(justify right)`` still runs +X, straight over the
    symbol it names. eeschema's four spin styles are (0,left) (90,left)
    (180,right) (270,right); local labels additionally sit on the wire, so
    they carry ``bottom`` to lift the text above it.
    """
    j = "left" if orientation in (0, 90) else "right"
    return f"{j} bottom" if tag == "label" else j


def uuid_node(value: str) -> SNode:
    return _list(_atom("uuid"), _atom(_q(value)))


def at(nm_pt: tuple[int, int], angle: float = 0.0) -> SNode:
    a = float(angle)
    angle_atom = _atom(str(int(a)) if a == int(a) else repr(a))
    return _list(_atom("at"), _mm(nm_pt[0]), _mm(nm_pt[1]), angle_atom)


def append_top(doc: SNode, child: SNode) -> None:
    """Append ``child`` as a top-level node (preserving ws/children invariant)."""
    indent = _doc_child_indent(doc)
    k = len(doc.children or [])
    doc.children.append(child)            # type: ignore[union-attr]
    doc.ws.insert(k, indent)              # type: ignore[union-attr]


def append_top_idempotent(doc: SNode, child: SNode, uid: str) -> None:
    """Idempotent top-level append: replace any prior node with ``uid`` IN PLACE.

    Re-running an op-list yields the same deterministic ``uid`` per created node,
    so replacing makes ``draw --apply`` converge (SPEC risk #7) instead of
    accumulating duplicate wires/labels/junctions on every run. Replacement is
    in place (same child index, same leading whitespace) rather than
    remove-then-append: appending would migrate every replayed node to the end
    of the document while non-op nodes (auto-junctions) stayed put, so the first
    re-apply reordered the file and byte-idempotency only converged on the
    SECOND apply.
    """
    for i, c in enumerate(doc.children or []):
        if not c.is_list:
            continue
        u = c.find("uuid")
        if (u is not None and len(u.children or []) >= 2
                and u.children[1].value == uid):   # type: ignore[index]
            doc.children[i] = child            # type: ignore[index]
            return
    _append_top(doc, child)


def doc_child_indent(doc: SNode) -> str:
    for w in (doc.ws or [])[1:]:
        if "\n" in w:
            return w
    return "\n\t"


def at_nm(at: SNode) -> tuple[int, int]:
    def _f(idx: int) -> float:
        if at.children and idx < len(at.children):
            v = at.children[idx].value
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    return (units.mm_to_nm(_f(1)), units.mm_to_nm(_f(2)))


def mil_pair(p: tuple[int, int]) -> str:
    """A point rendered as ``(x,y)`` mil — for collision/refusal messages."""
    return f"({units.nm_to_mil(p[0]):g},{units.nm_to_mil(p[1]):g})"


def unescape_atom(s: str) -> str:
    """Undo ``_q``'s quoting/escaping on a freshly-written atom token.

    A PARSED atom's ``.value`` is already unquoted; a node this same apply
    wrote carries the quoted token — normalize both for comparison.
    """
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    body = s[1:-1]
    out: list[str] = []
    i = 0
    esc = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(esc.get(body[i + 1], "\\" + body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# Underscore aliases: the writers moved here from ``writers/kicad.py`` call
# these helpers by their historical private names, and every body above was
# relocated VERBATIM (a rename inside a body is a behaviour risk this batch
# refuses to take). They are the same function objects, resolved at call
# time through the module globals.
# --------------------------------------------------------------------------- #
_atom = atom
_list = list_
_mm = mm
_xy = xy
_pts = pts
_stroke = stroke
_fill = fill
_effects = effects
_label_justify = label_justify
_uuid_node = uuid_node
_at = at
_append_top = append_top
_append_top_idempotent = append_top_idempotent
_doc_child_indent = doc_child_indent
_at_nm = at_nm
_mil_pair = mil_pair
_unescape_atom = unescape_atom
