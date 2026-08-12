"""``akcli arrange`` — resolve symbol overlaps by nudging FREE components.

Closes the layout loop: ``draw`` places, ``check --layout`` finds overlaps,
``arrange`` fixes the ones that are safe to fix. A component is only moved
when it is **free** — none of its pin tips carries a wire endpoint or a label
anchor. Moving an anchored component would silently strand its labels/wires
(labels do not travel with ``move_component``), which is exactly the class of
bug ``check --nets`` exists to catch; those are reported as skipped instead.

The planner is a greedy first-fit: components keep their positions in
reading order (top-left first); each overlapping free component slides right
in grid steps (then down a row) until its padded bounding box fits. Moves are
emitted as ``move_component`` ops and applied through the standard draw
pipeline — atomic write, ``.bak``, connectivity re-verify — so ``arrange
--apply`` inherits every safety rail and ``akcli undo`` reverts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import sexpr
from ..geometry.boxes import Box as _Box
from ..geometry.boxes import overlaps as _overlaps
from ..readers import kicad as _krd
from ..safety import read_text_guarded
from .collect import SymInfo, collect as _collect

GRID_MIL = 100.0          # nudge step (2x the 50-mil pin grid)
MARGIN_MIL = 50.0         # required clearance between full boxes
_MAX_TRIES = 400          # bounded search per component


@dataclass
class Move:
    ref: str
    frm: tuple[float, float]
    to: tuple[float, float]

    def to_op(self, *, carry: bool = False) -> dict:
        op = {"op": "move_component", "designator": self.ref,
              "x_mil": self.to[0], "y_mil": self.to[1]}
        if carry:
            op["carry_labels"] = True
            op["carry_wires"] = True
        return op


def groups_from_properties(path: str | Path) -> dict[str, list[str]]:
    """``{group: [refdes, ...]}`` recovered from the hidden ``Group`` property.

    The sheet itself is the group map: components placed with a ``group`` tag
    carry the property, so ``arrange --groups`` (bare, no file) can re-pack
    the modules without the original op-list. Power satellites (``#`` refs)
    are skipped — they ride their host bundle anyway.
    """
    root = sexpr.parse(read_text_guarded(path))
    out: dict[str, list[str]] = {}
    for sym in _krd._placed_symbols(root):
        props = _krd._props(sym)
        gname = props.get("Group")
        ref = props.get("Reference")
        if not gname or not ref or ref.startswith("#"):
            continue
        members = out.setdefault(gname, [])
        if ref not in members:
            members.append(ref)
    return dict(sorted(out.items()))


def _pad(b: _Box, m: float) -> _Box:
    return _Box(b.x0 - m, b.y0 - m, b.x1 + m, b.y1 + m, b.name, b.at)


def _shift(b: _Box, dx: float, dy: float) -> _Box:
    return _Box(b.x0 + dx, b.y0 + dy, b.x1 + dx, b.y1 + dy, b.name, b.at)


def plan(path: str | Path, *, grid: float = GRID_MIL,
         margin: float = MARGIN_MIL) -> dict:
    """Compute the nudges that would make the sheet overlap-free.

    Returns ``{"moves": [Move], "anchored_overlaps": [ref], "clean": bool,
    "symbols": N}``. Never touches the file.
    """
    root = sexpr.parse(read_text_guarded(path))
    syms, anchors = _collect(root)
    for s in syms:
        s.anchored = bool(s.pin_tips & anchors)

    # reading order: anchored symbols are immovable obstacles, free symbols
    # keep their place when possible and slide when they collide
    ordered = sorted(syms, key=lambda s: (s.box.y0, s.box.x0))
    placed: list[_Box] = [s.box for s in ordered if s.anchored]
    moves: list[Move] = []
    anchored_overlaps: list[str] = []

    fixed = [s for s in ordered if s.anchored]
    for a in fixed:
        for b in fixed:
            if a.ref < b.ref and _overlaps(_pad(a.box, margin / 2),
                                           _pad(b.box, margin / 2)):
                anchored_overlaps.extend([a.ref, b.ref])

    for s in ordered:
        if s.anchored:
            continue
        box = _pad(s.box, margin / 2)
        dx = dy = 0.0
        tries = 0
        while any(_overlaps(_shift(box, dx, dy), _pad(p, margin / 2))
                  for p in placed) and tries < _MAX_TRIES:
            dx += grid
            tries += 1
            if tries % 40 == 0:          # give up on the row, start the next
                dx = 0.0
                dy += grid * 4
        if tries >= _MAX_TRIES:
            anchored_overlaps.append(s.ref)
            placed.append(s.box)
            continue
        placed.append(_shift(s.box, dx, dy))
        if dx or dy:
            moves.append(Move(ref=s.ref, frm=s.at,
                              to=(s.at[0] + dx, s.at[1] + dy)))

    return {
        "moves": moves,
        "anchored_overlaps": sorted(set(anchored_overlaps)),
        "clean": not moves and not anchored_overlaps,
        "symbols": len(syms),
    }


# --------------------------------------------------------------------------- #
# group re-layout (`arrange --groups`)
# --------------------------------------------------------------------------- #
# Defaults tuned for a human-refinable starting layout (mils): tight inside a
# group, a wide channel between groups so functional blocks read apart.
GROUP_MARGIN_MIL = 200.0      # clearance between bundles inside a group
GROUP_GAP_MIL = 1200.0        # vertical channel between groups
ROW_WIDTH_MIL = 4000.0        # wrap a group's shelf past this width
_UNGROUPED = "(ungrouped)"


def _union(boxes: list[_Box]) -> _Box:
    return _Box(min(b.x0 for b in boxes), min(b.y0 for b in boxes),
                max(b.x1 for b in boxes), max(b.y1 for b in boxes), "", (0.0, 0.0))


def _bundle_host(sat: SymInfo, anchors: list[SymInfo]) -> SymInfo | None:
    """The anchor a satellite (#PWR/#FLG) rides with: shared pin first, else nearest.

    A power port sits exactly on its host's pin (pin-tip coincidence). A
    PWR_FLAG often lands on a wire MIDPOINT (no pin), so it falls back to the
    nearest anchor by box-centre distance — good enough to keep it in the right
    functional block; the connectivity gate refuses the whole re-layout if that
    guess ever splits a net, so a wrong guess is loud, never silent.
    """
    on_pin = [a for a in anchors if sat.pin_tips & a.pin_tips]
    if on_pin:
        return min(on_pin, key=lambda a: a.ref)
    if not anchors:
        return None
    scx = (sat.box.x0 + sat.box.x1) / 2
    scy = (sat.box.y0 + sat.box.y1) / 2
    return min(anchors, key=lambda a: (
        ((a.box.x0 + a.box.x1) / 2 - scx) ** 2
        + ((a.box.y0 + a.box.y1) / 2 - scy) ** 2))


def plan_groups(path: str | Path, groups: dict[str, list[str]], *,
                margin: float = GROUP_MARGIN_MIL, group_gap: float = GROUP_GAP_MIL,
                row_width: float = ROW_WIDTH_MIL,
                page_width: float | None = None,
                origin: tuple[float, float] | None = None) -> dict:
    """Rigidly relocate each functional group into its own shelf-packed block.

    ``groups`` maps a group name to the component designators it owns (order
    preserved). A satellite power symbol (``#PWR``/``#FLG``) rides with the
    anchor it is attached to; a listed-but-absent or unlisted anchor is
    reported. Each bundle (anchor + its satellites) moves as a RIGID body via
    ``move_component`` with ``carry_labels``/``carry_wires``, so — with the
    label-on-pin pattern — the re-layout is net-preserving by construction.

    Block placement: by default groups stack straight down the page,
    ``group_gap`` apart. With ``page_width`` set, group blocks shelf-pack in
    TWO dimensions — left to right, wrapping past ``page_width`` — with
    ``group_gap`` guaranteed both horizontally and vertically (the layout the
    pod-class boards use: functional neighbours side by side, a routing
    channel between every pair).

    Never touches the file; returns ``{"moves", "unplaced", "groups", "clean",
    "symbols"}``; each ``groups`` entry reports the block's ``at``/``size``
    (mil). The moves are applied through the standard draw pipeline
    (``.bak`` + connectivity verify + ``undo``).
    """
    root = sexpr.parse(read_text_guarded(path))
    syms, _anchors_pts = _collect(root)
    by_ref = {s.ref: s for s in syms}

    anchors = [s for s in syms if not s.ref.startswith("#")]
    satellites = [s for s in syms if s.ref.startswith("#")]

    # anchor -> group (listed wins; the rest fall into a trailing bucket)
    group_of: dict[str, str] = {}
    unplaced: list[str] = []
    ordered_groups: list[str] = []
    for gname, refs in groups.items():
        ordered_groups.append(gname)
        for ref in refs:
            if ref not in by_ref:
                unplaced.append(ref)
            elif ref.startswith("#"):
                # satellites follow their host; naming one explicitly is a no-op
                continue
            else:
                group_of[ref] = gname
    for a in anchors:
        group_of.setdefault(a.ref, _UNGROUPED)
    if any(g == _UNGROUPED for g in group_of.values()):
        ordered_groups.append(_UNGROUPED)

    # satellites ride with an anchor's bundle (same group, same delta)
    members: dict[str, list[SymInfo]] = {a.ref: [a] for a in anchors}
    for sat in satellites:
        host = _bundle_host(sat, anchors)
        if host is not None:
            members[host.ref].append(sat)
        else:
            unplaced.append(sat.ref)

    ox = origin[0] if origin else min((s.box.x0 for s in syms), default=0.0)
    oy = origin[1] if origin else min((s.box.y0 for s in syms), default=0.0)

    # pass 1 — shelf-pack each group's bundles into a LOCAL block at (0,0):
    # per-member deltas into block coordinates plus the block's w x h.
    blocks: list[dict] = []
    for gname in ordered_groups:
        anchor_refs = [a.ref for a in anchors if group_of.get(a.ref) == gname]
        if not anchor_refs:
            continue
        bundles = [(ref, _union([m.box for m in members[ref]])) for ref in anchor_refs]
        rel: list[tuple[SymInfo, float, float]] = []
        shelf_x = 0.0
        shelf_top = 0.0
        shelf_h = 0.0
        block_bottom = 0.0
        block_right = 0.0
        for ref, bbox in bundles:
            bw, bh = bbox.x1 - bbox.x0, bbox.y1 - bbox.y0
            if shelf_x > 0.0 and (shelf_x + bw) > row_width:
                shelf_x = 0.0                              # wrap to the next shelf
                shelf_top = block_bottom + margin
                shelf_h = 0.0
            dx = shelf_x - bbox.x0
            dy = shelf_top - bbox.y0
            for m in members[ref]:
                rel.append((m, dx, dy))
            shelf_x += bw + margin
            shelf_h = max(shelf_h, bh)
            block_bottom = max(block_bottom, shelf_top + shelf_h)
            block_right = max(block_right, shelf_x - margin)
        blocks.append({"group": gname, "anchors": len(anchor_refs),
                       "rel": rel, "w": block_right, "h": block_bottom})

    # pass 2 — place the blocks: a single column by default, or a 2D shelf
    # (wrap past page_width) with group_gap guaranteed on BOTH axes.
    group_report: list[dict] = []
    finals: list[tuple[SymInfo, tuple[float, float]]] = []
    gx, gy = ox, oy
    row_bottom = oy
    for blk in blocks:
        if page_width is not None and gx > ox and (gx - ox) + blk["w"] > page_width:
            gx = ox                                        # wrap to the next band
            gy = row_bottom + group_gap
        for m, dx, dy in blk["rel"]:
            finals.append((m, (m.at[0] + dx + gx, m.at[1] + dy + gy)))
        group_report.append({"group": blk["group"], "anchors": blk["anchors"],
                             "at": [gx, gy], "size": [blk["w"], blk["h"]]})
        row_bottom = max(row_bottom, gy + blk["h"])
        if page_width is not None:
            gx += blk["w"] + group_gap
        else:
            gy = gy + blk["h"] + group_gap

    # pass 3 — emit TWO-PHASE moves through a staging band below everything.
    # Applied sequentially, direct moves can land an early bundle's slot on a
    # not-yet-moved bundle's pin coordinates; the later mover then samples its
    # PRE-move tips and carries the parked bundle's labels away with it. A
    # uniform shift into empty space first (relative geometry preserved), then
    # per-bundle placement from there, makes label carrying collision-free.
    moves: list[Move] = []
    moved = [(m, to) for m, to in finals
             if (round(to[0]), round(to[1])) != (round(m.at[0]), round(m.at[1]))]
    if moved:
        orig_bot = max(s.box.y1 for s in syms)
        final_bot = max(to[1] + (m.box.y1 - m.at[1]) for m, to in finals)
        stage = max(orig_bot, final_bot) - oy + max(group_gap, margin)
        for m, _to in moved:
            moves.append(Move(ref=m.ref, frm=m.at,
                              to=(m.at[0], m.at[1] + stage)))
        for m, to in moved:
            moves.append(Move(ref=m.ref, frm=(m.at[0], m.at[1] + stage), to=to))

    return {
        "moves": moves,
        "unplaced": sorted(set(unplaced)),
        "groups": group_report,
        "clean": not moves,
        "symbols": len(syms),
    }
