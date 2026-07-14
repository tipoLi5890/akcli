"""Seeded fuzz of the op-list layer (zero extra dependencies).

Contracts under test (mirroring tests/test_fuzz_sexpr.py):

* ``ops.validate_oplist`` — for ANY JSON-shaped document it returns a list of
  :class:`ops.OpError` (every ``code`` a member of the frozen
  ``errors.ERROR_CODES`` registry) and never raises;
* ``ops.expand_macros`` — for any mutation of a valid op-list it either
  returns a dict or raises the package's one exception type
  (:class:`AkcliError`), never a raw ``KeyError``/``TypeError``;
* agreement — a document that ``validate_oplist`` accepts cleanly must not
  make ``writers.kicad.apply`` (dry-run) escape with anything but
  :class:`AkcliError`: per-op failures surface as error ``OpResult``s and the
  target file is never written.

Seeds are fixed so failures reproduce. Known, genuine defects found by the
fuzzers are pinned as non-strict ``xfail`` tests at the bottom (they turn
XPASS, not red, once fixed).
"""

from __future__ import annotations

import copy
import random
from pathlib import Path

import pytest

from akcli import ops
from akcli.errors import ERROR_CODES, AkcliError
from akcli.writers import kicad as kw

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"

BLANK_SHEET = (
    '(kicad_sch (version 20231120) (generator "akcli") '
    '(uuid "11111111-2222-3333-4444-555555555555") (paper "A4"))\n'
)

# Keys whose values hit `x in frozenset` membership tests inside the
# validator. An UNHASHABLE value (list/dict) there raises TypeError today —
# a genuine contract violation pinned by
# test_unhashable_enum_slot_values_known_defect below, so the general fuzzers
# feed these slots scalars only and stay green on the rest of the surface.
_ENUM_SLOT_KEYS = frozenset(
    {"op", "target_format", "rotation", "orientation", "mirror", "scope"}
)

_SCALAR_POISON = (
    None, True, False, 0, -1, 3.5, 10**30, float("nan"),
    "", "x", "R1.1", "mid(", "0.1", "x" * 100_000,
)
_ANY_POISON = _SCALAR_POISON + (
    [], {}, [None, True, "x"], {"a": 1}, [[0, 0]], [[[[[0]]]]],
)


def _doc(op_list: list) -> dict:
    return {"protocol_version": 1, "target_format": "kicad", "ops": op_list}


# Hand-built known-valid base documents (guarded by test_base_docs_are_valid).
_CORE_OPS_DOC = [
    {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
     "x_mil": 2000, "y_mil": 1000, "rotation": 90, "mirror": "none", "value": "1k"},
    {"op": "place_component", "lib_id": "Device:C", "designator": "C1",
     "x_mil": 2400, "y_mil": 1000, "unit": 1, "footprint": "Lib:FP"},
    {"op": "add_wire", "vertices": ["R1.1", [2400, 850], "C1.1"]},
    {"op": "add_bus", "vertices": [[0, 0], [500, 0]]},
    {"op": "add_junction", "at": [2400, 850]},
    {"op": "add_no_connect", "pin": "R1.2"},
    {"op": "add_net_label", "name": "CLK", "at": "R1.1", "scope": "global",
     "orientation": 90},
    {"op": "place_power_port", "lib_id": "power:GND", "net_name": "GND",
     "at": [100, 100]},
    {"op": "place_gnd", "at": "C1.2"},
    {"op": "add_bus_entry", "at": [500, 0], "size": [100, 100]},
    {"op": "add_text", "text": "note", "at": [50, 50], "angle": 33.5},
    {"op": "set_component_transform", "designator": "R1", "rotation": 180},
    {"op": "set_component_parameters", "designator": "C1", "value": "100n",
     "parameters": {"MPN": "X"}},
    {"op": "move_component", "designator": "R1", "x_mil": 3000, "y_mil": 1500},
    {"op": "delete_object", "uuid": "aaaa-bbbb"},
    {"op": "delete_component", "designator": "C9"},
]
_MACRO_OPS_DOC = [
    {"op": "place_divider", "x_mil": 1000, "y_mil": 1000, "top_net": "VIN",
     "mid_net": "FB", "bottom_net": "GND", "designators": ["R21", "R22"]},
    {"op": "place_decoupling", "x_mil": 2000, "y_mil": 1000, "power_net": "VCC",
     "designator": "C21"},
    {"op": "place_pullup", "x_mil": 3000, "y_mil": 1000, "net": "SDA",
     "rail_net": "VCC", "designator": "R23"},
    {"op": "place_led_indicator", "x_mil": 4000, "y_mil": 1000, "net": "STAT",
     "designators": ["R24", "D21"]},
    {"op": "place_rc_filter", "x_mil": 5000, "y_mil": 1000, "in_net": "IN",
     "out_net": "OUT", "designators": ["R25", "C22"]},
    {"op": "place_crystal", "x_mil": 6000, "y_mil": 1000, "in_net": "XI",
     "out_net": "XO", "designators": ["Y21", "C23", "C24"]},
    {"op": "connect_and_label", "from": "R21.1", "to": "R23.1", "net": "VIN"},
    {"op": "place_pwr_flag", "at": "mid(R21.1,R23.1)"},
]
_BASE_DOCS = (_doc(_CORE_OPS_DOC), _doc(_MACRO_OPS_DOC))


