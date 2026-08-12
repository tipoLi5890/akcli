"""Shared read-only SNode accessors (rank 30).

Home of the lookups that used to exist as near-identical copies in
``writers/kicad.py``, ``writers/instances.py``, ``readers/kicad.py`` and
``checks/layout.py`` — the duplication that forced ``groupframe`` and
``checks/layout`` to lazily import the 2000-line writer just to read a root
uuid.  Everything here is pure SNode + units; no reader/writer imports.
"""

from __future__ import annotations

from .. import units
from ..errors import AkcliError, fail
from .parser import SNode

__all__ = [
    "atom_value", "fnum", "child_mil", "root_uuid", "require_root_uuid",
]


def atom_value(node: SNode | None, idx: int) -> str | None:
    """Atom value of child ``idx`` (or ``None``)."""
    if node is not None and node.children and 0 <= idx < len(node.children):
        c = node.children[idx]
        if c.is_atom:
            return c.value
    return None


def fnum(node: SNode | None, idx: int, default: float = 0.0) -> float:
    v = atom_value(node, idx)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        # Corrupt numeric field (bad merge, disk damage, hand edit): surface a
        # structured parse error instead of a raw ValueError traceback, so the
        # CLI's JSON envelope contract holds for malformed inputs too.
        raise AkcliError(
            "KICAD_SEXPR_BAD_NUMBER",
            f"malformed number {v!r} in ({node.tag} ...)"
            if node is not None else f"malformed number {v!r}") from None


def child_mil(node: SNode | None, idx: int) -> float:
    """Numeric child ``idx`` (mm in the file) converted to mil."""
    return units.nm_to_mil(units.mm_to_nm(fnum(node, idx)))


def root_uuid(doc: SNode) -> str | None:
    """The schematic's root-sheet UUID (top-level ``(uuid ...)``), or None."""
    node = doc.find("uuid")
    if node is not None and node.children and len(node.children) >= 2:
        return node.children[1].value
    return None


def require_root_uuid(doc: SNode) -> str:
    """``root_uuid`` that refuses a document without one (writer contract)."""
    val = root_uuid(doc)
    if not val:
        fail("VERIFY_FAILED", "schematic root sheet has no (uuid ...)")
    return val
