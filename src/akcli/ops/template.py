"""``akcli ops template`` — a fill-in-the-blanks skeleton for one op.

Placeholders are not a table any more: each :class:`akcli.opspec.Field`
carries its own ``template`` value, so the skeleton is a straight read of the
declaration (derived from opspec — the ONE vocabulary source).
"""

from __future__ import annotations

from .. import opspec
from ..opspec import UNSET
from .vocab import (
    _MACRO_TEMPLATE_OMIT,
    _OP_OPTIONAL,
    _OP_REQUIRED,
    MACRO_OPS,
    MACRO_OPTIONAL,
    MACRO_REQUIRED,
    OP_NAMES,
)


def _placeholder(name: str, field: str) -> object:
    """The template value for one field of one op.

    The same field name means different things across ops
    (``add_net_label``'s ``name`` is a net name, ``add_sheet``'s is a sheet
    name), which is why the placeholder is declared per field per op rather
    than globally.
    """
    try:
        spec = opspec.by_name(name)
    except KeyError:
        return f"<{field}>"
    for f in spec.fields:
        if f.name == field:
            return f"<{field}>" if f.template is UNSET else f.template
    return f"<{field}>"


def op_template(name: str, *, include_optional: bool = True) -> dict:
    """A fill-in-the-blanks skeleton for one op (see ``akcli ops template``)."""
    if name in MACRO_OPS:
        op = {"op": name}
        for field in MACRO_REQUIRED.get(name, []):
            op[field] = _placeholder(name, field)
        if include_optional:
            omit = _MACRO_TEMPLATE_OMIT.get(name, frozenset())
            for field, placeholder in MACRO_OPTIONAL.get(name, {}).items():
                if field not in omit:
                    op.setdefault(field, placeholder)
        return op
    if name not in OP_NAMES:
        raise KeyError(name)
    op: dict = {"op": name}
    for field in _OP_REQUIRED.get(name, []):
        op[field] = _placeholder(name, field)
    if include_optional:
        for field, placeholder in _OP_OPTIONAL.get(name, {}).items():
            op.setdefault(field, placeholder)
    return op
