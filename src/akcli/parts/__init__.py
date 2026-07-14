"""``akcli.parts`` — JLCPCB/LCSC part search (the ONLY networked package).

Network code is isolated here and imported **lazily** by the ``jlc`` CLI subcommand
only; the rest of akcli stays strictly offline and zero-dependency.
Transport is stdlib ``urllib`` with an injectable ``opener`` so tests run with no
network. See :mod:`akcli.parts.search`.
"""

from __future__ import annotations

# NOTE: only re-export the data types here. We deliberately do NOT bind the
# ``search`` function into this package namespace, because that would shadow the
# ``parts.search`` *submodule* — callers (cli.py, tests) rely on
# ``from .parts import search`` returning the module so they can reach both
# ``search.search`` and ``search.get``.
from .search import JlcNetworkError, Part

__all__ = ["Part", "JlcNetworkError"]
