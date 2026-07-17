"""Coordinate / transform core for the KiCad writer (SPEC §3.5).

This is *the module that makes wires hit pins*: it computes the exact world
(sheet) coordinate of a symbol pin given a placed instance, so a wire endpoint
emitted at that coordinate is electrically coincident with the pin.

Hard rules (SPEC §1.1 / §1.2 / §2.1):

* **All geometry is integer nanometres.** Inputs are converted to integer nm and
  every transform is closed over the integers (rotation by 0/90/180/270 and
  mirror only permute/negate coordinates, so no rounding ever creeps in). We only
  stringify to millimetres at serialize time via :func:`nm_to_mm_str`.
* **Canonical frame is +Y down** (KiCad sheet convention). KiCad *library* symbol
  geometry is authored **+Y up**, so :func:`pin_world` applies the library Y-flip
  before the instance transform.
* **Transform order is rotate-then-mirror** (SPEC §2.1), applied about the symbol
  origin, then translated by the instance placement. Rotation is the enum
  ``{0,90,180,270}``; mirror is ``{none,x,y}`` where ``x`` mirrors across the X
  axis (negates Y) and ``y`` mirrors across the Y axis (negates X).

The rotation matrices implement **eeschema's** instance transform, established
empirically against ``kicad-cli sch export netlist`` (KiCad 10.0.4) over the
full rot × mirror matrix (see ``tests/test_kicad_parity.py``). A file angle of
``+90`` rotates the symbol 90° *counter-clockwise on screen*, which in the
+Y-down algebra is:

    0   -> ( x,  y)
    90  -> ( y, -x)
    180 -> (-x, -y)
    270 -> (-y,  x)

followed by the mirror. (The pre-parity code rotated the other way — ``90 ->
(-y, x)`` — which put every rot-90/270 pin on the point-mirrored position;
eeschema then saw the wire miss the pin. The KiCad reader delegates here, so
reader and writer cannot diverge again.)
"""

from __future__ import annotations

from .. import units
from ..model import Component, Pin, SymbolDef

# Re-exported from units so writer code has a single import surface (SPEC §3.5).
from ..units import mil_to_nm, nm_to_mm_str

__all__ = [
    "pin_world",
    "label_angle_away",
    "transform_point",
    "transform_angle",
    "transform_child",
    "grid_snap_nm",
    "mil_to_nm",
    "nm_to_mm_str",
    "DEFAULT_GRID_NM",
]

# Default schematic grid: 50 mil (SPEC §1.1 / §2.1), in integer nm.
DEFAULT_GRID_NM: int = 50 * units.NM_PER_MIL


