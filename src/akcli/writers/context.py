"""Per-apply execution context + document/instance lookups.

``ExecCtx`` is the one mutable object an ``apply`` (or one sheet of a
multi-file transaction) carries through every op: the memoized
``lib_id -> SymbolDef`` cache, the uuid/annotation/power-port claim
registries that refuse two ops seeding one node, the text-anchor registry
behind Reference/Value autoplacement, and the pin-tip memo the router
consults.

The document lookups below (placed symbols, references, units, the inline
library view) sit here rather than in a module of their own: they are the
vocabulary every layer above — anchors, properties, placement, handlers —
reads the live document with, and ``_instance_component`` (the bridge from
a placed ``(symbol ...)`` node to a ``model.Component``) already needs
them.

Relocated VERBATIM from ``writers/kicad.py`` (0.21 batch R3b).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import model, units
from ..errors import AkcliError, fail
from ..kicad_escape import unescape_string
from ..readers import kicad_lib
from ..sexpr import SNode
from ..sexpr import query as _query
from . import instances, lib_cache


# The ONE root-uuid reader lives in sexpr.query; re-exported here because the
# writer's callers (groupframe, checks.layout, txn) historically import it
# from this module.
root_uuid = _query.root_uuid


def _placed_symbols(doc: SNode) -> list[SNode]:
    return [s for s in doc.find_all("symbol") if s.find("lib_id") is not None]


def _inst_lib_id(sym: SNode) -> str:
    """A placed instance's ``(lib_id ...)`` value, **unescaped** (``{slash}`` → ``/``).

    Normalizes to the same representation the lib_symbols cache is keyed by, so
    ``find_cached``/``resolve`` agree whether the doc was written by KiCad (escaped)
    or by an older akcli (raw)."""
    node = sym.find("lib_id")
    if node is None or len(node.children or ()) < 2:
        return ""
    return unescape_string(node.children[1].value) or ""


def _symbol_reference(sym: SNode) -> str | None:
    inst = sym.find("instances")
    if inst is not None:
        for proj in inst.find_all("project"):
            for path in proj.find_all("path"):
                ref = path.find("reference")
                if ref is not None and len(ref.children or []) >= 2:
                    v = ref.children[1].value
                    if v:
                        return v
    for prop in sym.find_all("property"):
        kids = prop.children or []
        if len(kids) >= 3 and kids[1].value == "Reference":
            return kids[2].value
    return None


def _symbol_answers_to(sym: SNode, ref: str) -> bool:
    """Does ``sym`` carry ``ref`` on ANY of its instance paths (or property)?

    A sheet file instantiated twice holds one reference PER path — an agent
    addressing the part by its second instance's refdes (R201) must find the
    same symbol node the first instance's refdes (R101) does.
    """
    refs = instances.symbol_references(sym)
    if refs:
        return ref in refs.values()
    return _symbol_reference(sym) == ref


def _symbol_by_ref(doc: SNode, ref: str) -> SNode | None:
    for sym in _placed_symbols(doc):
        if _symbol_answers_to(sym, ref):
            return sym
    return None


def _symbols_by_ref(doc: SNode, ref: str) -> list[SNode]:
    """ALL placed instances of ``ref`` (a multi-unit part is several)."""
    return [s for s in _placed_symbols(doc) if _symbol_answers_to(s, ref)]


def _symbol_by_uuid(doc: SNode, uid: str) -> SNode | None:
    for sym in _placed_symbols(doc):
        u = sym.find("uuid")
        if u is not None and len(u.children or []) >= 2 and u.children[1].value == uid:
            return sym
    return None


def _symbol_unit(sym: SNode) -> int:
    node = sym.find("unit")
    if node is not None and len(node.children or []) >= 2:
        try:
            return int(float(node.children[1].value or "1"))
        except (TypeError, ValueError):
            pass
    return 1


def _inline_library(doc: SNode) -> model.Library:
    """A :class:`model.Library` view of the document's current ``(lib_symbols)``."""
    node = doc.find("lib_symbols")
    if node is not None:
        return kicad_lib.library_from_lib_symbols(node, "<inline>")
    return model.Library(source_path="<inline>", source_format="kicad", symbols=[])


