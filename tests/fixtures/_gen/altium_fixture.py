"""High-level Altium schematic fixture generator.

Turns hand-authored Altium record dicts into a ``FileHeader`` byte buffer and a
complete ``.SchDoc`` OLE container, plus a reviewable ``.records.txt`` sibling.

Two landmines this module deliberately removes for the fixture author:

* **HEADER record** -- :meth:`SchDocBuilder` records are the *post-header* record
  list; :func:`serialize_fileheader` auto-prepends the Altium ``|HEADER=...|``
  record (which the reader drops via ``recs[1:]``).
* **OwnerIndex off-by-one** -- every ``add``/``component``/``pin`` returns the
  record's **post-header index**; pass that handle straight back as ``owner`` and
  the generator writes the correct ``OwnerIndex``.  A naive author who counted
  the HEADER as record 0 would be off by one; here it is impossible.

The net-regression fixtures (``shared_name_label``, ``junction_cross``,
``t_junction``, ``no_erc``, ``two_gnd_ports``) ship with **hand-authored**
expected netlists (literals in :data:`FIXTURES`).  :func:`compute_netlist` is an
*independent* correct net-inference (global same-name merge, junction dots,
T-junctions, No-ERC) used only to cross-check the literals at generate time -- the
JSON is NEVER snapshotted from a parser.

Self-contained: depends only on the sibling generators (:mod:`ole_writer`,
:mod:`cfbf_builder`), never on a reader module.  Run ``python3 altium_fixture.py``
for a write-then-re-read self-test, or ``python3 altium_fixture.py --emit DIR`` to
(re)generate the committed corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cfbf_builder
import ole_writer

# Pin "PinConglomerate & 3" direction -> unit vector (Altium Location units).
DIRS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}

# FileHeader stream is padded to this many bytes for the fat-chain fixture so the
# stream spans >= 9 sectors (9 * 512 = 4608) and exceeds the 4096-byte mini cutoff.
FATCHAIN_PAD = 4608

_HEADER_TEXT = "Protel for Windows - Schematic Capture Binary File Version 5.0"

# Altium ``Style`` for a GND power port (cosmetic; readers use it for the symbol).
POWER_STYLE_GND = 4


# ---------------------------------------------------------------------------
# Record authoring
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    return v if isinstance(v, str) else str(v)


class SchDocBuilder:
    """Author the *post-header* Altium record list for one schematic sheet."""

    def __init__(self):
        self._recs: list[dict[str, str]] = []

    # -- generic ----------------------------------------------------------
    def add(self, record: int, owner: int | None = None, **fields) -> int:
        """Append one record; return its **post-header index** (the OwnerIndex base)."""
        d: dict[str, str] = {"RECORD": str(record)}
        if owner is not None:
            d["OwnerIndex"] = str(owner)
        for k, v in fields.items():
            d[k] = _fmt(v)
        self._recs.append(d)
        return len(self._recs) - 1

    # -- typed helpers ----------------------------------------------------
    def component(self, designator: str, x: int = 0, y: int = 0,
                  lib_ref: str = "DEVICE", **extra) -> int:
        idx = self.add(1, **{"Location.X": x, "Location.Y": y,
                             "LibReference": lib_ref}, **extra)
        self.add(34, owner=idx, Text=designator)        # RECORD 34 = Designator
        return idx

    def pin(self, owner: int, number: str, name: str, tip_x: int, tip_y: int,
            direction: int = 2, length: int = 10, electrical: int = 4,
            part: int = 1) -> int:
        dx, dy = DIRS[direction & 3]
        return self.add(
            2, owner=owner,
            **{"Designator": number, "Name": name,
               "Location.X": tip_x - length * dx, "Location.Y": tip_y - length * dy,
               "PinLength": length, "PinConglomerate": direction & 3,
               "Electrical": electrical, "OwnerPartId": part})

    def wire(self, *points) -> int:
        f: dict = {"LocationCount": len(points)}
        for i, (x, y) in enumerate(points, 1):
            f[f"X{i}"] = x
            f[f"Y{i}"] = y
        return self.add(27, **f)

    def junction(self, x: int, y: int) -> int:
        return self.add(29, **{"Location.X": x, "Location.Y": y})

    def net_label(self, x: int, y: int, text: str) -> int:
        return self.add(25, **{"Location.X": x, "Location.Y": y, "Text": text})

    def power_port(self, x: int, y: int, text: str, style: int = POWER_STYLE_GND) -> int:
        return self.add(17, **{"Location.X": x, "Location.Y": y,
                               "Text": text, "Style": style})

    def no_erc(self, x: int, y: int) -> int:
        return self.add(22, **{"Location.X": x, "Location.Y": y})

    # -- parameters / model links -------------------------------------------
    def parameter(self, owner: int, name: str, text: str = "") -> int:
        """RECORD 41 owned by ``owner`` (a component -> ``Component.parameters``)."""
        return self.add(41, owner=owner, Name=name, Text=text)

    def doc_parameter(self, name: str, text: str = "") -> int:
        """RECORD 41 with NO OwnerIndex: an Altium *document* parameter (title block)."""
        return self.add(41, Name=name, Text=text)

    def model(self, owner: int, model_name: str, model_type: str = "PCBLIB",
              is_current: bool | None = None, **extra) -> int:
        """The RECORD-44 -> RECORD-45 model link Altium writes under a component.

        ``model_type`` is Altium's ``ModelType`` (``PCBLIB`` footprint, ``SIM``
        spice model, ``SI``, ``PCB3DLib``); ``is_current`` writes ``IsCurrent=T/F``
        (omitted when None). Returns the RECORD-45 index.
        """
        impl = self.add(44, owner=owner)
        f: dict = {"ModelName": model_name}
        if model_type is not None:
            f["ModelType"] = model_type
        if is_current is not None:
            f["IsCurrent"] = "T" if is_current else "F"
        f.update(extra)
        return self.add(45, owner=impl, **f)

    # -- hierarchy records --------------------------------------------------
    def sheet_symbol(self, x: int, y: int, w: int, h: int) -> int:
        """RECORD 15: Location = TOP-LEFT corner (+Y-up frame); extends right/down."""
        return self.add(15, **{"Location.X": x, "Location.Y": y,
                               "XSize": w, "YSize": h})

    def sheet_name(self, owner: int, text: str) -> int:
        return self.add(32, owner=owner, Text=text)

    def sheet_file(self, owner: int, filename: str) -> int:
        return self.add(33, owner=owner, Text=filename)

    def sheet_entry(self, owner: int, name: str, side: int = 0,
                    distance: int = 0, iotype: int = 0) -> int:
        """RECORD 16: Side 0=left 1=right 2=top 3=bottom; DistanceFromTop in
        1/10 Location units from the side's origin (Altium convention)."""
        return self.add(16, owner=owner, **{"Name": name, "Side": side,
                                            "DistanceFromTop": distance,
                                            "IOType": iotype})

    def port(self, x: int, y: int, text: str, iotype: int = 3) -> int:
        return self.add(18, **{"Location.X": x, "Location.Y": y,
                               "Text": text, "IOType": iotype})


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _frame(fields: dict[str, str]) -> bytearray:
    """One Altium record: 3-byte LE length + 1 flag byte (0=text) + NUL-terminated payload."""
    payload = "".join(f"|{k}={v}" for k, v in fields.items()) + "|"
    pb = payload.encode("latin-1", "replace") + b"\x00"
    ln = len(pb)
    if ln > 0xFFFFFF:
        raise ValueError("record exceeds 24-bit length")
    return bytearray(ln.to_bytes(3, "little") + b"\x00" + pb)


