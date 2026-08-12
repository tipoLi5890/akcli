"""S-expression IR layer (rank 30): the lossless SNode tree.

``parser.py`` (formerly ``readers/sexpr.py``) is the ONE tokenizer/parser
and byte-identical serializer (``dumps``); ``writer.py`` (formerly
``writers/sexpr_writer.py``) holds atom-construction helpers; ``query.py``
holds shared read-only SNode accessors (``root_uuid``, numeric children)
that used to be duplicated across writers/readers/checks.
"""

from .parser import SNode, dumps, parse  # noqa: F401

__all__ = ["SNode", "parse", "dumps"]