def test_base_docs_are_valid():
    """Guard: the mutation seeds start from genuinely valid documents."""
    for doc in _BASE_DOCS:
        assert ops.validate_oplist(doc) == []
        expanded = ops.expand_macros(doc)
        assert isinstance(expanded, dict)
        assert ops.validate_oplist(expanded) == []


# --------------------------------------------------------------------------- #
# contract assertions shared by every fuzz layer
# --------------------------------------------------------------------------- #
def _assert_validate_contract(doc: object, note: str) -> None:
    errs = ops.validate_oplist(doc)
    assert isinstance(errs, list), note
    for e in errs:
        assert isinstance(e, ops.OpError), note
        assert e.code in ERROR_CODES, f"{note}: unregistered code {e.code!r}"
        assert isinstance(e.op_index, int) and e.op_index >= -1, note
        assert isinstance(e.message, str), note


def _assert_expand_contract(doc: object, note: str) -> None:
    if not isinstance(doc, dict):
        return                       # expand_macros' precondition is a dict
    try:
        out = ops.expand_macros(doc)
    except AkcliError:
        return                       # structured rejection is a pass
    assert isinstance(out, dict), note


# --------------------------------------------------------------------------- #
# (a) random JSON documents
# --------------------------------------------------------------------------- #
_FIELD_KEYS = (
    "op", "lib_id", "designator", "x_mil", "y_mil", "at", "vertices", "pin",
    "name", "net_name", "text", "uuid", "rotation", "orientation", "mirror",
    "scope", "unit", "value", "size", "angle", "parameters", "match",
    "_note", "bogus_key",
)


def _rand_value(rng: random.Random, key: str, depth: int) -> object:
    if key in _ENUM_SLOT_KEYS:
        return rng.choice(_SCALAR_POISON)
    if depth <= 0 or rng.random() < 0.6:
        return rng.choice(_ANY_POISON)
    if rng.random() < 0.5:
        return [_rand_value(rng, "", depth - 1) for _ in range(rng.randrange(4))]
    return {
        rng.choice(_FIELD_KEYS): _rand_value(rng, rng.choice(_FIELD_KEYS), depth - 1)
        for _ in range(rng.randrange(4))
    }


