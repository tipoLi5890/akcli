"""Ground truth beyond the netlist: the optional ``expected_facts.json``.

``expected_nets.json`` can only say which pins share a named net. An EDIT task
("move C1 without changing a net", "delete the indicator and leave nothing
dangling", "document the sheet") and several macro/power tasks need more: a
part's position or value, a part's absence, a no-connect flag, a real power
symbol on a rail, an annotation object, the exact net diff the edit must
report. Each of those is one FACT here, checked against the same public
surfaces an agent would read back (``akcli read --json``, ``akcli nets
--json``, ``akcli check --json``, ``akcli plan --json``) — plus the applied
``.kicad_sch`` itself for annotation objects, which no JSON surface lists.

The file is a JSON object; every key is optional, unknown keys are an ERROR
(a typo'd fact must never silently pass)::

    {
      "components": {"R1": {"value": "2.2k", "footprint": "Lib:FP",
                            "x_mil": 4500, "y_mil": 2450,
                            "rotation": 180, "mirror": "x",
                            "lib_id": "Device:R",
                            "parameters": {"MPN": "X"}}},
      "absent": ["D1"],
      "no_connects": ["J1.2"],
      "power_ports": ["GND"],
      "pwr_flags": ["VIN"],
      "texts": ["DIVIDER"],
      "text_boxes": ["line one\\nline two"],
      "rectangles": [{"encloses": ["R1", "R2"], "excludes": ["C1"]}],
      "title_block": {"title": "T", "rev": "B", "comment1": "c"},
      "forbid_findings": ["NET_LABEL_UNATTACHED"],
      "net_diff": {"equivalent": false, "risk": true, "lines": ["! MOVE ..."]}
    }

Semantics (one check per listed item unless noted):

* ``components`` — per designator, every listed field must match the placed
  part (coordinates within half a mil; ``parameters`` is a subset match).
* ``absent`` — the designator must not be placed.
* ``no_connects`` — the pin's tip must carry a no-connect flag.
* ``power_ports`` / ``pwr_flags`` — the NAMED net must contain at least one
  real power-symbol pin / ``PWR_FLAG`` pin (a same-name label alone fails).
* ``texts`` / ``text_boxes`` — a top-level text / text box with exactly this
  content must exist.
* ``rectangles`` — a top-level rectangle must contain every pin tip and the
  origin of each ``encloses`` part and the origin of no ``excludes`` part.
* ``title_block`` — per field (``title``/``date``/``rev``/``company``/
  ``comment1``..``comment9``), the title block must hold exactly this value.
* ``forbid_findings`` — ONE check: ``akcli check --integrity --nets`` must
  report none of these finding codes.
* ``net_diff`` — ONE check: the ``net_diff`` block ``akcli plan --json``
  reports for the candidate must equal every listed key (``lines`` is the
  exact "Net changes" block). ``"risk": true`` declares an INTENDED
  split/merge/move on a named net, so the harness applies that task without
  ``--strict-nets`` — the exact post-edit netlist is still enforced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, NamedTuple

FACTS_FILE = "expected_facts.json"

_COMPONENT_FIELDS = frozenset({"x_mil", "y_mil", "rotation", "mirror", "value",
                               "footprint", "lib_id", "parameters"})
_RECT_FIELDS = frozenset({"encloses", "excludes"})
_TITLE_FIELDS = frozenset({"title", "date", "rev", "company",
                           *(f"comment{i}" for i in range(1, 10))})
_NET_DIFF_FIELDS = frozenset({"equivalent", "risk", "lines"})
_LIST_KINDS = ("absent", "no_connects", "power_ports", "pwr_flags", "texts",
               "text_boxes", "forbid_findings")
FACT_KINDS = frozenset({"components", "rectangles", "title_block", "net_diff",
                        *_LIST_KINDS})

_COORD_TOL_MIL = 0.5
_MM_PER_MIL = 0.0254


class Check(NamedTuple):
    """One scored fact: a stable name, the verdict, and why it failed."""

    name: str
    ok: bool
    detail: str = ""


class FactsError(ValueError):
    """``expected_facts.json`` itself is malformed (a harness bug, not a miss)."""


def _str_list(kind: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v for v in value):
        raise FactsError(f"{kind}: expected a list of non-empty strings")
    return value


def _unknown(kind: str, keys: object, allowed: frozenset[str]) -> None:
    if not isinstance(keys, dict):
        raise FactsError(f"{kind}: expected an object")
    extra = sorted(set(keys) - allowed)
    if extra:
        raise FactsError(f"{kind}: unknown field(s) {extra} "
                         f"(allowed: {sorted(allowed)})")


def validate(facts: object) -> dict:
    """Shape-check a facts document; unknown kinds/fields fail closed."""
    if not isinstance(facts, dict):
        raise FactsError("expected_facts.json must be a JSON object")
    _unknown("expected_facts", facts, FACT_KINDS)
    for kind in _LIST_KINDS:
        if kind in facts:
            _str_list(kind, facts[kind])
    comps = facts.get("components", {})
    if not isinstance(comps, dict):
        raise FactsError("components: expected an object keyed by designator")
    for ref, want in comps.items():
        _unknown(f"components.{ref}", want, _COMPONENT_FIELDS)
        if not want:
            raise FactsError(f"components.{ref}: no field to check")
    rects = facts.get("rectangles", [])
    if not isinstance(rects, list):
        raise FactsError("rectangles: expected a list")
    for i, want in enumerate(rects):
        _unknown(f"rectangles[{i}]", want, _RECT_FIELDS)
        _str_list(f"rectangles[{i}].encloses", want.get("encloses"))
        _str_list(f"rectangles[{i}].excludes", want.get("excludes", []))
    if "title_block" in facts:
        _unknown("title_block", facts["title_block"], _TITLE_FIELDS)
    if "net_diff" in facts:
        _unknown("net_diff", facts["net_diff"], _NET_DIFF_FIELDS)
        if not facts["net_diff"]:
            raise FactsError("net_diff: no field to check")
    return facts


def load(task_dir: Path) -> dict:
    """A task's facts document (``{}`` when it ships none)."""
    path = task_dir / FACTS_FILE
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactsError(f"{path.name}: not valid JSON ({exc})") from None
    return validate(doc)