def serialize_fileheader(records: list[dict[str, str]], *,
                         pad_to_at_least: int = 0, weight: int | None = None) -> bytes:
    """Auto-prepend the HEADER record and frame ``records`` into a FileHeader buffer.

    If ``pad_to_at_least`` is set and the buffer is shorter, the **last** record's
    declared length is grown with trailing NUL bytes.  The Altium framing strips
    trailing NULs (``rstrip(b"\\x00")``), so the padded buffer parses to the
    *identical* record list -- this is how the fat-chain and miniFAT fixtures share
    one record set across two container layouts.
    """
    if weight is None:
        weight = len(records)
    header = {"HEADER": _HEADER_TEXT, "WEIGHT": str(weight)}
    frames = [_frame(header)] + [_frame(r) for r in records]
    total = sum(len(f) for f in frames)
    if total < pad_to_at_least:
        extra = pad_to_at_least - total
        last = frames[-1]
        ln = last[0] | (last[1] << 8) | (last[2] << 16)
        last[0:3] = (ln + extra).to_bytes(3, "little")
        last.extend(b"\x00" * extra)
    return b"".join(bytes(f) for f in frames)


def records_txt(records: list[dict[str, str]]) -> str:
    """Human-reviewable dump: post-header index + fields (OwnerIndex is index-relative)."""
    lines = [
        "# Altium FileHeader records (reviewable sibling -- NOT parsed at runtime).",
        "# Index is the POST-HEADER position: the reader drops record 'H' (HEADER),",
        "# so OwnerIndex N refers to the record printed with index N below.",
        " H : |HEADER=%s| (auto-prepended)" % _HEADER_TEXT,
    ]
    for i, d in enumerate(records):
        kv = " ".join(f"{k}={v}" for k, v in d.items())
        lines.append(f"{i:2d} : {kv}")
    return "\n".join(lines) + "\n"


