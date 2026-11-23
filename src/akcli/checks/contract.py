"""Design contracts: topology/semantic rules ERC cannot express (SPEC: Phase 3).

A contract file is TOML (stdlib ``tomllib``; zero dependencies) holding
``[[contract]]`` rules with datasheet-backed semantics:

.. code-block:: toml

    protocol_version = 1

    [[contract]]
    id = "reg-feedback-node"
    evidence = [
      "ACME-REG datasheet, Table 7",
      { source = "ACME-REG datasheet", page = 12, url = "https://example.com/reg.pdf" },
    ]
    require = [ { pin = "U1.FB2", net = "VFB2" } ]
    forbid  = [ { pin = "U1.FB1", net = "RAIL_A" } ]

    [[contract]]
    id = "usb-c-flip-pair"
    require_same_net = [ ["J1.A6", "J1.B6"], ["J1.A7", "J1.B7"] ]
    forbid_same_net  = [ ["J1.A2", "J1.B11"] ]

    [[contract]]
    id = "shunt-value"
    component = "R7"
    value = "150k"

    [[contract]]
    id = "shunt-value-window"
    component = "R7"
    value = { nominal = "150k", tolerance = "1%" }

    [[contract]]
    id = "bulk-cap-window"
    component = "C3"
    value = { min = "100n", max = "1u" }

    [[contract]]
    id = "gpio3-unused"
    nc = ["U1.GPIO3"]

    [[contract]]
    id = "thermal-via-exception"
    waived = true
    reason = "exposed-pad thermal vias approved"
    owner = "hw-lead"
    expires = "2027-01-01"

Rule vocabulary per contract: ``require`` / ``forbid`` (pin-on-net),
``require_same_net`` / ``forbid_same_net`` (pin-pair topology), ``component`` +
``value`` and ``nc`` (pin must not join a multi-pin net). ``severity`` defaults
to error.

``value`` is a string (exact, case/space-insensitive), a list of accepted
strings, or a range table — ``{ nominal, tolerance }`` (passes
when ``|actual - nominal| <= tolerance * nominal``; ``tolerance`` is a
percentage string) or ``{ min, max }`` (inclusive, either bound optional).
The range forms compare NUMBERS: bounds and the component's Value are read as
engineering notation (``150k`` / ``4k7`` / ``100n`` / ``4R7`` / ``1e-7``, with
an optional trailing unit — ``10uF``, ``4k7Ω``, ``100 ohm``); the component's
Value may also carry trailing BOM qualifiers (``150k 1%``, ``100n X7R``).
Nothing else is parsed: free text FAILS the range rule with a "cannot be
parsed" message — it never passes.

``evidence`` entries are strings or ``{ source, page, url }`` tables
(``source`` required), rendered into the finding message in file order.

A rule with ``waived = true`` is an approved exception and must carry a
non-empty ``owner`` and ``reason`` (``expires`` is optional).

The report distinguishes three outcomes, never conflating them: PASS
(``CONTRACT_PASS``, info), FAIL (``CONTRACT_*`` at the rule's severity) and
SKIPPED-BY-EXCEPTION (``CONTRACT_WAIVED``, note; an expired exception raises
``CONTRACT_EXCEPTION_EXPIRED`` instead of silently passing).

Pin names: ``REF.PIN`` matches the pin *number* first, then the pin *name*
(e.g. ``U1.FB2``), so datasheet-named rules read naturally.
"""

from __future__ import annotations

from .. import contract as _contract

import datetime as _dt
import math
import os
import re

from ..errors import fail
from ..model import Schematic
from ..report import Finding, Severity, anchor

__all__ = ["load", "run"]

PROTOCOL_VERSION = _contract.PROTOCOL_VERSIONS["contract-check"]

