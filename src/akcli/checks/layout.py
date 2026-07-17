"""Geometric overlap lint for KiCad schematics (``akcli check --layout``).

A ``.kicad_sch`` can be electrically perfect — clean ERC, exactly the intended
netlist — while its *graphics* are unreadable: a net label anchored on a pin
tip runs over the symbol body, two power ports stack on one coordinate, a
part is dropped on top of a connector. KiCad never checks any of this (ERC is
connectivity-only), and the op-list writer's only hard gate is connectivity,
so overlap slips through every automated draw.

This lint estimates world-space bounding boxes for symbol bodies and label
text and reports intersections. Stroke-font metrics are *approximated*
(KiCad's exact text extents need the font engine), so findings are strong
hints, not proofs — severity is WARNING and the check never blocks a write.

Scope: the root file only (the writer is flat) and ``.kicad_sch`` only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..readers import kicad as _krd
from ..readers import kicad_lib, sexpr
from ..report import Finding, Severity, anchor

LAYOUT_SYMBOL_OVERLAP = "LAYOUT_SYMBOL_OVERLAP"    # two symbol bodies intersect
LAYOUT_LABEL_OVER_SYMBOL = "LAYOUT_LABEL_OVER_SYMBOL"  # label text crosses a body
LAYOUT_LABEL_OVERLAP = "LAYOUT_LABEL_OVERLAP"      # two label texts intersect
LAYOUT_COINCIDENT_TEXT = "LAYOUT_COINCIDENT_TEXT"  # two texts share one anchor
LAYOUT_POWER_ON_PIN = "LAYOUT_POWER_ON_PIN"        # power symbol anchored on a pin tip
LAYOUT_LABEL_OVER_WIRE = "LAYOUT_LABEL_OVER_WIRE"  # label text crosses an unrelated wire
LAYOUT_WIRE_THROUGH_SYMBOL = "LAYOUT_WIRE_THROUGH_SYMBOL"  # wire crosses a symbol body
LAYOUT_GROUP_OVERLAP = "LAYOUT_GROUP_OVERLAP"      # two functional groups' extents intersect
LAYOUT_FRAME_STALE = "LAYOUT_FRAME_STALE"          # a group frame no longer contains its members
LAYOUT_TEXTBOX_OVER_SYMBOL = "LAYOUT_TEXTBOX_OVER_SYMBOL"  # note box drawn over a part

# Text metrics (mil). KiCad default field/label text is 1.27 mm = 50 mil tall;
# the stroke font advances roughly 0.9 * height per character. A global label
# adds its bubble outline + input/output arrow (~1.6 character widths).
_TEXT_H = 50.0
_CHAR_ADV = 0.9 * _TEXT_H
_PAD_GLOBAL = 1.6 * _CHAR_ADV
_PAD_LOCAL = 0.4 * _CHAR_ADV
_HALF_H_GLOBAL = 0.75 * _TEXT_H
_HALF_H_LOCAL = 0.6 * _TEXT_H

# Two boxes must interpenetrate by more than this to count (touching is fine).
_TOL = 1.0
# A wire must run at least this far (mil) INSIDE a box before it is "through"
# it — half a grid step, so pin-stub touches and corner grazes never fire.
_CROSS_MIN = 25.0


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float
    name: str                  # designator or label text
    at: tuple[float, float]    # anchor (mil), for the finding message


def _overlaps(a: _Box, b: _Box) -> bool:
    return (
        a.x0 < b.x1 - _TOL and b.x0 < a.x1 - _TOL
        and a.y0 < b.y1 - _TOL and b.y0 < a.y1 - _TOL
    )


def _key(x: float, y: float) -> tuple[float, float]:
    """Coordinate key robust to mm->mil float noise (0.1-mil buckets)."""
    return (round(x, 1), round(y, 1))


def _seg_in_box(
    a: tuple[float, float], b: tuple[float, float], box: _Box, shrink: float = _TOL
) -> float:
    """Length (mil) of segment ``a->b`` strictly INSIDE ``box`` shrunk by
    ``shrink`` (Liang–Barsky clip); 0 for a miss or a boundary graze."""
    x0, y0 = box.x0 + shrink, box.y0 + shrink
    x1, y1 = box.x1 - shrink, box.y1 - shrink
    if x0 >= x1 or y0 >= y1:
        return 0.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return 0.0  # parallel and outside this edge
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    if t0 >= t1:
        return 0.0
    return math.hypot(dx, dy) * (t1 - t0)


def _on_seg_f(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float],
    tol: float = 0.5,
) -> bool:
    """Float point-on-segment (inclusive of endpoints) within ``tol`` mil."""
    if not (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol):
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1]) <= tol
    return abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / length <= tol


def _mil(node: sexpr.SNode | None, idx: int) -> float:
    return _krd._mm_to_mil(_krd._fnum(node, idx))


def _world_box(
    ext: tuple[float, float, float, float],
    px: float, py: float, rot: int, mirror: str,
    name: str,
) -> _Box:
    """Instance-transform a lib-frame extent to a world (mil, +Y down) box."""
    (x0, y0, x1, y1) = ext
    pts = [
        _krd._pin_world(x, y, px, py, rot, mirror)
        for (x, y) in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    ]
    wx = [p[0] for p in pts]
    wy = [p[1] for p in pts]
    return _Box(min(wx), min(wy), max(wx), max(wy), name, (px, py))


def _label_box(text: str, x: float, y: float, angle: int, is_global: bool) -> _Box:
    """Estimated world box of a label's text (+bubble for global labels)."""
    length = len(text) * _CHAR_ADV + (_PAD_GLOBAL if is_global else _PAD_LOCAL)
    half_h = _HALF_H_GLOBAL if is_global else _HALF_H_LOCAL
    a = int(angle) % 360
    if a == 0:      # text extends +X
        return _Box(x, y - half_h, x + length, y + half_h, text, (x, y))
    if a == 180:    # -X
        return _Box(x - length, y - half_h, x, y + half_h, text, (x, y))
    if a == 90:     # up-screen (-Y)
        return _Box(x - half_h, y - length, x + half_h, y, text, (x, y))
    return _Box(x - half_h, y, x + half_h, y + length, text, (x, y))