def write_schdoc(path: str, builder: SchDocBuilder, *, layout: str = "minifat",
                 emit_records_txt: bool = True) -> bytes:
    """Write ``builder``'s records as a ``.SchDoc`` OLE container at ``path``."""
    pad = FATCHAIN_PAD if layout == "fatchain" else 0
    fh = serialize_fileheader(builder._recs, pad_to_at_least=pad)
    ole_writer.write_ole(path, {"FileHeader": fh}, layout=layout)
    if emit_records_txt:
        base = path[:-len(".SchDoc")] if path.endswith(".SchDoc") else path
        with open(base + ".records.txt", "w", encoding="utf-8") as f:
            f.write(records_txt(builder._recs))
    return fh


# ---------------------------------------------------------------------------
# Independent net inference (cross-checks the hand-authored expected JSON)
# ---------------------------------------------------------------------------
def _on_seg(p, a, b) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def compute_netlist(builder: SchDocBuilder):
    """Correct net inference over a builder's records (for self-validation only).

    Implements: wire union-find, junction-dot merge, T-junction (vertex on another
    wire's mid-span), pin/label attachment, and the GLOBAL same-name merge that the
    legacy parser lacked.  Returns ``(nets, no_erc_points)`` where ``nets`` is a list
    of ``{"name", "is_named", "members"}`` and members are sorted ``[des, number]``.
    """
    recs = builder._recs

    desig: dict[int, str] = {}
    for i, d in enumerate(recs):
        if d.get("RECORD") == "34":
            desig[int(d["OwnerIndex"])] = d.get("Text")
    comp_des = {i: desig.get(i) for i, d in enumerate(recs) if d.get("RECORD") == "1"}

    pins = []          # (designator, number, tip)
    for d in recs:
        if d.get("RECORD") != "2":
            continue
        oi = int(d["OwnerIndex"])
        x, y = int(d["Location.X"]), int(d["Location.Y"])
        dx, dy = DIRS[int(d.get("PinConglomerate", "0")) & 3]
        ln = int(d.get("PinLength", "0"))
        pins.append((comp_des.get(oi), d.get("Designator"), (x + ln * dx, y + ln * dy)))

    segs, wires = [], []
    for d in recs:
        if d.get("RECORD") != "27":
            continue
        n = int(d["LocationCount"])
        pts = [(int(d[f"X{k}"]), int(d[f"Y{k}"])) for k in range(1, n + 1)]
        segs += list(zip(pts, pts[1:]))
        wires.append(pts)

    junctions = [(int(d["Location.X"]), int(d["Location.Y"]))
                 for d in recs if d.get("RECORD") == "29"]
    named = []         # (point, text, scope)
    for d in recs:
        if d.get("RECORD") == "25":
            named.append(((int(d["Location.X"]), int(d["Location.Y"])), d.get("Text"), "label"))
        elif d.get("RECORD") == "17":
            named.append(((int(d["Location.X"]), int(d["Location.Y"])), d.get("Text"), "power"))
    no_erc = [(int(d["Location.X"]), int(d["Location.Y"]))
              for d in recs if d.get("RECORD") == "22"]

    parent: dict = {}

    def find(a):
        parent.setdefault(a, a)
        r = a
        while parent[r] != r:
            r = parent[r]
        while parent[a] != r:
            parent[a], a = r, parent[a]
        return r

    def union(a, b):
        parent[find(a)] = find(b)

    for a, b in segs:
        union(a, b)
    for j in junctions:                       # junction dot merges everything it touches
        for a, b in segs:
            if _on_seg(j, a, b):
                union(j, a)
                union(j, b)
    verts = {p for pts in wires for p in pts}
    for v in verts:                           # T-junction: vertex on another wire's span
        for a, b in segs:
            if v != a and v != b and _on_seg(v, a, b):
                union(v, a)
    for pt in [t for *_, t in pins] + [p for p, _, _ in named]:
        for a, b in segs:
            if pt == a or pt == b or _on_seg(pt, a, b):
                union(pt, a)

    by_name: dict = defaultdict(list)         # GLOBAL same-name merge
    for pt, text, _scope in named:
        if text:
            by_name[text].append(pt)
    for pts in by_name.values():
        for p in pts[1:]:
            union(pts[0], p)

    root_power, root_label = {}, {}
    for pt, text, scope in named:
        if not text:
            continue
        r = find(pt)
        (root_power if scope == "power" else root_label).setdefault(r, text)

    members: dict = defaultdict(set)
    for des, num, tip in pins:
        if des is None:
            continue
        members[find(tip)].add((des, num))

    nets = []
    for r, mem in members.items():
        name = root_power.get(r) or root_label.get(r)
        nets.append({"name": name, "is_named": name is not None,
                     "members": sorted([list(m) for m in mem])})
    nets.sort(key=lambda n: (n["members"][0] if n["members"] else []))
    return nets, [list(p) for p in no_erc]