_SEVERITIES = {
    "info": Severity.INFO, "note": Severity.NOTE,
    "warning": Severity.WARNING, "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}

# --------------------------------------------------------------------------- #
# value ranges (engineering notation) + structured evidence
# --------------------------------------------------------------------------- #
_RANGE_KEYS = ("nominal", "tolerance", "min", "max")
_EVIDENCE_KEYS = ("source", "page", "url")
# Unit a Value carries after the number ("10uF" -> "10u", "4k7Ω" -> "4k7";
# both the Greek capital omega and the ohm sign). A lower-case f/h counts as
# the unit only after a prefix letter: a bare "100f" is femto in SPICE
# notation, so it is refused rather than read as 100 F.
_UNIT_TAIL_RX = re.compile(
    r"\s*(?:(?<=[A-Za-z\u00b5\u03bc])[FfHh]|[FH]|(?i:ohms?)|[\u03a9\u2126])\s*$")
_PERCENT_RX = re.compile(r"^\s*±?\s*(\d+(?:\.\d+)?|\.\d+)\s*%\s*$")
# Relative float slack so a value sitting exactly ON a bound passes: "1.02p"
# is within 2 % of "1p", but 1.02e-12 - 1e-12 is not bit-equal to 0.02e-12.
_REL_EPS = 1e-9
# No "Ω" in this hint: it lands in finding messages, and a redirected Windows
# stdout (cp1252) cannot encode Greek.
_SPELLINGS = ("engineering notation such as '150k' / '4k7' / '100n' / '4R7' "
              "/ '1e-7', optionally with a unit ('10uF' / '100 ohm') and "
              "trailing qualifiers ('150k 1%' / '100n X7R')")


def _parse_value(text: object, *, qualifiers: bool = False) -> float | None:
    """Engineering-notation value of a Value field or range bound, else ``None``.

    The number crunching is :func:`akcli.calc.si.parse_value` — the parser
    the calculators use; on top of it only a trailing unit is dropped. With
    ``qualifiers`` (a component's Value, never a range bound) a BOM-style
    spelling is read by its leading token (``150k 1%``, ``100n X7R``) through
    the same grammar the review detectors use. Free text, ``nan``/``inf`` and
    booleans are all ``None``: a range check never guesses.
    """
    # Lazy: importing akcli.calc registers every calculator, which a contract
    # without a range rule never needs.
    from ..calc.si import leading_value_token
    from ..calc.si import parse_value as si_parse_value

    if qualifiers and isinstance(text, str):
        text = leading_value_token(text) or text

    raw: str | float | int
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        raw = text
    elif isinstance(text, str):
        raw = _UNIT_TAIL_RX.sub("", text.strip())
    else:
        return None
    try:
        return si_parse_value(raw)
    except ValueError:                    # CalcError: not a value / not finite
        return None


def _bound(spec: dict, key: str) -> float:
    val = _parse_value(spec[key])
    if val is None:
        raise ValueError(f"{key} {spec[key]!r} is not a value "
                         f"(accepted: {_SPELLINGS})")
    return val


def _range(spec: dict) -> tuple[float | None, float | None, str]:
    """Resolve a ``value`` range table to inclusive ``(lo, hi)`` + its wording.

    Raises ``ValueError`` naming the defect when the table is malformed.
    """
    unknown = sorted(str(k) for k in spec if k not in _RANGE_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)} (expected "
                         "nominal + tolerance, or min / max)")
    if "nominal" in spec or "tolerance" in spec:
        if "min" in spec or "max" in spec:
            raise ValueError("nominal/tolerance and min/max are separate "
                             "forms — use one")
        if "nominal" not in spec or "tolerance" not in spec:
            raise ValueError("nominal and tolerance are required together")
        nominal = _bound(spec, "nominal")
        tol = spec["tolerance"]
        m = _PERCENT_RX.match(tol) if isinstance(tol, str) else None
        if m is None:
            raise ValueError(f"tolerance {tol!r} is not a percentage "
                             "(write it as e.g. \"1%\")")
        pct = float(m.group(1))
        span = pct / 100.0 * abs(nominal)
        return nominal - span, nominal + span, f"{spec['nominal']} ±{pct:g}%"
    if "min" not in spec and "max" not in spec:
        raise ValueError("empty range (expected nominal + tolerance, or "
                         "min / max)")
    lo = _bound(spec, "min") if "min" in spec else None
    hi = _bound(spec, "max") if "max" in spec else None
    if lo is not None and hi is not None:
        if lo > hi:
            raise ValueError(f"min {spec['min']!r} is greater than max "
                             f"{spec['max']!r}")
        return lo, hi, f"{spec['min']} to {spec['max']}"
    if lo is not None:
        return lo, None, f"at least {spec['min']}"
    return None, hi, f"at most {spec['max']}"


