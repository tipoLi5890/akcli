"""Op-handler registry: the ``op`` decorator, ``OpCall``, ``HANDLERS``.

One table, populated by declaration. A handler module decorates each
``_op_*`` with ``@op("<op name>")``; importing :mod:`.handlers` registers
every one of them, and ``tests/test_ops.py``'s census asserts the table's
keys equal ``ops.OP_NAMES`` — so a vocabulary entry without a writer (or a
writer without a vocabulary entry) fails CI.

``OpCall`` bundles the six values every handler used to take positionally,
which keeps the dispatch signature stable as ops grow. ``_Note`` lives here
rather than in :mod:`.executor` because handlers raise it and the layering
gate forbids a handler -> executor back edge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..sexpr import SNode
from .context import ExecCtx


class _Note(Exception):
    """Non-error op outcome carrying a human-readable message (status stays ok)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class OpCall:
    """Everything one op handler is given (SPEC §2.4).

    ``doc``/``inst_path``/``ctx`` are sheet-scoped: constant across a
    single-file ``apply``, routed per-op by the multi-sheet transaction.
    """

    doc: SNode
    op: dict
    index: int
    src_libs: list
    inst_path: str
    ctx: ExecCtx


Handler = Callable[[OpCall], list]

# op name -> handler. Mutated ONLY by the decorator below (and by tests that
# monkeypatch one entry); ``writers.kicad._HANDLERS`` is this same object.
HANDLERS: dict[str, Handler] = {}


def op(*names: str) -> Callable[[Handler], Handler]:
    """Register a handler under one or more op names (sugar shares a handler)."""

    def deco(fn: Handler) -> Handler:
        for name in names:
            if name in HANDLERS:
                raise ValueError(
                    f"duplicate writer handler for op {name!r}: "
                    f"{getattr(HANDLERS[name], '__name__', HANDLERS[name])} "
                    f"and {getattr(fn, '__name__', fn)}")
            HANDLERS[name] = fn
        return fn

    return deco