def _rand_op(rng: random.Random) -> object:
    if rng.random() < 0.25:          # non-dict op slots: None/bool/num/str/list
        return rng.choice(_ANY_POISON)
    op: dict = {}
    if rng.random() < 0.8:
        op["op"] = rng.choice(
            sorted(ops.OP_NAMES | ops.MACRO_OPS) + list(_SCALAR_POISON)
        )
    for _ in range(rng.randrange(6)):
        key = rng.choice(_FIELD_KEYS)
        op[key] = _rand_value(rng, key, 3)
    return op


def _rand_doc(rng: random.Random) -> object:
    shape = rng.randrange(4)
    if shape == 0:                   # non-dict root
        return rng.choice(_ANY_POISON)
    doc: dict = {}
    if shape >= 2:                   # plausible skeleton with drifting header
        doc["protocol_version"] = rng.choice((1, 1, 2, 0, None, "1", [1]))
        doc["target_format"] = rng.choice(("kicad", "altium", "eagle", "", None))
    doc["ops"] = (
        [_rand_op(rng) for _ in range(rng.randrange(6))]
        if rng.random() < 0.8 else rng.choice(_ANY_POISON)
    )
    return doc


@pytest.mark.parametrize("seed", range(8))
def test_fuzz_random_documents_validate_never_raises(seed):
    rng = random.Random(seed)
    for i in range(30):
        doc = _rand_doc(rng)
        _assert_validate_contract(doc, f"seed={seed} iter={i} doc={doc!r:.200}")
        _assert_expand_contract(doc, f"seed={seed} iter={i}")


# --------------------------------------------------------------------------- #
# (b) mutations of known-valid op-lists
# --------------------------------------------------------------------------- #
def _mutate(rng: random.Random, doc: dict) -> dict:
    m = copy.deepcopy(doc)
    kind = rng.randrange(8)
    op_dicts = [o for o in m["ops"] if isinstance(o, dict)]
    victim = rng.choice(op_dicts)
    if kind == 0:                    # drop a required key
        name = victim.get("op")
        req = ops._OP_REQUIRED.get(name) or ops.MACRO_REQUIRED.get(name) or []
        for key in req:
            if key in victim:
                del victim[rng.choice([k for k in req if k in victim])]
                break
        else:
            del victim["op"]
    elif kind == 1:                  # retype a field value
        key = rng.choice([k for k in victim if k != "op"] or ["op"])
        pool = _SCALAR_POISON if key in _ENUM_SLOT_KEYS else _ANY_POISON
        victim[key] = rng.choice(pool)
    elif kind == 2:                  # unknown op name (typo -> did-you-mean path)
        name = str(victim.get("op"))
        i = rng.randrange(len(name))
        victim["op"] = rng.choice((name[:i] + name[i + 1:], name[::-1], "frobnicate"))
    elif kind == 3:                  # protocol drift
        m["protocol_version"] = rng.choice((0, 2, 99, None, "1", -1))
    elif kind == 4:                  # target drift
        m["target_format"] = rng.choice(("eagle", "", None, "KICAD", 7))
    elif kind == 5:                  # unknown / typo'd field key
        victim[rng.choice(("desginator", "x_mils", "zzz", "vertexes"))] = 0
    elif kind == 6:                  # replace a whole op slot with a scalar
        m["ops"][rng.randrange(len(m["ops"]))] = rng.choice(_SCALAR_POISON)
    else:                            # ops is not an array
        m["ops"] = rng.choice((None, True, "x", 3.5, {}, {"op": "add_text"}))
    return m


@pytest.mark.parametrize("seed", range(8))
def test_fuzz_mutations_validate_and_expand(seed):
    rng = random.Random(1000 + seed)
    for i in range(40):
        base = rng.choice(_BASE_DOCS)
        mutant = _mutate(rng, base)
        note = f"seed={seed} iter={i}"
        _assert_validate_contract(mutant, note)
        _assert_expand_contract(mutant, note)


# --------------------------------------------------------------------------- #
# (c) agreement: a clean validate means dry-run apply is AkcliError-contained
# --------------------------------------------------------------------------- #
_GRID = [g * 50 for g in range(-10, 200, 7)]