def wants_plan(facts: dict) -> bool:
    """Does this task assert the reported net diff (needs ``plan --json``)?"""
    return "net_diff" in facts


def allows_risk(facts: dict) -> bool:
    """True when the ground truth declares an INTENDED named-net change."""
    return facts.get("net_diff", {}).get("risk") is True


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def _split_pin(pin: str) -> tuple[str, str]:
    ref, _, num = pin.rpartition(".")
    return ref, num


def _near(a: object, b: object) -> bool:
    return (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and abs(float(a) - float(b)) <= _COORD_TOL_MIL)


def _component_miss(got: dict, want: dict) -> list[str]:
    miss: list[str] = []
    for key, exp in want.items():
        if key in ("x_mil", "y_mil"):
            ok = _near(got.get(key), exp)
            have: object = got.get(key)
        elif key == "rotation":
            have = got.get("rotation")
            ok = isinstance(have, (int, float)) and int(have) % 360 == exp
        elif key == "lib_id":
            have = got.get("library_ref")
            ok = have == exp
        elif key == "parameters":
            params = got.get("parameters") or {}
            have = {k: params.get(k) for k in exp}
            ok = have == exp
        else:                                   # value / footprint / mirror
            have = got.get(key)
            ok = have == exp
        if not ok:
            miss.append(f"{key}: expected {exp!r}, got {have!r}")
    return miss


def _annotations(sheet: Path) -> dict:
    """Top-level annotation objects of the applied sheet (mils / raw text).

    No ``--json`` surface lists free texts, text boxes or graphic rectangles,
    and the KiCad reader does not surface the title block, so these are read
    from the file through akcli's own lossless S-expression parser.
    """
    from akcli.sexpr import parse  # lazy: run_eval puts src/ on sys.path

    doc = parse(sheet.read_text(encoding="utf-8"))

    def num(node, idx: int) -> float:
        return float(node[idx].value) / _MM_PER_MIL

    out: dict = {"texts": [], "text_boxes": [], "rectangles": [], "title": {}}
    for node in doc:
        if not node.is_list:
            continue
        if node.tag == "text" and len(node) > 1:
            out["texts"].append(node[1].value)
        elif node.tag == "text_box" and len(node) > 1:
            out["text_boxes"].append(node[1].value)
        elif node.tag == "rectangle":
            start, end = node.find("start"), node.find("end")
            if start is not None and end is not None:
                xs = sorted((num(start, 1), num(end, 1)))
                ys = sorted((num(start, 2), num(end, 2)))
                out["rectangles"].append((xs[0], ys[0], xs[1], ys[1]))
        elif node.tag == "title_block":
            for field in node:
                if not field.is_list or len(field) < 2:
                    continue
                if field.tag == "comment" and len(field) > 2:
                    out["title"][f"comment{field[1].value}"] = field[2].value
                elif field.tag != "comment":
                    out["title"][field.tag] = field[1].value
    return out


def _inside(box: tuple[float, float, float, float], x: float, y: float) -> bool:
    x0, y0, x1, y1 = box
    return (x0 - _COORD_TOL_MIL <= x <= x1 + _COORD_TOL_MIL
            and y0 - _COORD_TOL_MIL <= y <= y1 + _COORD_TOL_MIL)


