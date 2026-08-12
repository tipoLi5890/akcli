"""Layout engines (rank 70): overlap nudging, group re-layout, group frames.

Package layout (formerly ``arrange.py`` + ``groupframe.py``, which reached
into each other and ``checks.layout`` through lazy imports — the one import
cycle in the tree, now unwound into a straight line):

* ``collect.py`` — one-pass symbol/world-box/anchor collection;
* ``plan.py``    — ``plan`` (overlap nudges) and ``plan_groups`` (rigid
  group re-layout), both emitting ``move_component`` ops;
* ``frames.py``  — group border rectangles + titles as ``add_rectangle`` /
  ``add_text`` ops.
"""

from .collect import SymInfo  # noqa: F401
from .plan import (  # noqa: F401
    GRID_MIL,
    GROUP_GAP_MIL,
    GROUP_MARGIN_MIL,
    MARGIN_MIL,
    ROW_WIDTH_MIL,
    Move,
    groups_from_properties,
    plan,
    plan_groups,
)

__all__ = [
    "SymInfo", "Move", "plan", "plan_groups", "groups_from_properties",
    "GRID_MIL", "MARGIN_MIL", "GROUP_MARGIN_MIL", "GROUP_GAP_MIL",
    "ROW_WIDTH_MIL",
]
