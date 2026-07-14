"""Design contracts: topology/semantic rules ERC cannot express (SPEC: Phase 3).

A contract file is TOML (stdlib ``tomllib``; zero dependencies) holding
``[[contract]]`` rules with datasheet-backed semantics:

.. code-block:: toml

    protocol_version = 1

    [[contract]]
    id = "reg-feedback-node"
    evidence = ["ACME-REG datasheet, Table 7"]
    require = [ { pin = "U1.FB2", net = "VFB2" } ]
    forbid  = [ { pin = "U1.FB1", net = "V3V3" } ]

    [[contract]]
    id = "usb-c-flip-pair"
    require_same_net = [ ["J1.A6", "J1.B6"], ["J1.A7", "J1.B7"] ]
    forbid_same_net  = [ ["J1.A2", "J1.B11"] ]

    [[contract]]
    id = "shunt-value"
    component = "R7"
    value = "150k"

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
``value`` (exact, case/space-insensitive; or a list of accepted values), and
``nc`` (pin must not join a multi-pin net). ``severity`` defaults to error.

The report distinguishes three outcomes, never conflating them: PASS
(``CONTRACT_PASS``, info), FAIL (``CONTRACT_*`` at the rule's severity) and
SKIPPED-BY-EXCEPTION (``CONTRACT_WAIVED``, note; an expired exception raises
``CONTRACT_EXCEPTION_EXPIRED`` instead of silently passing).

Pin names: ``REF.PIN`` matches the pin *number* first, then the pin *name*
(e.g. ``U1.FB2``), so datasheet-named rules read naturally.
"""

from __future__ import annotations

import datetime as _dt
import os

from ..errors import fail
from ..model import Schematic
from ..report import Finding, Severity, anchor

__all__ = ["load", "run"]

PROTOCOL_VERSION = 1

_SEVERITIES = {
    "info": Severity.INFO, "note": Severity.NOTE,
    "warning": Severity.WARNING, "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


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
        suffix = f"  [evidence: {'; '.join(map(str, evidence))}]" if evidence else ""

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