def _fmt(p: tuple[float, float]) -> str:
    def r(v: float) -> str:
        iv = round(v)
        return str(int(iv)) if abs(v - iv) < 0.01 else f"{v:.1f}"
    return f"({r(p[0])},{r(p[1])})"


def run(path: str | Path) -> list[Finding]:
    """Lint one ``.kicad_sch`` for geometric overlaps; returns findings."""
    p = Path(path)
    if p.suffix.lower() != ".kicad_sch":
        return [Finding(
            LAYOUT_SYMBOL_OVERLAP, Severity.INFO,
            "layout lint supports .kicad_sch only; skipped", refs=[str(p)],
        )]
    root = sexpr.parse(p.read_text(encoding="utf-8", errors="replace"))

    libsym = root.find("lib_symbols")
    library = (
        kicad_lib.library_from_lib_symbols(libsym) if libsym is not None else None
    )

    sym_boxes: list[_Box] = []          # body only (graphics)
    sym_full_boxes: list[_Box] = []     # body + pin field
    sym_pin_tips: list[set[tuple[float, float]]] = []  # per symbol, world pin tips
    sym_pin_nums: list[dict[tuple[float, float], str]] = []  # keyed tip -> pin number
    sym_power: list[bool] = []          # per symbol, is a (power) symbol
    sym_kind: list[str] = []            # short symbol name ("PWR_FLAG", "+3V3", "R")
    sym_group: list[str | None] = []    # functional group (hidden Group property)
    sym_has_body: list[bool] = []       # body box is real graphics, not pin fallback
    port_pins: dict[tuple[float, float], str] = {}   # power-symbol pin tip -> ref
    raw = _krd._raw_lib_nodes(root)

    for sym in _krd._placed_symbols(root):
        lib_id = _krd._av(sym.find("lib_id"), 1) or ""
        at = sym.find("at")
        px, py = _mil(at, 1), _mil(at, 2)
        rot = int(round(_krd._fnum(at, 3))) % 360
        mnode = sym.find("mirror")
        mirror = (_krd._av(mnode, 1) if mnode is not None else None) or "none"
        unit = int(_krd._fnum(sym.find("unit"), 1, 1.0))
        props = _krd._props(sym)
        ref = props.get("Reference") or lib_id

        try:
            symdef = kicad_lib.resolve(lib_id, [library] if library else [])
        except Exception:
            continue

        ext = kicad_lib.body_extent_mil(symdef, unit)
        pins = kicad_lib.unit_pins(symdef, unit)
        pin_world = [
            _krd._pin_world(lp.x_mil, lp.y_mil, px, py, rot, mirror) for lp in pins
        ]
        if ext is None and pin_world:
            xs = [q[0] for q in pin_world]
            ys = [q[1] for q in pin_world]
            ext_world: _Box | None = _Box(min(xs), min(ys), max(xs), max(ys), ref, (px, py))
        elif ext is not None:
            ext_world = _world_box(ext, px, py, rot, mirror, ref)
        else:
            ext_world = None
        if ext_world is None:
            continue

        sym_boxes.append(ext_world)
        sym_has_body.append(ext is not None)
        xs = [ext_world.x0, ext_world.x1] + [q[0] for q in pin_world]
        ys = [ext_world.y0, ext_world.y1] + [q[1] for q in pin_world]
        sym_full_boxes.append(_Box(min(xs), min(ys), max(xs), max(ys), ref, (px, py)))
        sym_pin_tips.append({(q[0], q[1]) for q in pin_world})
        sym_pin_nums.append({
            _key(q[0], q[1]): lp.number for lp, q in zip(pins, pin_world)
        })
        sym_power.append(_krd._is_power(lib_id, raw))
        sym_kind.append(props.get("Value") or lib_id.split(":")[-1])
        sym_group.append(props.get("Group"))

        if sym_power[-1]:
            for q in pin_world:
                port_pins[q] = ref

    label_boxes: list[tuple[_Box, str]] = []   # (box, scope-tag)
    anchors: dict[tuple[float, float], list[str]] = {}
    for tag in ("label", "global_label", "hierarchical_label"):
        for lb in root.find_all(tag):
            text = _krd._av(lb, 1) or ""
            at = lb.find("at")
            x, y = _mil(at, 1), _mil(at, 2)
            angle = int(round(_krd._fnum(at, 3))) % 360
            label_boxes.append((
                _label_box(text, x, y, angle, is_global=tag != "label"), tag,
            ))
            anchors.setdefault((x, y), []).append(f"{text} [{tag}]")

    wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for w in root.find_all("wire"):
        pts = w.find("pts")
        if pts is None:
            continue
        xy = [(_mil(q, 1), _mil(q, 2)) for q in pts.find_all("xy")]
        wires.extend(zip(xy, xy[1:]))

    findings: list[Finding] = []
    n = len(sym_full_boxes)

    # A power symbol whose pin tip sits ON another symbol's pin tip draws its
    # body over that pin. The generic overlap advice ("move one apart") is
    # WRONG here — moving the port breaks the connection; the fix is anchoring
    # it mid-wire — so this dedicated finding replaces LAYOUT_SYMBOL_OVERLAP
    # for the pair.
    anchored_pairs: set[tuple[int, int]] = set()
    for i in range(n):
        if not sym_power[i]:
            continue
        for tip in sym_pin_tips[i]:
            for j in range(n):
                if j == i or sym_power[j]:
                    continue
                num = sym_pin_nums[j].get(_key(tip[0], tip[1]))
                if num is None:
                    continue
                anchored_pairs.add((min(i, j), max(i, j)))
                who = ("PWR_FLAG" if sym_kind[i].upper() == "PWR_FLAG"
                       else f"power symbol {sym_kind[i]}")
                findings.append(Finding(
                    LAYOUT_POWER_ON_PIN, Severity.WARNING,
                    f"{who} ({sym_boxes[i].name}) anchored on "
                    f"{sym_full_boxes[j].name} pin {num} tip {_fmt(tip)} — it "
                    "renders on top of the pin; move it mid-wire on the net's "
                    "wire instead (see the place_pwr_flag macro)",
                    refs=[sym_boxes[i].name, f"{sym_full_boxes[j].name}.{num}"],
                    pos=tip,
                    anchors=[
                        anchor("component", sym_boxes[i].name, sym_boxes[i].at),
                        anchor("pin", f"{sym_full_boxes[j].name}.{num}", tip),
                    ],
                ))

    for i in range(n):
        for j in range(i + 1, n):
            if (i, j) in anchored_pairs:
                continue  # reported as LAYOUT_POWER_ON_PIN with better advice
            a, b = sym_full_boxes[i], sym_full_boxes[j]
            if _overlaps(a, b):
                findings.append(Finding(
                    LAYOUT_SYMBOL_OVERLAP, Severity.WARNING,
                    f"symbols {a.name} {_fmt(a.at)} and {b.name} {_fmt(b.at)} "
                    "overlap — move one apart (mil coordinates)",
                    refs=[a.name, b.name],
                    pos=a.at,
                    anchors=[anchor("component", a.name, a.at),
                             anchor("component", b.name, b.at)],
                ))

    for k, sb in enumerate(sym_boxes):
        if not sym_has_body[k]:
            continue  # pin-bbox fallback: wires legitimately reach into it
        for a, b in wires:
            if _seg_in_box(a, b, sb) <= _CROSS_MIN:
                continue
            # A wire touching one of this symbol's own pin tips is a
            # connection, not a crossing: graphics overhanging the tip (LED
            # emission arrows) make a terminating wire "penetrate" the hull,
            # and a power symbol anchored mid-wire (the prescribed PWR_FLAG
            # pattern) is bisected by the very wire it flags.
            if any(_on_seg_f(tip, a, b) for tip in sym_pin_tips[k]):
                continue
            findings.append(Finding(
                LAYOUT_WIRE_THROUGH_SYMBOL, Severity.WARNING,
                f"wire {_fmt(a)}-{_fmt(b)} runs through the body of "
                f"{sb.name} {_fmt(sb.at)} — reroute it around the symbol",
                refs=[sb.name],
                pos=sb.at,
                anchors=[anchor("component", sb.name, sb.at)],
            ))

    for box, _tag in label_boxes:
        for k, sb in enumerate(sym_boxes):
            # A label anchored ON one of this symbol's own pin tips is the
            # blessed label-on-pin pattern: test it against the drawn body
            # only. Any other label must also stay out of the pin field.
            own_pin = box.at in sym_pin_tips[k]
            hit = _overlaps(box, sb) or (
                not own_pin and _overlaps(box, sym_full_boxes[k])
            )
            if hit:
                findings.append(Finding(
                    LAYOUT_LABEL_OVER_SYMBOL, Severity.WARNING,
                    f"label '{box.name}' at {_fmt(box.at)} runs over "
                    f"{sb.name} {_fmt(sb.at)} — reorient it away from the symbol "
                    "(the writer auto-orients labels anchored on pins) or move it "
                    "to a wire stub",
                    refs=[box.name, sb.name],
                    pos=box.at,
                    anchors=[anchor("label", box.name, box.at),
                             anchor("component", sb.name, sb.at)],
                ))

    # Label text crossing a wire it is NOT anchored on. A label anchored on a
    # wire (anywhere along it, endpoints included) is the normal label-on-wire
    # pattern and exempt for that wire; only a clear crossing of a foreign
    # wire is worth a note (text metrics are estimates).
    for box, _tag in label_boxes:
        for a, b in wires:
            if _on_seg_f(box.at, a, b):
                continue
            if _seg_in_box(a, b, box) > _CROSS_MIN:
                findings.append(Finding(
                    LAYOUT_LABEL_OVER_WIRE, Severity.NOTE,
                    f"label '{box.name}' at {_fmt(box.at)} runs over the wire "
                    f"{_fmt(a)}-{_fmt(b)} it is not attached to — move or "
                    "reorient the label",
                    refs=[box.name],
                    pos=box.at,
                    anchors=[anchor("label", box.name, box.at)],
                ))
                break

    m = len(label_boxes)
    for i in range(m):
        for j in range(i + 1, m):
            a, b = label_boxes[i][0], label_boxes[j][0]
            if a.at == b.at:
                continue  # reported once below as coincident anchors
            if _overlaps(a, b):
                findings.append(Finding(
                    LAYOUT_LABEL_OVERLAP, Severity.WARNING,
                    f"labels '{a.name}' at {_fmt(a.at)} and '{b.name}' at "
                    f"{_fmt(b.at)} overlap — separate them or shorten the text",
                    refs=[a.name, b.name],
                    pos=a.at,
                    anchors=[anchor("label", a.name, a.at),
                             anchor("label", b.name, b.at)],
                ))

    for pt, texts in anchors.items():
        port = port_pins.get(pt)
        if port is not None:
            texts = texts + [port]
        if len(texts) > 1:
            findings.append(Finding(
                LAYOUT_COINCIDENT_TEXT, Severity.WARNING,
                f"{len(texts)} texts share anchor {_fmt(pt)}: "
                f"{', '.join(texts)} — they render on top of each other",
                refs=texts,
                pos=pt,
                anchors=[anchor("label", t, pt) for t in texts],
            ))

    # ---- note boxes over parts (readability, advisory) --------------------- #
    for c in root.children or []:
        if (not c.is_list or not (c.children or [])
                or c.children[0].value != "text_box"):
            continue
        at = c.find("at")
        size = c.find("size")
        if at is None or size is None:
            continue
        tx, ty = _mil(at, 1), _mil(at, 2)
        tw, th = _mil(size, 1), _mil(size, 2)
        tbox = _Box(min(tx, tx + tw), min(ty, ty + th),
                    max(tx, tx + tw), max(ty, ty + th), "text_box", (tx, ty))
        for i in range(n):
            if sym_has_body[i] and _overlaps(tbox, sym_boxes[i]):
                findings.append(Finding(
                    LAYOUT_TEXTBOX_OVER_SYMBOL, Severity.NOTE,
                    f"a text_box at {_fmt((tx, ty))} covers symbol "
                    f"{sym_boxes[i].name} — move the note box clear of the "
                    "part so both stay readable",
                    refs=[sym_boxes[i].name],
                    pos=(tx, ty),
                ))

    # ---- functional-group lints (advisory, like everything above) --------- #
    group_union: dict[str, _Box] = {}
    for i in range(n):
        g = sym_group[i]
        if not g:
            continue
        b = sym_full_boxes[i]
        u = group_union.get(g)
        group_union[g] = b if u is None else _Box(
            min(u.x0, b.x0), min(u.y0, b.y0),
            max(u.x1, b.x1), max(u.y1, b.y1), g, u.at)
    gnames = sorted(group_union)
    for a_i in range(len(gnames)):
        for b_i in range(a_i + 1, len(gnames)):
            ga, gb = gnames[a_i], gnames[b_i]
            if _overlaps(group_union[ga], group_union[gb]):
                ua, ub = group_union[ga], group_union[gb]
                findings.append(Finding(
                    LAYOUT_GROUP_OVERLAP, Severity.WARNING,
                    f"functional groups {ga!r} and {gb!r} overlap "
                    f"({_fmt((ua.x0, ua.y0))}-{_fmt((ua.x1, ua.y1))} vs "
                    f"{_fmt((ub.x0, ub.y0))}-{_fmt((ub.x1, ub.y1))}) — "
                    "re-pack with `akcli arrange --groups` or move one "
                    "module's origin",
                    refs=[ga, gb],
                    pos=(ua.x0, ua.y0),
                ))
    if group_union:
        from ..groupframe import frame_uuid
        from ..writers.kicad import _root_uuid
        root_uuid = _root_uuid(root)
        expect = {frame_uuid(root_uuid, g): g for g in group_union}
        for c in root.children or []:
            if (not c.is_list or not (c.children or [])
                    or c.children[0].value != "rectangle"):
                continue
            un = c.find("uuid")
            uid = (str(un.children[1].value)
                   if un is not None and len(un.children or []) >= 2 else "")
            g = expect.get(uid)
            if g is None:
                continue
            st, en = c.find("start"), c.find("end")
            if st is None or en is None:
                continue
            rx0, ry0 = _mil(st, 1), _mil(st, 2)
            rx1, ry1 = _mil(en, 1), _mil(en, 2)
            rx0, rx1 = min(rx0, rx1), max(rx0, rx1)
            ry0, ry1 = min(ry0, ry1), max(ry0, ry1)
            u = group_union[g]
            if u.x0 < rx0 or u.y0 < ry0 or u.x1 > rx1 or u.y1 > ry1:
                findings.append(Finding(
                    LAYOUT_FRAME_STALE, Severity.NOTE,
                    f"group frame for {g!r} no longer contains all its "
                    "members (parts moved since it was drawn) — refresh with "
                    "`akcli groups <sch> --frame --apply`",
                    refs=[g],
                    pos=(rx0, ry0),
                ))

    return findings