# ---------------------------------------------------------------------------
# Net-regression fixture definitions (hand-authored)
# ---------------------------------------------------------------------------
def _fx_shared_name_label():
    """Two same-Text 'STAT' labels on disjoint clusters -> ONE merged net (STAT/LED1 class)."""
    b = SchDocBuilder()
    u2 = b.component("U2", 900, 1000)
    b.pin(u2, "1", "STAT", 1000, 1000)
    r7 = b.component("R7", 2100, 1000)
    b.pin(r7, "1", "A", 2000, 1000)
    b.wire((1000, 1000), (2000, 1000))
    b.net_label(1500, 1000, "STAT")
    u3 = b.component("U3", 900, 2000)
    b.pin(u3, "2", "LED", 1000, 2000)
    r12 = b.component("R12", 2100, 2000)
    b.pin(r12, "1", "B", 2000, 2000)
    b.wire((1000, 2000), (2000, 2000))
    b.net_label(1500, 2000, "STAT")
    expected = {
        "description": "Two disjoint wire clusters each carry a net label with the "
                       "same Text 'STAT'. The global same-name merge stitches them "
                       "into ONE net of 4 pins (the STAT/LED1_GPIO_RD class the legacy "
                       "parser split). Zero single-pin nets.",
        "coordinate_units": "altium_location_units",
        "single_pin_net_count": 0,
        "nets": [{"name": "STAT", "is_named": True,
                  "members": [["R12", "1"], ["R7", "1"], ["U2", "1"], ["U3", "2"]]}],
    }
    return b, expected


