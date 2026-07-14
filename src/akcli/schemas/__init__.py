"""Packaged mirror of the repo-root ``schemas/`` JSON contracts.

The repo-root copies are CANONICAL — edit those, then copy them here so
installed wheels (where no repo root exists) can serve them via
``importlib.resources`` (see :func:`akcli.ops._schema_text`).
``tests/test_schema_exports.py`` asserts the two copies are byte-identical,
so a drifted mirror can never ship.
"""