def _rect_ok(box, want: dict, comps: dict) -> bool:
    for ref in want["encloses"]:
        comp = comps.get(ref)
        if comp is None:
            return False
        pts = [(comp["x_mil"], comp["y_mil"])]
        pts += [(p["x_mil"], p["y_mil"]) for p in comp.get("pins", [])]
        if not all(_inside(box, x, y) for x, y in pts):
            return False
    for ref in want.get("excludes", []):
        comp = comps.get(ref)
        if comp is not None and _inside(box, comp["x_mil"], comp["y_mil"]):
            return False
    return True


def evaluate(facts: dict, *, sheet: Path,
             run_json: Callable[[list[str]], dict],
             nets: dict[str, frozenset[str]],
             plan_diff: dict | None) -> list[Check]:
    """Check every fact against the applied ``sheet``.

    ``run_json(argv)`` runs akcli and returns its parsed ``--json`` document;
    ``nets`` maps each NAMED net to its full membership, power-symbol pins
    included; ``plan_diff`` is the ``net_diff`` block ``plan --json`` reported
    for the candidate (``None`` when the plan carried none).
    """
    checks: list[Check] = []
    if not facts:
        return checks

    need_read = any(k in facts for k in (
        "components", "absent", "no_connects", "power_ports", "pwr_flags",
        "rectangles"))
    doc = run_json(["read", str(sheet), "--json"]) if need_read else {}
    comps = {c["designator"]: c for c in doc.get("components", [])}

    for ref, want in facts.get("components", {}).items():
        got = comps.get(ref)
        if got is None:
            checks.append(Check(f"component {ref}", False, "not placed"))
            continue
        miss = _component_miss(got, want)
        checks.append(Check(f"component {ref}", not miss, "; ".join(miss)))

    for ref in facts.get("absent", []):
        checks.append(Check(f"absent {ref}", ref not in comps,
                            "still placed" if ref in comps else ""))

    nc_points = [tuple(p) for p in doc.get("no_erc_points", [])]
    for pin in facts.get("no_connects", []):
        ref, num = _split_pin(pin)
        tip = next(((p["x_mil"], p["y_mil"])
                    for p in comps.get(ref, {}).get("pins", [])
                    if p["number"] == num), None)
        ok = tip is not None and any(
            _near(tip[0], x) and _near(tip[1], y) for x, y in nc_points)
        checks.append(Check(
            f"no_connect {pin}", ok,
            "" if ok else ("no such pin" if tip is None
                           else f"no no-connect flag at {list(tip)}")))

    flags = {r for r, c in comps.items()
             if str(c.get("library_ref", "")).rpartition(":")[2] == "PWR_FLAG"}
    ports = {r for r in comps if r.startswith("#")} - flags
    for kind, refs, what in (("power_ports", ports, "power symbol"),
                             ("pwr_flags", flags, "PWR_FLAG")):
        for net in facts.get(kind, []):
            members = nets.get(net)
            ok = members is not None and any(
                _split_pin(m)[0] in refs for m in members)
            checks.append(Check(
                f"{kind[:-1]} on {net}", ok,
                "" if ok else (f"no named net {net!r}" if members is None
                               else f"no {what} pin on net {net!r}")))

    if any(k in facts for k in ("texts", "text_boxes", "rectangles",
                                "title_block")):
        ann = _annotations(sheet)
        for kind, label in (("texts", "text"), ("text_boxes", "text_box")):
            for text in facts.get(kind, []):
                ok = text in ann[kind]
                checks.append(Check(
                    f"{label} {text!r}", ok,
                    "" if ok else f"not found among {ann[kind]!r}"))
        for i, want in enumerate(facts.get("rectangles", [])):
            ok = any(_rect_ok(box, want, comps) for box in ann["rectangles"])
            checks.append(Check(
                f"rectangle[{i}] enclosing {want['encloses']}", ok,
                "" if ok else f"no such rectangle among "
                              f"{[list(b) for b in ann['rectangles']]}"))
        for field, exp in facts.get("title_block", {}).items():
            have = ann["title"].get(field)
            checks.append(Check(
                f"title_block.{field}", have == exp,
                "" if have == exp else f"expected {exp!r}, got {have!r}"))

    if "forbid_findings" in facts:
        report = run_json(["check", str(sheet), "--integrity", "--nets",
                           "--json", "--fail-on", "never"])
        bad = sorted({f"{f.get('code')}: {f.get('message')}"
                      for f in report.get("findings", [])
                      if f.get("code") in set(facts["forbid_findings"])})
        checks.append(Check("forbid_findings", not bad, "; ".join(bad)))

    if "net_diff" in facts:
        want = facts["net_diff"]
        if plan_diff is None:
            checks.append(Check("net_diff", False, "plan reported no net diff"))
        else:
            miss = [f"{k}: expected {v!r}, got {plan_diff.get(k)!r}"
                    for k, v in want.items() if plan_diff.get(k) != v]
            checks.append(Check("net_diff", not miss, "; ".join(miss)))
    return checks