def _fx_junction_cross():
    """A '+' crossing WITH a RECORD-29 junction dot -> all 4 arms merge into one net."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 2000)
    b.pin(u1, "1", "A", 1000, 2000)
    u2 = b.component("U2", 3100, 2000)
    b.pin(u2, "1", "B", 3000, 2000)
    u3 = b.component("U3", 2000, 900)
    b.pin(u3, "1", "C", 2000, 1000, direction=3)
    u4 = b.component("U4", 2000, 3100)
    b.pin(u4, "1", "D", 2000, 3000, direction=1)
    b.wire((1000, 2000), (3000, 2000))       # horizontal
    b.wire((2000, 1000), (2000, 3000))       # vertical
    b.junction(2000, 2000)                   # the dot that makes the crossing a connection
    expected = {
        "description": "A horizontal and a vertical wire cross at (2000,2000) with a "
                       "RECORD-29 junction dot. The dot merges both wires -> ONE net of "
                       "4 pins. Without the dot a bare crossing is NOT a connection.",
        "coordinate_units": "altium_location_units",
        "single_pin_net_count": 0,
        "nets": [{"name": None, "is_named": False,
                  "members": [["U1", "1"], ["U2", "1"], ["U3", "1"], ["U4", "1"]]}],
    }
    return b, expected


def _fx_t_junction():
    """A wire endpoint landing on another wire's mid-span -> T-junction merge (no dot)."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 2000)
    b.pin(u1, "1", "A", 1000, 2000)
    u2 = b.component("U2", 3100, 2000)
    b.pin(u2, "1", "B", 3000, 2000)
    u3 = b.component("U3", 2000, 4100)
    b.pin(u3, "1", "C", 2000, 4000, direction=1)
    b.wire((1000, 2000), (3000, 2000))       # horizontal trunk
    b.wire((2000, 2000), (2000, 4000))       # vertical branch; top vertex on the trunk span
    expected = {
        "description": "The vertical wire's top vertex (2000,2000) lands on the "
                       "horizontal wire's mid-span -> a T-junction connection with NO "
                       "explicit dot. All 3 pins form ONE net.",
        "coordinate_units": "altium_location_units",
        "single_pin_net_count": 0,
        "nets": [{"name": None, "is_named": False,
                  "members": [["U1", "1"], ["U2", "1"], ["U3", "1"]]}],
    }
    return b, expected


def _fx_no_erc():
    """A RECORD-22 No-ERC marker on a deliberately open pin -> that pin is suppressed."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000)
    b.pin(u1, "1", "A", 1000, 1000)
    b.pin(u1, "2", "NC", 1000, 1500, direction=3)    # open pin (nothing attached)
    u2 = b.component("U2", 2100, 1000)
    b.pin(u2, "1", "B", 2000, 1000)
    b.wire((1000, 1000), (2000, 1000))
    b.no_erc(1000, 1500)                              # blesses the open pin U1.2
    expected = {
        "description": "U1.1<->U2.1 are wired together; U1.2 is intentionally open and "
                       "carries a RECORD-22 No-ERC marker at its tip (1000,1500) so an "
                       "open-pin ERC check is suppressed there.",
        "coordinate_units": "altium_location_units",
        "single_pin_net_count": 1,
        "no_erc_points": [[1000, 1500]],
        "nets": [
            {"name": None, "is_named": False, "members": [["U1", "1"], ["U2", "1"]]},
            {"name": None, "is_named": False, "members": [["U1", "2"]],
             "note": "open pin, suppressed by No-ERC"},
        ],
    }
    return b, expected


def _fx_two_gnd_ports():
    """Two same-name 'GND' power ports on separate clusters -> collapse to one GND net."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000)
    b.pin(u1, "1", "A", 1000, 1000)
    u2 = b.component("U2", 2100, 1000)
    b.pin(u2, "1", "B", 2000, 1000)
    b.wire((1000, 1000), (2000, 1000))
    b.power_port(1500, 1000, "GND")
    u3 = b.component("U3", 900, 2000)
    b.pin(u3, "1", "C", 1000, 2000)
    u4 = b.component("U4", 2100, 2000)
    b.pin(u4, "1", "D", 2000, 2000)
    b.wire((1000, 2000), (2000, 2000))
    b.power_port(1500, 2000, "GND")
    expected = {
        "description": "Two disjoint clusters each carry a 'GND' power port. Power ports "
                       "are global, so the same-name merge collapses both clusters into "
                       "ONE net named GND (4 pins).",
        "coordinate_units": "altium_location_units",
        "single_pin_net_count": 0,
        "nets": [{"name": "GND", "is_named": True,
                  "members": [["U1", "1"], ["U2", "1"], ["U3", "1"], ["U4", "1"]]}],
    }
    return b, expected