# ---------------------------------------------------------------------------
# Core point transform (integer nm, rotate-then-mirror, +Y down)
# ---------------------------------------------------------------------------
def transform_point(
    pt: tuple[int, int],
    rot: int = 0,
    mirror: str = "none",
    origin: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """Rotate-then-mirror a point about (0,0), then translate by ``origin``.

    All coordinates are integer nanometres in the canonical **+Y-down** frame.
    ``rot`` is one of ``{0,90,180,270}`` (any other multiple of 90 is normalised;
    non-multiples raise ``ValueError``). ``mirror`` is ``{none,x,y}``. Rotation
    is eeschema's: +90 turns counter-clockwise on screen (see module docstring).
    """
    x = int(pt[0])
    y = int(pt[1])

    r = int(rot) % 360
    if r == 0:
        pass
    elif r == 90:
        x, y = y, -x
    elif r == 180:
        x, y = -x, -y
    elif r == 270:
        x, y = -y, x
    else:
        raise ValueError(f"rotation must be a multiple of 90, got {rot!r}")

    if mirror == "x":          # mirror across the X axis -> negate Y
        y = -y
    elif mirror == "y":        # mirror across the Y axis -> negate X
        x = -x
    elif mirror not in ("none", "", None):
        raise ValueError(f"mirror must be one of none|x|y, got {mirror!r}")

    return (x + int(origin[0]), y + int(origin[1]))


def transform_angle(angle: float, rot: int = 0, mirror: str = "none") -> float:
    """Transform a child text/property angle (degrees) under rotate-then-mirror.

    A rotation adds ``rot`` to a direction angle; ``mirror x`` (negate Y) reflects
    the angle (``a -> -a``); ``mirror y`` (negate X) reflects it about the Y axis
    (``a -> 180 - a``). Result is normalised to ``[0, 360)``.
    """
    a = float(angle) + (int(rot) % 360)
    if mirror == "x":
        a = -a
    elif mirror == "y":
        a = 180.0 - a
    elif mirror not in ("none", "", None):
        raise ValueError(f"mirror must be one of none|x|y, got {mirror!r}")
    return a % 360.0


# Lib-frame (+Y up) unit vectors for the four pin orientations.
_PIN_DIR: dict[int, tuple[int, int]] = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def label_angle_away(pin_orientation: int, rot: int = 0, mirror: str = "none") -> int:
    """KiCad label angle whose text extends AWAY from the symbol body.

    A library pin's ``(at x y a)`` angle points from its electrical tip *toward*
    the body, so the free side of the pin is the opposite direction. This
    transforms that away-vector through the library Y-flip and the instance
    rotate-then-mirror, then maps the world vector to the KiCad label-angle
    convention (0 = text extends +X, 90 = up-screen, 180 = -X, 270 = down-screen).
    A label at the pin tip with this angle reads outward instead of running
    over the symbol it names.
    """
    dx, dy = _PIN_DIR.get(int(pin_orientation) % 360, (1, 0))
    flipped = (-dx, dy)                      # away = -dir, then +Y-up -> +Y-down
    wx, wy = transform_point(flipped, rot, mirror)
    if wx > 0:
        return 0
    if wx < 0:
        return 180
    return 90 if wy < 0 else 270


def transform_child(
    local_nm: tuple[int, int],
    local_angle: float,
    rot: int = 0,
    mirror: str = "none",
    origin: tuple[int, int] = (0, 0),
) -> tuple[tuple[int, int], float]:
    """World ``((x_nm, y_nm), angle_deg)`` for a symbol-local child (property).

    ``local_nm`` is the child's anchor in **symbol-local, +Y-up** nm (the KiCad
    library convention, identical to pin offsets). This applies the library
    Y-flip, then the instance rotate-then-mirror + translate (position via
    :func:`transform_point`, angle via :func:`transform_angle`).
    """
    flipped = (int(local_nm[0]), -int(local_nm[1]))  # +Y up -> +Y down
    world = transform_point(flipped, rot, mirror, origin)
    # The Y-flip negates a direction angle just like mirror across X.
    angle = transform_angle(-float(local_angle), rot, mirror)
    return world, angle


# ---------------------------------------------------------------------------
# Pin world coordinate — the function that makes wires hit pins
# ---------------------------------------------------------------------------
def pin_world(sym: SymbolDef, inst: Component, pin: Pin) -> tuple[int, int]:
    """World coordinate (integer nm, +Y down) of ``pin``'s electrical endpoint.

    ``sym`` is the resolved library symbol, ``inst`` the placed component, and
    ``pin`` one of the symbol's pins (symbol-local, +Y-up mils). The library
    Y-flip is applied first, then the instance rotate-then-mirror about the
    placement, then translation by the instance position.

    A wire endpoint emitted at this coordinate is exactly coincident with the
    pin, which is what the connectivity verifier requires.
    """
    # Symbol-local pin offset, +Y up -> +Y down, in integer nm.
    local = (mil_to_nm(pin.x_mil), -mil_to_nm(pin.y_mil))
    origin = (mil_to_nm(inst.x_mil), mil_to_nm(inst.y_mil))
    return transform_point(local, inst.rotation, inst.mirror, origin)


def world_box_from_extent(
    ext_mil: tuple[float, float, float, float],
    rotation: int, mirror: str, origin_nm: tuple[int, int],
) -> tuple[int, int, int, int]:
    """World (nm, +Y down) AABB of a lib-frame (mil, +Y up) body extent.

    ``ext_mil`` is ``kicad_lib.body_extent_mil``'s ``(x0, y0, x1, y1)``. The
    four corners get the library Y-flip, then the instance rotate-then-mirror
    about ``origin_nm`` — the same chain as :func:`pin_world`, so a body box
    computed here can never disagree with the pins' world coordinates. Shared
    by the writer's property autoplacement, the layout lint and ``akcli bbox``
    (one transform, no parity drift).
    """
    x0, y0, x1, y1 = ext_mil
    pts = [
        transform_point((mil_to_nm(cx), -mil_to_nm(cy)), rotation, mirror, origin_nm)
        for (cx, cy) in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    ]
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def grid_snap_nm(
    pt: tuple[int, int], grid: int = DEFAULT_GRID_NM
) -> tuple[int, int]:
    """Snap an integer-nm point to the nearest ``grid`` multiple (default 50 mil).

    ``grid <= 0`` is a no-op (returns the point as ints).
    """
    if grid <= 0:
        return (int(pt[0]), int(pt[1]))
    g = int(grid)
    return (
        int(round(int(pt[0]) / g)) * g,
        int(round(int(pt[1]) / g)) * g,
    )
