"""Op-list primitives: protocol stamp, error/accessor types, value predicates.

The bottom of the ``akcli.ops`` package — it imports nothing from its siblings
so every other module here may depend on it. Everything in this file is about
the SHAPE of a value (is it a point? a pin ref? a mid() anchor?), never about
which ops exist; the vocabulary itself lives in :mod:`akcli.ops.vocab`.
"""

from __future__ import annotations

from .. import contract

import re
from dataclasses import dataclass

PROTOCOL_VERSION: int = contract.PROTOCOL_VERSIONS["oplist"]

# The default authoring grid (SPEC §2.1). Published through `akcli
# capabilities` as ``ops.constraints.grid_mil`` and used by the mid() snap.
GRID_MIL: float = 50.0


# --------------------------------------------------------------------------- #
# mid() anchor grammar
# --------------------------------------------------------------------------- #
# "mid(REF.PIN,REF.PIN)" — midpoint of two axis-aligned pins. Resolved by the
# writer against the live document (pins must exist when the op executes).
_MID_RE = re.compile(
    r"^mid\(\s*([^,()\s]+\.[^,()\s]+)\s*,\s*([^,()\s]+\.[^,()\s]+)\s*\)$"
)


def parse_mid_anchor(s: object) -> tuple[str, str] | None:
    """The two ``"REF.PIN"`` arguments of a ``"mid(A.p,B.p)"`` anchor, else ``None``."""
    if not isinstance(s, str):
        return None
    m = _MID_RE.match(s)
    return (m.group(1), m.group(2)) if m else None


def _in(value: object, choices: frozenset) -> bool:
    """Membership test that treats an UNHASHABLE value as 'not a member'.

    Enum-checked slots (op / target_format / rotation / mirror / scope / sheet
    pin type|side) reach ``value in <frozenset>``; a list/dict there raises
    ``TypeError``. The validator must NEVER raise for a structural problem — it
    returns OpErrors — so this contains the crash into a clean 'not a member'.
    """
    try:
        return value in choices
    except TypeError:
        return False


@dataclass
class OpError:
    """A single structural problem found in an op-list.

    ``op_index`` is ``-1`` for document-level problems (protocol/target/ops shape).
    ``code`` is a frozen ERROR code from :mod:`errors`.
    """

    op_index: int
    op: str | None
    code: str
    message: str


class Op:
    """Typed accessor wrapper over a raw op dict."""

    __slots__ = ("raw",)

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def name(self) -> str | None:
        return self.raw.get("op")

    def __getitem__(self, key: str):
        return self.raw[key]

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def _is_point(v: object) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v)
    )


def _is_endpoint(v: object) -> bool:
    # endpoint = [x,y] point OR a "REF.PIN" reference string (NOT a mid() anchor)
    if isinstance(v, str):
        if v.startswith("mid("):
            return False
        return v.count(".") >= 1 and not v.startswith(".") and not v.endswith(".")
    return _is_point(v)


def _is_anchor(v: object) -> bool:
    # anchor = endpoint OR "mid(REF.PIN,REF.PIN)" (label / power-port `at`)
    if isinstance(v, str) and v.startswith("mid("):
        return parse_mid_anchor(v) is not None
    return _is_endpoint(v)


def _is_anchor_ref(v: object) -> bool:
    # relative-placement anchor: "REF.PIN" (pin tip) or bare "REF" (origin)
    return (isinstance(v, str) and bool(v) and not v.startswith("mid(")
            and not v.startswith(".") and not v.endswith("."))


def _is_pin_ref(v: object) -> bool:
    return (isinstance(v, str) and not v.startswith("mid(")
            and "." in v and not v.startswith(".") and not v.endswith("."))