def _valid_op(rng: random.Random, n: int) -> dict:
    """One structurally VALID op with adversarial-but-well-typed values."""
    x, y = rng.choice(_GRID), rng.choice(_GRID)
    ref = rng.choice(("R", "C", "ZZ")) + str(n)   # unique -> no duplicate lint
    choice = rng.randrange(8)
    if choice == 0:
        return {"op": "place_component",
                "lib_id": rng.choice(("Device:R", "Device:C", "Nope:Missing")),
                "designator": ref, "x_mil": x, "y_mil": y,
                "rotation": rng.choice((0, 90, 180, 270)),
                "mirror": rng.choice(("none", "x", "y"))}
    if choice == 1:                  # endpoints referencing absent components
        return {"op": "add_wire",
                "vertices": [f"{ref}.{rng.randrange(9)}", [x, y]]}
    if choice == 2:
        return {"op": "add_net_label", "name": rng.choice(("N", "x" * 5000)),
                "at": rng.choice(([x, y], f"{ref}.1", "mid(A.1,B.2)")),
                "scope": rng.choice(("local", "global", "hierarchical"))}
    if choice == 3:
        return {"op": "add_junction", "at": [x, y]}
    if choice == 4:                  # NaN/huge are well-typed numbers -> valid
        return {"op": "move_component", "designator": ref,
                "x_mil": rng.choice((x, float("nan"), 1e308)), "y_mil": y}
    if choice == 5:
        return {"op": "place_power_port", "lib_id": "power:GND",
                "net_name": "GND", "at": [x, y]}
    if choice == 6:
        return {"op": "delete_component", "designator": ref}
    return {"op": "add_text", "text": "t\n\"quoted\"\t", "at": [x, y],
            "angle": rng.uniform(-720, 720)}


@pytest.mark.parametrize("seed", range(6))
def test_fuzz_valid_docs_dry_run_contained(seed, tmp_path):
    sheet = tmp_path / "blank.kicad_sch"
    sheet.write_text(BLANK_SHEET)
    rng = random.Random(2000 + seed)
    applied = total = 0
    for i in range(8):
        doc = _doc([_valid_op(rng, 10 * i + k) for k in range(rng.randrange(1, 5))])
        total += 1
        if ops.validate_oplist(doc):
            continue                 # property is conditional on a clean validate
        applied += 1
        try:
            results = kw.apply(doc, str(sheet), apply=False, sources=[str(DEVICE)])
        except AkcliError:
            continue                 # structured rejection is a pass
        assert isinstance(results, list) and len(results) == len(doc["ops"])
        assert all(isinstance(r, kw.OpResult) for r in results)
    assert applied >= total // 2, "generator drifted: too few docs validate clean"
    # dry-run must never touch the target file
    assert sheet.read_text() == BLANK_SHEET


# --------------------------------------------------------------------------- #
# regression: unhashable values in enum-checked slots (formerly xfail)
# --------------------------------------------------------------------------- #
def test_unhashable_enum_slot_values_return_operrors():
    """An UNHASHABLE (list/dict) value in an enum-checked slot (op /
    target_format / rotation / mirror / scope) must yield OpErrors, never a
    TypeError from ``x in frozenset``. Contained by ``ops._in`` — the validator
    and ``expand_macros`` stay total over any JSON-shaped input."""
    place = {"op": "place_component", "lib_id": "a", "designator": "b",
             "x_mil": 0, "y_mil": 0}
    docs = [
        {"protocol_version": 1, "target_format": ["kicad"], "ops": []},
        _doc([{"op": ["place_component"]}]),
        _doc([dict(place, rotation=[90])]),
        _doc([dict(place, mirror={"a": 1})]),
        _doc([{"op": "add_net_label", "name": "N", "at": [0, 0],
               "scope": ["local"]}]),
    ]
    for doc in docs:
        _assert_validate_contract(doc, repr(doc))
        _assert_expand_contract(doc, repr(doc))