@dataclass
class ExecCtx:
    """Per-``apply`` execution context (one per file, one per run).

    ``symdefs`` memoizes ``lib_id -> SymbolDef`` resolved from ONE cache body
    (re-parsing the growing inline ``lib_symbols`` per placed op made a large
    op-list quadratic); a cache entry never changes within a run.  ``uids`` /
    ``annotation_uids`` / ``power_ports`` are the per-apply claim registries
    (see :func:`_claim_uid`); ``doc_path`` is the file being edited
    (``add_sheet`` resolves its child against that directory); ``pin_tips``
    caches the placed-pin-tip set and is invalidated on every placement.
    """

    doc_path: Path | None = None
    symdefs: dict[str, model.SymbolDef] = field(default_factory=dict)
    text_anchors: list[tuple[int, int]] = field(default_factory=list)
    uids: dict[str, tuple[int, str]] = field(default_factory=dict)
    power_ports: dict = field(default_factory=dict)
    annotation_uids: dict = field(default_factory=dict)
    pin_tips: set[tuple[int, int]] | None = None


def _ctx_new(doc_path: Path | None = None) -> ExecCtx:
    """Fresh per-apply context (kept as a function: txn spawns one per sheet)."""
    return ExecCtx(doc_path=doc_path)


def _claim_uid(ctx: ExecCtx | None, uid: str, idx: int, what: str, hint: str) -> str:
    """Record that op ``idx`` created ``uid``; refuse a second, different claim.

    Node uuids are seeded from CONTENT alone (coordinates, net name, designator
    — never the op index), which is what makes an edited op-list replay onto the
    same nodes instead of duplicating the design downstream of the edit. The
    flip side is that two ops in ONE op-list can now seed the same uuid, and
    ``_append_top_idempotent`` would let the later one silently replace the
    earlier one's node (both reported ``ok``). Two identical wires / two texts at
    one point / two power ports on one coordinate are never a real intent, so
    the collision is a hard error naming both ops — the same contract as the
    annotation ``key`` guard.
    """
    if ctx is None:
        return uid
    prev = ctx.uids.setdefault(uid, (idx, what))
    if prev != (idx, what):
        raise AkcliError(
            "OP_UNSUPPORTED",
            f"op [{idx}] {what} collides with op [{prev[0]}] {prev[1]} "
            f"(both seed the same uuid; the later op would replace the "
            f"earlier node) — {hint}")
    return uid


def _symdef_from_body(body: SNode, lib_id: str) -> model.SymbolDef:
    """Resolve a :class:`SymbolDef` from a single cached ``(symbol ...)`` body."""
    wrapper = SNode.make_list([SNode.atom("lib_symbols"), body])
    lib = kicad_lib.library_from_lib_symbols(wrapper, "<inline>")
    return kicad_lib.resolve(lib_id, [lib])


def _ctx_symdef(doc: SNode, ctx: ExecCtx, lib_id: str) -> model.SymbolDef:
    """Memoized symbol resolution against the document's ``lib_symbols`` cache."""
    sd = ctx.symdefs.get(lib_id)
    if sd is None:
        body = lib_cache.find_cached(doc.find("lib_symbols"), lib_id)
        if body is None:
            fail("SYMBOL_NOT_FOUND", f"lib_id {lib_id!r} not in (lib_symbols ...) cache")
        sd = _symdef_from_body(body, lib_id)
        ctx.symdefs[lib_id] = sd
    return sd


def _instance_component(sym: SNode, lib_id: str):
    """Minimal placement-only Component for :func:`geometry.pin_world`."""
    at = sym.find("at")

    def _f(idx: int, default: float = 0.0) -> float:
        if at is not None and at.children and idx < len(at.children):
            v = at.children[idx].value
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        return default

    px = units.nm_to_mil(units.mm_to_nm(_f(1)))
    py = units.nm_to_mil(units.mm_to_nm(_f(2)))
    rot = int(round(_f(3))) % 360
    mnode = sym.find("mirror")
    mirror = (mnode.children[1].value if mnode and len(mnode.children or []) >= 2 else None) or "none"
    return model.Component(
        designator=_symbol_reference(sym) or "?",
        library_ref=lib_id,
        x_mil=px,
        y_mil=py,
        rotation=rot,
        mirror=mirror,
    )