def _le(a: float, b: float) -> bool:
    """``a <= b``, inclusive of float noise on the bound itself."""
    return a <= b or math.isclose(a, b, rel_tol=_REL_EPS)


def _check_range(ref: str, actual: str | None, spec: dict) -> list[str]:
    """Range form of a value rule; an unparseable Value fails, never passes."""
    try:
        lo, hi, want = _range(spec)
    except ValueError as exc:             # only for a doc that skipped load()
        return [f"{ref}: invalid value range: {exc}"]
    got = _parse_value(actual, qualifiers=True)
    if got is None:
        return [f"{ref}: value {actual!r} cannot be parsed as a number, so "
                f"it cannot be checked against {want} (accepted: "
                f"{_SPELLINGS})"]
    if (lo is not None and not _le(lo, got)) or (
            hi is not None and not _le(got, hi)):
        return [f"{ref}: value {actual!r}, expected {want}"]
    return []


def _evidence_defect(item: object) -> str | None:
    """Why one ``evidence`` entry is malformed, or ``None`` when it is fine."""
    if isinstance(item, str):
        return None
    if not isinstance(item, dict):
        return f"entry {item!r} is neither a string nor a table"
    unknown = sorted(str(k) for k in item if k not in _EVIDENCE_KEYS)
    if unknown:
        return (f"unknown key(s) {', '.join(unknown)} (expected source, "
                "page, url)")
    source = item.get("source")
    if not isinstance(source, str) or not source.strip():
        return "a table entry needs a non-empty source"
    page = item.get("page")
    if "page" in item and (isinstance(page, bool) or not isinstance(page, int)
                           or page < 1):
        return f"page {page!r} is not a positive integer"
    url = item.get("url")
    if "url" in item and (not isinstance(url, str) or not url.strip()):
        return f"url {url!r} is not a non-empty string"
    return None


def _evidence_text(item: object) -> str:
    """One ``evidence`` entry as message text; a table is ``source p.N <url>``."""
    if not isinstance(item, dict):
        return str(item)
    text = str(item.get("source", ""))
    if item.get("page") is not None:
        text += f" p.{item['page']}"
    if item.get("url"):
        text += f" <{item['url']}>"
    return text


def _nonempty(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def load(path: os.PathLike | str) -> dict:
    """Load + structurally validate a contract TOML file."""
    import tomllib
    from pathlib import Path

    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail("BAD_CONFIG", f"contract file {path}: {exc}")
    pv = doc.get("protocol_version", PROTOCOL_VERSION)
    if isinstance(pv, int) and pv > PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"contract protocol_version {pv} > supported {PROTOCOL_VERSION}")
    rules = doc.get("contract")
    if not isinstance(rules, list) or not rules:
        fail("BAD_CONFIG", f"contract file {path}: no [[contract]] rules")
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            fail("BAD_CONFIG", f"contract file {path}: rule #{i} is not a table")
        rid = rule.get("id")
        if not rid or not isinstance(rid, str):
            fail("BAD_CONFIG", f"contract file {path}: rule #{i} has no id")
        if rid in seen:
            fail("BAD_CONFIG", f"contract file {path}: duplicate id {rid!r}")
        seen.add(rid)
        sev = rule.get("severity", "error")
        if sev not in _SEVERITIES:
            fail("BAD_CONFIG",
                 f"contract {rid!r}: unknown severity {sev!r} "
                 f"(expected {'/'.join(_SEVERITIES)})")
        if rule.get("waived") and not (_nonempty(rule.get("owner"))
                                       and _nonempty(rule.get("reason"))):
            fail("BAD_CONFIG",
                 f"contract {rid!r}: a waived rule needs owner AND reason")
        evidence = rule.get("evidence", [])
        if not isinstance(evidence, list):
            fail("BAD_CONFIG",
                 f"contract {rid!r}: evidence must be a list of strings "
                 "and/or { source, page, url } tables")
        for item in evidence:
            defect = _evidence_defect(item)
            if defect:
                fail("BAD_CONFIG", f"contract {rid!r}: evidence: {defect}")
        value = rule.get("value")
        if ("value" in rule) != ("component" in rule):
            # half a value rule checks nothing and would report PASS
            fail("BAD_CONFIG",
                 f"contract {rid!r}: a value rule needs both component "
                 "and value")
        if isinstance(value, dict):
            try:
                _range(value)
            except ValueError as exc:
                fail("BAD_CONFIG", f"contract {rid!r}: value: {exc}")
        elif isinstance(value, list) and any(isinstance(v, dict) for v in value):
            fail("BAD_CONFIG",
                 f"contract {rid!r}: value: a range table cannot be a list "
                 "element (a list holds accepted exact values)")
    return doc