def _fx_demo():
    """Representative small sheet shared by ole_minifat.SchDoc and ole_fatchain.SchDoc."""
    b = SchDocBuilder()
    u1 = b.component("U1", 900, 1000, lib_ref="RES")
    b.pin(u1, "1", "P0.25", 1000, 1000, electrical=4)
    u2 = b.component("U2", 2100, 1000, lib_ref="CAP")
    b.pin(u2, "1", "VDD", 2000, 1000, electrical=7)
    b.wire((1000, 1000), (2000, 1000))
    b.net_label(1500, 1000, "V3V3")
    return b


# name -> factory  (for the committed net-regression corpus)
FIXTURES = {
    "shared_name_label": _fx_shared_name_label,
    "junction_cross": _fx_junction_cross,
    "t_junction": _fx_t_junction,
    "no_erc": _fx_no_erc,
    "two_gnd_ports": _fx_two_gnd_ports,
}


# ---------------------------------------------------------------------------
# Corpus emission
# ---------------------------------------------------------------------------
def _norm_nets(nets):
    return sorted((n["name"], n["is_named"], tuple(tuple(m) for m in n["members"]))
                  for n in nets)


def emit_corpus(dest_dir: str) -> list[str]:
    """(Re)generate the committed fixture corpus under ``dest_dir``; return file paths."""
    os.makedirs(dest_dir, exist_ok=True)
    malformed_dir = os.path.join(dest_dir, "malformed")
    os.makedirs(malformed_dir, exist_ok=True)
    written: list[str] = []

    def _write(path, data, *, binary):
        mode = "wb" if binary else "w"
        with open(path, mode, **({} if binary else {"encoding": "utf-8"})) as f:
            f.write(data)
        written.append(path)

    # --- two-layout demo container (identical records, mini vs fat-chain) ---
    demo = _fx_demo()
    for name, layout in (("ole_minifat", "minifat"), ("ole_fatchain", "fatchain")):
        p = os.path.join(dest_dir, f"{name}.SchDoc")
        write_schdoc(p, demo, layout=layout)
        written.append(p)
        written.append(os.path.join(dest_dir, f"{name}.records.txt"))

    # --- net-regression fixtures (.SchDoc + hand-authored expected .json) ---
    for name, factory in FIXTURES.items():
        builder, expected = factory()
        got_nets, got_no_erc = compute_netlist(builder)
        if _norm_nets(got_nets) != _norm_nets(expected["nets"]):
            raise AssertionError(
                f"{name}: hand-authored expected nets disagree with independent "
                f"compute_netlist:\n  expected={_norm_nets(expected['nets'])}\n"
                f"  computed={_norm_nets(got_nets)}")
        if "no_erc_points" in expected and got_no_erc != expected["no_erc_points"]:
            raise AssertionError(f"{name}: no_erc_points mismatch "
                                 f"{got_no_erc} != {expected['no_erc_points']}")
        p = os.path.join(dest_dir, f"{name}.SchDoc")
        write_schdoc(p, builder, layout="minifat")
        written.append(p)
        written.append(os.path.join(dest_dir, f"{name}.records.txt"))
        _write(os.path.join(dest_dir, f"{name}.expected.json"),
               json.dumps(expected, indent=2, ensure_ascii=False) + "\n", binary=False)

    # --- malformed OLE corpus ----------------------------------------------
    for fname, fn in cfbf_builder.MALFORMED_OLE.items():
        _write(os.path.join(malformed_dir, fname), fn(), binary=True)

    # --- malformed S-expression corpus (for the KiCad sexpr fuzz tests) -----
    _write(os.path.join(malformed_dir, "deeply_nested.kicad_sch"),
           "(" * 200000 + ")" * 200000 + "\n", binary=False)
    _write(os.path.join(malformed_dir, "huge_atom.kicad_sch"),
           "(symbol " + "a" * 10_000_000 + ")\n", binary=False)
    _write(os.path.join(malformed_dir, "unterminated_quote.kicad_sch"),
           '(kicad_sch (property "value "unterminated)\n', binary=False)

    _write_manifest(dest_dir, written)
    return written


