"""Design-intent assertions: assert the netlist the designer MEANT.

In a real design session the same hand-rolled question is re-asked after every
edit: "is U1.4 still on SWCLK, and did nothing short SWCLK into SWDIO?". This
module makes that a first-class, file-driven check.

Intent file (JSON)::

    {"protocol_version": 1,
     "mode": "exact",                    # or "subset"; default "exact"
     "nets": {
        "SWCLK": ["U1.4", "J2.2"],       # plain list -> uses document mode
        "GND": {"members": ["R*.2", "U?.1"], "mode": "subset"}
     }}

A net value is EITHER the classic plain list of ``"REF.PIN"`` strings, OR an
object ``{"members": [...], "mode": "exact"|"subset"}`` that overrides the
document-level ``mode`` for that one net (``protocol_version`` stays 1 — this
is an additive format upgrade, old plain-list documents keep working
unchanged).

Member strings are ``"REF.PIN"`` split on the FIRST dot — designators never
contain dots, pin numbers may (``"U1.P0.25"`` parses as pin ``P0.25``). The
REF part may be an ``fnmatch`` wildcard (``"R*.1"``, ``"U?.3"``) — the pin
part is always literal. A wildcard member is satisfied when AT LEAST ONE
actual schematic pin matches both the REF pattern and the literal pin number;
if none match at all it is reported as ``INTENT_MISSING_MEMBER``, naming the
pattern itself (never an expansion). **Wildcards are ignored when computing
EXTRA members in exact mode**: only the net's literal (non-wildcard) members
are subtracted from the matched actual net's membership, so an actual pin
that is present ONLY because it happens to match a wildcard pattern still
shows up as an extra member. This keeps ``exact`` honest — a wildcard is a
existence assertion ("at least one R matches"), not a closed-world
enumeration of every R that may be on the net.

``run(sch, spec)`` matches each intent net onto the actual net (``sch.nets``,
the shared netbuild output) containing the MOST of its listed pins — never by
display name, because auto-names churn and designers rename freely. It reports:

* ``INTENT_PIN_UNKNOWN``    (ERROR) — a listed literal REF.PIN does not exist
  in the schematic (wildcards are exempt — they may legitimately match zero
  or many pins)
* ``INTENT_NET_NOT_FOUND``  (ERROR) — no actual net contains any listed pin
* ``INTENT_MISSING_MEMBER`` (ERROR) — an intent pin (or wildcard pattern) is
  absent from the matched net
* ``INTENT_EXTRA_MEMBER``   (ERROR, exact mode only) — the matched net carries
  pins the intent's literal members omit; ``subset`` mode asserts containment
  and skips this
* ``INTENT_NETS_SHORTED``   (ERROR) — two intent nets resolve to the SAME actual net

``snapshot(sch)`` emits a valid intent document from the current netlist (named
nets only by default), enabling the snapshot -> edit -> assert workflow.
Snapshot output always uses the plain-list net-value form (no wildcards, no
per-net mode overrides — those are authored by hand).

``load`` raises ``BAD_CONFIG`` naming the offending entry for shape errors and
``PROTOCOL_MISMATCH`` for a wrong ``protocol_version`` (mirrors ops.py).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AkcliError, fail
from ..model import Net, Pin, PinRef, Schematic
from ..report import Finding, Severity, anchor

PROTOCOL_VERSION = 1
MODES = ("exact", "subset")

INTENT_PIN_UNKNOWN = "INTENT_PIN_UNKNOWN"
INTENT_NET_NOT_FOUND = "INTENT_NET_NOT_FOUND"
INTENT_MISSING_MEMBER = "INTENT_MISSING_MEMBER"
INTENT_EXTRA_MEMBER = "INTENT_EXTRA_MEMBER"
INTENT_NETS_SHORTED = "INTENT_NETS_SHORTED"

_TOP_KEYS = frozenset({"protocol_version", "mode", "nets"})
_NET_OBJECT_KEYS = frozenset({"members", "mode"})
_WILDCARD_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class Member:
    """One intent-net member: a concrete pin, or an fnmatch wildcard on REF.

    ``is_wildcard`` is derived automatically from ``ref`` (True when it
    contains any of ``* ? [``) — the pin part is never a wildcard.
    """

    ref: str
    pin: str
    is_wildcard: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_wildcard",
                            any(c in self.ref for c in _WILDCARD_CHARS))

    @property
    def token(self) -> str:
        return f"{self.ref}.{self.pin}"


@dataclass
class NetSpec:
    """One intent net: its members plus its effective mode.

    ``mode`` is already resolved at load time — the net's own override if it
    supplied one via the object net-value form, else the document's mode.
    """

    members: list[Member] = field(default_factory=list)
    mode: str = "exact"


@dataclass
class IntentSpec:
    """A validated intent document: net name -> its NetSpec."""

    nets: dict[str, NetSpec] = field(default_factory=dict)
    mode: str = "exact"
    protocol_version: int = PROTOCOL_VERSION


# --------------------------------------------------------------------------- #
# load / validate
# --------------------------------------------------------------------------- #
def _parse_member(net_name: str, raw: object, where: str) -> Member:
    if not isinstance(raw, str):
        fail("BAD_CONFIG",
             f"{where}: net '{net_name}': member {raw!r} must be a string "
             "'REF.PIN' (e.g. 'U1.2' or 'R*.1')")
    token = raw.strip()
    ref, dot, pin = token.partition(".")
    if not dot or not ref or not pin:
        fail("BAD_CONFIG",
             f"{where}: net '{net_name}': member {raw!r} must be 'REF.PIN' "
             "(e.g. 'U1.2' or 'R*.1')")
    return Member(ref=ref, pin=pin)


def load(path: str | Path) -> IntentSpec:
    """Load and validate an intent JSON file (see module docstring for shape).

    Raises ``AkcliError('BAD_CONFIG', ...)`` naming the offending entry, or
    ``AkcliError('PROTOCOL_MISMATCH', ...)`` on a wrong ``protocol_version``.
    ``FileNotFoundError`` propagates (the CLI maps it to exit 4).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AkcliError("BAD_CONFIG", f"invalid intent JSON in {p}: {exc}") from exc

    where = str(p)
    if not isinstance(doc, dict):
        fail("BAD_CONFIG", f"{where}: intent root must be a JSON object")
    extra = set(doc) - _TOP_KEYS
    if extra:
        fail("BAD_CONFIG",
             f"{where}: unknown key(s): {', '.join(sorted(map(str, extra)))} "
             f"(expected {', '.join(sorted(_TOP_KEYS))})")

    pv = doc.get("protocol_version")
    if pv != PROTOCOL_VERSION:
        fail("PROTOCOL_MISMATCH",
             f"{where}: intent protocol_version {pv!r} != {PROTOCOL_VERSION}")

    mode = doc.get("mode", "exact")
    if mode not in MODES:
        fail("BAD_CONFIG",
             f"{where}: mode {mode!r} must be one of {', '.join(MODES)}")

    raw_nets = doc.get("nets")
    if not isinstance(raw_nets, dict):
        fail("BAD_CONFIG",
             f"{where}: 'nets' must be an object of "
             '{"NAME": ["REF.PIN", ...], ...}')

    nets: dict[str, NetSpec] = {}
    for name, value in raw_nets.items():
        name = str(name)
        if not name.strip():
            fail("BAD_CONFIG", f"{where}: net names must be non-empty")

        if isinstance(value, list):
            members_raw = value
            net_mode = mode
        elif isinstance(value, dict):
            extra_keys = set(value) - _NET_OBJECT_KEYS
            if extra_keys:
                fail("BAD_CONFIG",
                     f"{where}: net '{name}': unknown key(s) in object form: "
                     f"{', '.join(sorted(map(str, extra_keys)))} "
                     f"(expected {', '.join(sorted(_NET_OBJECT_KEYS))})")
            members_raw = value.get("members")
            net_mode = value.get("mode", mode)
            if net_mode not in MODES:
                fail("BAD_CONFIG",
                     f"{where}: net '{name}': mode {net_mode!r} must be one "
                     f"of {', '.join(MODES)}")
        else:
            fail("BAD_CONFIG",
                 f"{where}: net '{name}': value must be an array of "
                 '"REF.PIN" strings or an object '
                 '{"members": [...], "mode": "exact"|"subset"}')

        if not isinstance(members_raw, list) or not members_raw:
            fail("BAD_CONFIG",
                 f"{where}: net '{name}': members must be a non-empty array "
                 'of "REF.PIN" strings')

        seen: list[Member] = []
        seen_tokens: set[str] = set()
        for raw in members_raw:
            m = _parse_member(name, raw, where)
            if m.token not in seen_tokens:  # silently dedupe repeats within one net
                seen_tokens.add(m.token)
                seen.append(m)
        nets[name] = NetSpec(members=seen, mode=net_mode)

    return IntentSpec(nets=nets, mode=mode, protocol_version=pv)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _net_label(net: Net) -> str:
    return net.name if net.name else f"<unnamed {net.stable_id}>"


def _name_set(net: Net) -> set[str]:
    """Case-folded set of every name a net answers to (name + aliases + sources)."""
    names = [net.name, *net.aliases, *net.source_names]
    return {n.strip().upper() for n in names if n}


def _refs(members) -> list[str]:
    return [f"{d}.{p}" for d, p in members]


def _pin_index(sch: Schematic) -> dict[PinRef, Pin]:
    """Map every ``(designator, pin_number)`` to its :class:`model.Pin`."""
    index: dict[PinRef, Pin] = {}
    for c in sch.components:
        for p in c.pins:
            index[(c.designator, p.number)] = p
    return index


def _pin_anchors(
    tokens: list[str], pin_index: dict[PinRef, Pin]
) -> tuple[tuple[float, float] | None, list[dict]]:
    """Anchor each ``REF.PIN`` token that resolves to a real pin (positionless
    tokens — an unknown pin or an unmatched wildcard pattern — are skipped).
    Returns ``(pos, anchors)`` where ``pos`` is the first anchored pin's
    position, or ``None`` if none of ``tokens`` resolve."""
    anchors: list[dict] = []
    pos: tuple[float, float] | None = None
    for token in tokens:
        ref, _, num = token.partition(".")
        pin = pin_index.get((ref, num))
        if pin is None:
            continue
        p = (pin.x_mil, pin.y_mil)
        anchors.append(anchor("pin", token, p))
        if pos is None:
            pos = p
    return pos, anchors


def run(sch: Schematic, spec: IntentSpec) -> list[Finding]:
    """Assert ``spec`` against the schematic's built netlist. Pure, deterministic.

    Returns one aggregated finding per intent net per problem class (refs carry
    the individual pins), plus one finding per shorted intent-net group.
    """
    findings: list[Finding] = []
    pin_idx = _pin_index(sch)

    known_pins: set[PinRef] = {
        (c.designator, p.number) for c in sch.components for p in c.pins
    }
    pin_to_net: dict[PinRef, int] = {}
    for i, net in enumerate(sch.nets):
        for m in net.members:
            pin_to_net[tuple(m)] = i

    resolved: dict[str, int] = {}  # intent net name -> matched sch.nets index
    for name in sorted(spec.nets):
        net_spec = spec.nets[name]
        members = net_spec.members

        # Concrete (non-wildcard) members absent from the schematic entirely.
        # Wildcards are exempt: matching zero pins is a MISSING_MEMBER, not
        # an unknown-pin error (the pattern never claimed to name a real pin).
        unknown = sorted(
            m.token for m in members
            if not m.is_wildcard and (m.ref, m.pin) not in known_pins
        )
        if unknown:
            findings.append(Finding(
                INTENT_PIN_UNKNOWN, Severity.ERROR,
                f"intent net '{name}': pin(s) not in schematic: "
                + ", ".join(unknown),
                refs=unknown,
            ))
        unknown_set = set(unknown)

        # Every member's set of actual candidate pins: itself for a concrete
        # member (empty if unknown), or every known pin whose designator
        # fnmatch-matches the REF pattern and whose pin number equals the
        # member's literal pin, for a wildcard member.
        candidates: dict[Member, list[PinRef]] = {}
        for m in members:
            if m.is_wildcard:
                candidates[m] = sorted(
                    (d, p) for d, p in known_pins
                    if p == m.pin and fnmatch.fnmatchcase(d, m.ref)
                )
            elif m.token in unknown_set:
                candidates[m] = []
            else:
                candidates[m] = [(m.ref, m.pin)]

        # Best-containing actual net. Votes are per MEMBER (a wildcard that
        # matches many pins on one net still casts a single vote there), and
        # literal members DOMINATE the ranking — a broad wildcard must never
        # drag the intent net onto an unrelated net that merely contains many
        # pattern-matching pins, away from the concrete pins the user named.
        lit_hits: dict[int, int] = {}
        wild_hits: dict[int, int] = {}
        for m, cands in candidates.items():
            nets_seen = {pin_to_net[pr] for pr in cands if pr in pin_to_net}
            bucket = wild_hits if m.is_wildcard else lit_hits
            for idx in nets_seen:
                bucket[idx] = bucket.get(idx, 0) + 1
        hits: dict[int, int] = {
            i: lit_hits.get(i, 0) + wild_hits.get(i, 0)
            for i in set(lit_hits) | set(wild_hits)
        }
        if not hits:
            all_tokens = [m.token for m in members]
            pos, anchors_ = _pin_anchors(all_tokens, pin_idx)
            findings.append(Finding(
                INTENT_NET_NOT_FOUND, Severity.ERROR,
                f"intent net '{name}': no actual net contains any of its "
                f"pin(s): {', '.join(all_tokens)}",
                refs=all_tokens,
                pos=pos,
                anchors=anchors_,
            ))
            continue
        best = min(
            hits,
            key=lambda i: (
                -lit_hits.get(i, 0),      # literal members dominate
                -wild_hits.get(i, 0),
                0 if name.strip().upper() in _name_set(sch.nets[i]) else 1,
                i,
            ),
        )
        resolved[name] = best
        actual = sch.nets[best]
        label = _net_label(actual)
        actual_members = {tuple(m) for m in actual.members}

        missing = sorted(
            m.token for m in members
            if m.token not in unknown_set
            and not any(pr in actual_members for pr in candidates[m])
        )
        if missing:
            pos, anchors_ = _pin_anchors(missing, pin_idx)
            anchors_.append(anchor("net", label))
            findings.append(Finding(
                INTENT_MISSING_MEMBER, Severity.ERROR,
                f"intent net '{name}' (matched actual net '{label}'): "
                f"missing member(s): {', '.join(missing)}",
                refs=missing,
                pos=pos,
                anchors=anchors_,
            ))

        if net_spec.mode == "exact":
            # Wildcards are ignored for EXTRA-member computation: only
            # literal members are subtracted from the actual net's
            # membership (see module docstring for the rationale).
            literal_want = {(m.ref, m.pin) for m in members if not m.is_wildcard}
            extra = sorted(f"{d}.{p}" for d, p in actual_members - literal_want)
            if extra:
                pos, anchors_ = _pin_anchors(extra, pin_idx)
                anchors_.append(anchor("net", label))
                findings.append(Finding(
                    INTENT_EXTRA_MEMBER, Severity.ERROR,
                    f"intent net '{name}' (matched actual net '{label}'): "
                    f"extra member(s) not in intent: {', '.join(extra)}",
                    refs=extra,
                    pos=pos,
                    anchors=anchors_,
                ))

    # Two intent nets landing on ONE actual net is a short against intent.
    by_actual: dict[int, list[str]] = {}
    for name, idx in resolved.items():
        by_actual.setdefault(idx, []).append(name)
    for idx in sorted(by_actual):
        names = sorted(by_actual[idx])
        if len(names) < 2:
            continue
        quoted = ", ".join(f"'{n}'" for n in names)
        shorted_label = _net_label(sch.nets[idx])
        findings.append(Finding(
            INTENT_NETS_SHORTED, Severity.ERROR,
            f"intent nets {quoted} resolve to the same actual net "
            f"'{shorted_label}' — shorted against intent",
            refs=names,
            anchors=[anchor("net", shorted_label)],
        ))

    return findings


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def snapshot(sch: Schematic, include_unnamed: bool = False) -> dict:
    """Emit a valid intent document from the current netlist.

    Named nets only by default (auto-named nets churn on every edit);
    ``include_unnamed=True`` adds them keyed by their membership-stable
    ``stable_id``. A duplicate display name (e.g. the same local label on two
    sheets) is disambiguated as ``NAME@stable_id`` — matching is by membership,
    so the key is only a report label. ``snapshot -> run`` yields no findings.
    """
    nets: dict[str, list[str]] = {}
    for net in sch.nets:
        named = bool(net.is_named and net.name)
        if not named and not include_unnamed:
            continue
        key = net.name if named else net.stable_id
        if key in nets:
            key = f"{key}@{net.stable_id}"
        nets[key] = _refs(net.members)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "mode": "exact",
        "nets": {k: nets[k] for k in sorted(nets)},
    }