class _Netview:
    """Pin/net lookups over a schematic, with pin-NAME fallback."""

    def __init__(self, sch: Schematic) -> None:
        self.sch = sch
        self.by_ref = {c.designator: c for c in sch.components}
        # (ref, pin_number) -> net index
        self.net_of: dict[tuple[str, str], int] = {}
        for i, net in enumerate(sch.nets):
            for m in net.members:
                self.net_of[m] = i
        # net name/alias -> index
        self.net_by_name: dict[str, int] = {}
        for i, net in enumerate(sch.nets):
            if net.name:
                self.net_by_name.setdefault(net.name, i)
            for a in net.aliases:
                self.net_by_name.setdefault(a, i)

    def pin_key(self, spec: str) -> tuple[str, str] | None:
        """Resolve ``REF.PIN`` (number first, then name) to a (ref, number) key."""
        if "." not in spec:
            return None
        ref, pin = spec.split(".", 1)
        comp = self.by_ref.get(ref)
        if comp is None:
            return None
        for p in comp.pins:
            if p.number == pin:
                return (ref, p.number)
        matches = [p for p in comp.pins if (p.name or "") == pin]
        if len(matches) == 1:
            return (ref, matches[0].number)
        return None

    def net_index(self, key: tuple[str, str]) -> int | None:
        return self.net_of.get(key)


def _norm_value(v: str) -> str:
    return "".join(str(v).split()).lower()


