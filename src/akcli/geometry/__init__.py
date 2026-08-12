"""Spatial layer (rank 40): the ONE coordinate-transform implementation.

``transform.py`` (formerly ``writers/geometry.py``) owns rotate-then-mirror,
pin world coordinates and grid snapping; ``boxes.py`` owns bounding-box
primitives (formerly private helpers inside ``checks/layout.py``).  Readers,
writers, checks and renderers all delegate here — the reader once kept its
own mirror-then-rotate copy that disagreed with both the writer and
eeschema, which is exactly the divergence this layer exists to prevent.
"""

from .transform import (  # noqa: F401
    point_world_mil,
    DEFAULT_GRID_NM,
    grid_snap_nm,
    label_angle_away,
    mil_to_nm,
    nm_to_mm_str,
    pin_world,
    transform_angle,
    transform_child,
    transform_point,
    world_box_from_extent,
)

__all__ = [
    "pin_world",
    "label_angle_away",
    "transform_point",
    "transform_angle",
    "transform_child",
    "grid_snap_nm",
    "mil_to_nm",
    "nm_to_mm_str",
    "world_box_from_extent",
    "point_world_mil",
    "DEFAULT_GRID_NM",
]
