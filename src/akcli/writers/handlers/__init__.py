"""Per-op writer handlers — importing this package registers them all.

One module per op family; every ``_op_*`` carries an ``@op("...")``
decorator from :mod:`..registry`, so the import below IS the registration.
``writers.executor`` imports this package for exactly that side effect.
"""

from __future__ import annotations

from . import (  # noqa: F401 - imported for their @op registrations
    components,
    delete,
    graphics,
    labels,
    sheets,
    wires,
)