def _check_rule(rule: dict, view: _Netview) -> list[str]:
    """Evaluate one contract rule; returns failure messages (empty = pass)."""
    fails: list[str] = []
    sch = view.sch

    def _resolve(spec: str) -> tuple[str, str] | None:
        key = view.pin_key(spec)
        if key is None:
            fails.append(f"pin {spec!r} not found on the schematic")
        return key

    for item in rule.get("require", []):
        key = _resolve(str(item.get("pin", "")))
        if key is None:
            continue
        want = str(item.get("net", ""))
        idx = view.net_index(key)
        actual = (sch.nets[idx].name if idx is not None else None)
        want_idx = view.net_by_name.get(want)
        if want_idx is None:
            fails.append(f"required net {want!r} does not exist")
        elif idx != want_idx:
            fails.append(f"{item.get('pin')}: on {actual or '(no net)'}, "
                         f"required {want!r}")

    for item in rule.get("forbid", []):
        key = _resolve(str(item.get("pin", "")))
        if key is None:
            continue
        banned = str(item.get("net", ""))
        idx = view.net_index(key)
        if idx is not None and view.net_by_name.get(banned) == idx:
            fails.append(f"{item.get('pin')}: is on forbidden net {banned!r}")

    for pair in rule.get("require_same_net", []):
        keys = [_resolve(str(p)) for p in pair[:2]]
        if None in keys or len(pair) < 2:
            continue
        ia, ib = view.net_index(keys[0]), view.net_index(keys[1])
        if ia is None or ib is None or ia != ib:
            na = sch.nets[ia].name if ia is not None else "(no net)"
            nb = sch.nets[ib].name if ib is not None else "(no net)"
            fails.append(f"{pair[0]} and {pair[1]} must share a net "
                         f"(got {na!r} vs {nb!r})")
        elif len(pair) >= 3 and sch.nets[ia].name != pair[2]:
            fails.append(f"{pair[0]}/{pair[1]}: net is "
                         f"{sch.nets[ia].name!r}, expected {pair[2]!r}")

    for pair in rule.get("forbid_same_net", []):
        keys = [_resolve(str(p)) for p in pair[:2]]
        if None in keys or len(pair) < 2:
            continue
        ia, ib = view.net_index(keys[0]), view.net_index(keys[1])
        if ia is not None and ia == ib:
            fails.append(f"{pair[0]} and {pair[1]} must NOT share a net "
                         f"(both on {sch.nets[ia].name or 'an unnamed net'!r})")

    if "component" in rule and "value" in rule:
        ref = str(rule["component"])
        comp = view.by_ref.get(ref)
        if comp is None:
            fails.append(f"component {ref!r} not found")
        elif isinstance(rule["value"], dict):
            fails.extend(_check_range(ref, comp.value, rule["value"]))
        else:
            accepted = rule["value"]
            if not isinstance(accepted, list):
                accepted = [accepted]
            got = _norm_value(comp.value or "")
            if got not in {_norm_value(v) for v in accepted}:
                fails.append(f"{ref}: value {comp.value!r}, expected "
                             + " or ".join(repr(str(v)) for v in accepted))

    for spec in rule.get("nc", []):
        key = _resolve(str(spec))
        if key is None:
            continue
        idx = view.net_index(key)
        if idx is not None and len(sch.nets[idx].members) > 1:
            fails.append(f"{spec}: must be unconnected but joins "
                         f"{sch.nets[idx].name or 'an unnamed net'} "
                         f"({len(sch.nets[idx].members)} pins)")
    return fails


def run(sch: Schematic, doc: dict, *, today: _dt.date | None = None) -> list[Finding]:
    """Evaluate every contract; PASS/FAIL/WAIVED are all explicit findings."""
    view = _Netview(sch)
    findings: list[Finding] = []
    today = today or _dt.date.today()

    for rule in doc.get("contract", []):
        rid = str(rule.get("id"))
        evidence = rule.get("evidence") or []
        suffix = (f"  [evidence: {'; '.join(map(_evidence_text, evidence))}]"
                  if evidence else "")

        if rule.get("waived"):
            expires = rule.get("expires")
            expired = False
            if expires:
                try:
                    expired = _dt.date.fromisoformat(str(expires)) < today
                except ValueError:
                    expired = True
            if expired:
                findings.append(Finding(
                    "CONTRACT_EXCEPTION_EXPIRED", Severity.WARNING,
                    f"{rid}: exception expired ({expires}); owner "
                    f"{rule.get('owner', '?')} must re-review — "
                    f"{rule.get('reason', 'no reason recorded')}{suffix}"))
            else:
                findings.append(Finding(
                    "CONTRACT_WAIVED", Severity.NOTE,
                    f"{rid}: skipped by approved exception "
                    f"(owner {rule.get('owner', '?')}"
                    + (f", expires {expires}" if expires else "")
                    + f") — {rule.get('reason', 'no reason recorded')}{suffix}"))
            continue

        fails = _check_rule(rule, view)
        if fails:
            sev = _SEVERITIES[rule.get("severity", "error")]
            for msg in fails:
                findings.append(Finding(
                    "CONTRACT_FAIL", sev, f"{rid}: {msg}{suffix}",
                    anchors=[anchor("net", rid)]))
        else:
            findings.append(Finding(
                "CONTRACT_PASS", Severity.INFO, f"{rid}: holds{suffix}"))
    return findings