def _iter_fixture_files(dest_dir: str):
    """All committed fixture files (excludes _gen sources and the manifest itself)."""
    for root, dirs, files in os.walk(dest_dir):
        dirs[:] = [d for d in dirs if d not in ("_gen", "__pycache__")]
        for fn in files:
            if fn == "MANIFEST.sha256":
                continue
            full = os.path.join(root, fn)
            yield full, os.path.relpath(full, dest_dir).replace(os.sep, "/")


def _write_manifest(dest_dir: str, written: list[str]):
    lines = []
    for full, rel in sorted(_iter_fixture_files(dest_dir), key=lambda t: t[1]):
        with open(full, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        lines.append(f"{digest}  {rel}")
    with open(os.path.join(dest_dir, "MANIFEST.sha256"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Reference framing (only for the self-test; mirrors readers/altium_records.py)
# ---------------------------------------------------------------------------
def _parse_records_for_selftest(buf: bytes) -> list[str]:
    recs, pos = [], 0
    while pos + 4 <= len(buf):
        ln = buf[pos] | (buf[pos + 1] << 8) | (buf[pos + 2] << 16)
        pos += 4
        recs.append(buf[pos:pos + ln].rstrip(b"\x00").decode("latin-1", "replace"))
        pos += ln
    return recs


def _selftest():
    # 1. record builder round-trips through the OLE container.
    b = _fx_demo()
    fh = serialize_fileheader(b._recs)
    blob = ole_writer.to_ole({"FileHeader": fh}, "minifat")
    back = ole_writer.read_cfbf(blob)["FileHeader"]
    assert back == fh, "FileHeader did not round-trip through CFBF"

    # 2. miniFAT vs fat-chain layouts parse to IDENTICAL records.
    fh_mini = serialize_fileheader(b._recs)
    fh_fat = serialize_fileheader(b._recs, pad_to_at_least=FATCHAIN_PAD)
    assert len(fh_mini) < cfbf_builder.MINI_CUTOFF, "mini FileHeader unexpectedly large"
    assert len(fh_fat) >= cfbf_builder.MINI_CUTOFF, "fat FileHeader too small"
    mini_streams = ole_writer.read_cfbf(ole_writer.to_ole({"FileHeader": fh_mini}, "minifat"))
    fat_streams = ole_writer.read_cfbf(ole_writer.to_ole({"FileHeader": fh_fat}, "fatchain"))
    r_mini = _parse_records_for_selftest(mini_streams["FileHeader"])
    r_fat = _parse_records_for_selftest(fat_streams["FileHeader"])
    assert r_mini == r_fat, "miniFAT vs fat-chain records differ"
    # HEADER is record 0; the post-header list is what readers keep (recs[1:]).
    assert r_mini[1:] == [
        "".join(f"|{k}={v}" for k, v in d.items()) + "|" for d in b._recs
    ], "post-header records do not match the authored set"
    print(f"  layout-parity OK ({len(fh_mini)}B mini / {len(fh_fat)}B fat, "
          f"{len(r_mini)} records each)")

    # 3. every net-regression fixture's hand-authored JSON matches the independent
    #    correct net inference, and parses back out of its own OLE container.
    for name, factory in FIXTURES.items():
        builder, expected = factory()
        nets, no_erc = compute_netlist(builder)
        assert _norm_nets(nets) == _norm_nets(expected["nets"]), f"{name}: net mismatch"
        if "no_erc_points" in expected:
            assert no_erc == expected["no_erc_points"], f"{name}: no_erc mismatch"
        fh = serialize_fileheader(builder._recs)
        got = ole_writer.read_cfbf(ole_writer.to_ole({"FileHeader": fh}, "minifat"))
        assert _parse_records_for_selftest(got["FileHeader"])[1:], f"{name}: empty parse"
        print(f"  [{name}] {len(expected['nets'])} net(s), {len(builder._recs)} records OK")

    print("altium_fixture self-test OK")


if __name__ == "__main__":   # pragma: no cover - self-test / emitter
    if len(sys.argv) >= 3 and sys.argv[1] == "--emit":
        files = emit_corpus(sys.argv[2])
        print(f"emitted {len(files)} files under {sys.argv[2]}")
    else:
        _selftest()
