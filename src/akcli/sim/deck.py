"""Schematic -> SPICE deck: the pure-text *value* layer of ``akcli sim``.

:func:`build` walks a :class:`~akcli.model.Schematic` that already
carries inferred ``Net`` records and emits a complete, self-contained ngspice
deck as a single string.  It performs three jobs and nothing else (the engine
and the assertion layers are separate stages):

* **Node mapping.**  Every net becomes a SPICE node.  The ground net (matched
  case-insensitively against the ``gnd`` argument, its ``source_names`` or the
  literal ``"0"``) maps to node ``"0"``; every other named net is *sanitized*
  to an uppercase ``[A-Za-z0-9_]`` token (a leading digit is prefixed with
  ``N``); unnamed nets become ``N<index>``.  ngspice is case-insensitive, so
  two distinct nets that sanitize to the same token are a hard error
  (``SIM_NODE_COLLISION``) — **including two same-named nets from different
  sheet scopes** (netbuild keeps local labels sheet-local, so two instances of
  one child sheet each own a ``MID``; SPICE is flat, so emitting both would
  silently short them).  A deck with no ground net is a hard error
  (``SIM_NO_GROUND``) — the caller is pointed at the ``--gnd`` flag.

* **Elements.**  Each real component (``#``-prefixed virtual power symbols are
  skipped) is resolved through :func:`akcli.sim.models.resolve`.
  ``ok`` cards are emitted as ``<name> <nodes...> <value|model>``; ``skip``
  cards become a comment; ``unmodeled`` cards are collected, commented, and
  reported as ``SIM_UNMODELED`` warnings.  A pin that sits on no net gets a
  unique dangling node ``NC_<ref>_<pin>`` and a warning.

* **Dropped parts gate the verdict.**  Every part absent from the deck (skipped
  *or* unmodeled) is listed in ``Deck.skipped`` and reported
  (``SIM_PART_SKIPPED``, WARNING).  When such a part has two or more connected
  nodes and one of them is *measured* by an assert (``v(...)`` in its meas text),
  the finding is an **ERROR**: a skipped series element changes what was
  measured, so the run must not report PASS.  A value that had to be normalized
  to become SPICE-legal (``"10k 1%"`` -> ``10k``) is reported
  ``SIM_VALUE_NORMALIZED`` (WARNING); one that could not be parsed confidently
  raises ``SIM_VALUE_INVALID`` from :mod:`akcli.sim.models`.

* **Stimuli & layout.**  ``spec.stimuli`` describe independent ``V``/``I``
  sources and ``B`` behavioural sources (whose expression has *all* whitespace
  stripped — a libngspice parser gotcha).  The deck is laid out title-first
  (``* akcli sim: <source>``), then elements, stimuli, deduplicated model
  cards, the ``.<analysis>`` dot-cards from ``spec.analyses`` and finally
  ``.end``.

* **Convergence diagnostics.**  After element emission, a node with no
  independent source whose only remaining device connections are capacitors (or
  nothing, because every device on it was skipped) is reported ``SIM_FLOATING_NODE``
  (WARNING) — ngspice returns a singular matrix for such a node.  ``spec.options.rshunt``
  controls the fix: absent/``"auto"`` appends ``.option rshunt=1e12`` (and a
  ``SIM_RSHUNT_ADDED`` NOTE) only when >=1 floating node was found, ``false``
  never emits it, and a number/string always emits that value.  A power-named net
  (``+*``/``VCC*``/``VDD*``/``VBAT*``/``VSUP*``) with no voltage-source drive is
  reported ``SIM_UNDRIVEN_RAIL`` (WARNING) — a silent read-~0 trap.

The engineering-notation -> SPICE ``M``/``MEG`` normalization lives in
:func:`akcli.sim.models.spice_value` and is *not* reimplemented here;
component values arrive already normalized on the ``DeviceCard``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..errors import AkcliError
from ..model import Component, Net, Schematic
from ..report import Finding, Severity


@dataclass
class Deck:
    """A rendered SPICE deck plus the diagnostics gathered while building it.

    ``text`` is the full deck string (title line first, ``.end`` last).
    ``node_of`` maps each source net *name* to the SPICE node it became (unnamed
    nets are keyed by their generated ``N<index>`` node).  ``warnings`` are
    :class:`~akcli.report.Finding` records (unmodeled parts, dangling
    pins); ``unmodeled`` lists the designators that had no SPICE model.
    ``skipped`` lists **every** part that is absent from the deck — skipped
    (``Sim.Enable=0``, a ``spec.models`` skip, a connector/test-point) *and*
    unmodeled — as ``{"designator", "status", "reason", "nodes", "measured"}``
    dicts, so a caller can report exactly what the simulation left out.
    """

    text: str
    node_of: dict[str, str] = field(default_factory=dict)
    warnings: list[Finding] = field(default_factory=list)
    unmodeled: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# node sanitizing
# --------------------------------------------------------------------------- #
# Explicit ASCII class: ``str.isalnum()`` accepts all Unicode letters/digits,
# which would let multibyte tokens (and case-fold quirks like 'µ'.upper() == 'Μ')
# into the deck; restrict to plain ASCII so the collision check is well-defined.
_NON_NODE_CHAR_RX = re.compile(r"[^A-Za-z0-9_]")

# A net whose name looks like a power rail (matched case-insensitively against
# the net name and each of its source_names/aliases). An undriven rail is a
# silent-zero trap: a live session once forgot VSUP on +3V and every node read
# ~0 with no error.
_POWER_NAME_RX = re.compile(r"^(?:\+|VCC|VDD|VBAT|VSUP)", re.IGNORECASE)

# SPICE element letters that can *drive* a node: independent V/I/B sources and
# subckts (X — regulators, references, op-amps). A node touched only by
# passives (and no source) is undriven / floating.
_DRIVER_LETTERS = frozenset({"V", "I", "B", "X"})

# The rshunt value auto-inserted when a floating node is detected (a very large
# conductance to ground that resolves the singular-matrix without perturbing the
# operating point; the reference fix used exactly this).
_AUTO_RSHUNT = "1e12"


def _sanitize(name: str) -> str:
    """Return an ngspice-safe node token for a net name.

    Uppercased (ngspice is case-insensitive), every character outside the ASCII
    ``[A-Za-z0-9_]`` set replaced by ``_``, and a leading digit prefixed with
    ``N`` so the token is never mistaken for a number.
    """
    token = _NON_NODE_CHAR_RX.sub("_", name).upper()
    if not token:
        token = "NET"
    if token[0].isdigit():
        token = "N" + token
    return token


# Node references inside an assert's measurement text: ``v(mid)``, ``V(a,b)``.
# Used to decide whether a dropped part sits on a node the run MEASURES — the
# difference between a harmless omission and a wrong measured value.
_MEAS_NODE_RX = re.compile(r"(?i)\bv\s*\(\s*([^)]*)\)")


def _spec_asserts(spec: object) -> list:
    """The assert list of a ``SimSpec`` / plain-dict spec (empty when absent)."""
    asserts = getattr(spec, "asserts", None)
    if asserts is None and isinstance(spec, dict):
        asserts = spec.get("assert") or spec.get("asserts")
    return list(asserts or [])


def _measured_nodes(spec: object) -> set[str]:
    """SPICE nodes referenced by ``v(...)`` in any assert's measurement text."""
    nodes: set[str] = set()
    for a in _spec_asserts(spec):
        if not isinstance(a, dict):
            continue
        text = " ".join(str(a.get(k, "")) for k in ("meas", "when"))
        for group in _MEAS_NODE_RX.findall(text):
            for tok in group.split(","):
                tok = tok.strip()
                if tok:
                    nodes.add("0" if tok == "0" else _sanitize(tok))
    return nodes


def _net_scope(net: Net, sheet_of: dict[str, str]) -> str:
    """Human-readable sheet scope of ``net`` (the sheets its members live on).

    ``netbuild`` keeps local labels **sheet-scoped**, so two instances of the
    same child sheet produce two distinct ``Net`` records that share a name
    (``MID``). SPICE has no hierarchy, so the only way to tell them apart in a
    collision message is where their pins are.
    """
    sheets = sorted({sheet_of.get(des, "") for des, _pin in net.members})
    named = [s for s in sheets if s]
    if not named:
        return "root sheet"
    return "sheet " + ", ".join(named)


def _is_ground(net: Net, gnd: str) -> bool:
    """True when ``net`` is the ground net for the given ``--gnd`` name.

    Matches (case-insensitively) the net's canonical name, any of its
    ``source_names``/``aliases`` or the literal SPICE ground ``"0"``.
    """
    target = gnd.strip().lower()
    candidates = [net.name or ""]
    candidates += list(net.source_names or [])
    candidates += list(net.aliases or [])
    lowered = {str(c).strip().lower() for c in candidates}
    return target in lowered or "0" in lowered


def _element_name(letter: str, designator: str) -> str:
    """SPICE element name for a device ``letter`` and schematic ``designator``.

    SPICE infers the device type from the first letter, so a designator that
    already starts with the right letter (``R1`` for an ``R`` card) is used
    verbatim; otherwise the letter is prefixed (``X`` + ``U3`` -> ``XU3``).  An
    empty letter (unclassified) leaves the designator untouched.
    """
    if not letter:
        return designator
    if designator and designator[0].lower() == letter[0].lower():
        return designator
    return letter + designator


def _strip_ws(expr: str) -> str:
    """Remove *all* whitespace from a B-source expression.

    libngspice's B-source parser treats ``I=expr``/``V=expr`` as a single token
    and mis-parses any embedded spaces (a hard-won reference-script gotcha).
    """
    return re.sub(r"\s+", "", str(expr))


def _fmt_rshunt(value: object) -> str:
    """Render an explicit ``options.rshunt`` value for a ``.option`` card.

    Strings pass through verbatim (``"1e12"``, ``"1G"``); an integral float is
    printed without a spurious ``.0`` (``1e12`` -> ``1000000000000``); other
    numbers use ``repr`` so no precision is lost.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build(sch: Schematic, spec: object, *, gnd: str = "GND") -> Deck:
    """Render ``sch`` (with its inferred nets) to a :class:`Deck`.

    ``spec`` is a :class:`~akcli.sim.assertions.SimSpec` (or anything
    exposing ``stimuli``, ``analyses`` and ``options``).  ``gnd`` names the net
    that becomes SPICE node ``"0"``.

    Raises :class:`~akcli.errors.AkcliError` ``SIM_NO_GROUND`` when no
    net matches ``gnd`` and ``SIM_NODE_COLLISION`` when two distinct nets
    sanitize to the same node token.
    """
    from . import models  # sibling stage; imported lazily so tests can stub it

    stimuli = list(getattr(spec, "stimuli", []) or [])
    analyses = dict(getattr(spec, "analyses", {}) or {})
    options = dict(getattr(spec, "options", {}) or {})

    # --- 1. nodes ---------------------------------------------------------- #
    node_of: dict[str, str] = {}          # net name (or N<idx>) -> SPICE node
    pin_node: dict[tuple[str, str], str] = {}  # (designator, pin) -> node
    name_lookup: dict[str, str] = {"0": "0"}   # lower name -> node (for stimuli)
    origin: dict[str, str] = {}                # node -> first net that claimed it
    node_display: dict[str, str] = {}          # node -> net display name (non-gnd)
    node_power_names: dict[str, list[str]] = {}  # node -> names to rail-match
    have_ground = False
    unnamed_idx = 0
    # designator -> source sheet, for the collision message's scope names.
    sheet_of = {c.designator: (getattr(c, "sheet", "") or "")
                for c in sch.components}
    origin_net: dict[str, Net] = {}   # node -> the Net record that claimed it

    # Node tokens claimed by ground/named nets, pre-computed so the unnamed-net
    # generator can skip them: a net literally named 'N1' must never be silently
    # merged with a generated N1 node (a distinct-net short otherwise).
    reserved: set[str] = set()
    for net in sch.nets:
        if _is_ground(net, gnd):
            reserved.add("0")
        elif net.is_named and net.name:
            reserved.add(_sanitize(net.name))

    for net in sch.nets:
        if _is_ground(net, gnd):
            node = "0"
            have_ground = True
            key = net.name or "0"
        elif net.is_named and net.name:
            node = _sanitize(net.name)
            key = net.name
            prior = origin.get(node)
            prior_net = origin_net.get(node)
            if prior is not None and prior != key:
                raise AkcliError(
                    "SIM_NODE_COLLISION",
                    f"nets {prior!r} and {key!r} both sanitize to node "
                    f"{node!r}; rename one (ngspice node names are "
                    f"case-insensitive)",
                )
            if prior_net is not None and prior_net is not net:
                # Same NAME, different net: netbuild keeps local labels
                # sheet-scoped, so two instances of one child sheet each carry
                # their own 'MID'. SPICE is flat — emitting both as node MID
                # silently WIRES THEM TOGETHER, which is a wrong answer with no
                # warning (the merged node reads one shared value).
                raise AkcliError(
                    "SIM_NODE_COLLISION",
                    f"net {key!r} exists in two different sheet scopes "
                    f"({_net_scope(prior_net, sheet_of)} and "
                    f"{_net_scope(net, sheet_of)}) and both would become the "
                    f"single flat SPICE node {node!r} — ngspice has no "
                    f"hierarchy, so the two nets would be silently shorted "
                    f"together; give them distinct names (e.g. {key}_A/{key}_B) "
                    f"or promote them to one global label if they really are "
                    f"one net",
                )
            origin[node] = key
            origin_net[node] = net
        else:
            # Advance to the first free N<i>, skipping tokens already taken by a
            # named net or an earlier unnamed net.
            while True:
                unnamed_idx += 1
                node = f"N{unnamed_idx}"
                if node not in reserved:
                    break
            reserved.add(node)
            key = node
            origin[node] = key

        node_of[key] = node
        if node != "0":
            node_display.setdefault(node, net.name or key)
            cands = [net.name or "", *(net.source_names or []), *(net.aliases or [])]
            node_power_names.setdefault(node, [c for c in cands if c])
        for cand in [net.name or ""] + list(net.source_names or []) + list(net.aliases or []):
            if cand:
                name_lookup.setdefault(str(cand).strip().lower(), node)
        for des, pin in net.members:
            pin_node[(des, pin)] = node

    if not have_ground:
        raise AkcliError(
            "SIM_NO_GROUND",
            f"no net named {gnd!r} (nor '0'); pass --gnd <net> to name the "
            f"reference net that should become SPICE node 0",
        )

    # --- 2. elements ------------------------------------------------------- #
    elements: list[str] = []
    warnings: list[Finding] = []
    unmodeled: list[str] = []
    skipped: list[dict] = []
    measured_nodes = _measured_nodes(spec)
    # sim.json 'acknowledged_unmodeled': designators the author accepts as
    # absent from the deck — downgrades the in-series ERROR to WARNING.
    acked = {str(x) for x in (getattr(spec, "acknowledged_unmodeled", []) or [])}
    acked_used: set[str] = set()
    model_cards: list[str] = []
    # per-node bookkeeping for the floating-node / undriven-rail diagnostics.
    node_ok_letters: dict[str, set[str]] = {}    # node -> SPICE letters emitted
    node_stranders: dict[str, list[str]] = {}    # node -> skip/unmodeled refs

    def _add_card(card: str | None) -> None:
        if card:
            block = card.strip()
            if block and block not in model_cards:
                model_cards.append(block)

    def _strand(comp: Component) -> None:
        """Record ``comp`` as having stranded every real net it touches (it was
        skipped or unmodeled, so it contributes no DC path)."""
        for pin in comp.pins:
            node = pin_node.get((comp.designator, pin.number))
            if node is not None and node != "0":
                node_stranders.setdefault(node, []).append(comp.designator)

    def _record_dropped(comp: Component, status: str, note: str, *,
                        warn: bool = True) -> None:
        """Record + report a part that is absent from the deck.

        Every dropped part becomes visible (``SIM_PART_SKIPPED``; ``warn=False``
        for an unmodeled part, which already has its own ``SIM_UNMODELED``
        warning), and one that sits **in series on a measured node** escalates to
        ERROR: a skipped R2 in a measured divider changes the measured value, so
        a run that excluded it must never print PASS.
        """
        nodes = sorted({
            nd for pin in comp.pins
            if (nd := pin_node.get((comp.designator, pin.number))) is not None
        })
        hit = sorted(measured_nodes & set(nodes))
        # Two or more distinct connected nodes = the part could carry current
        # between them, so dropping it changes the measurement. A one-terminal
        # part (test point, mounting hole, flag) cannot.
        in_series = len(nodes) >= 2 and bool(hit)
        skipped.append({"designator": comp.designator, "status": status,
                        "reason": note, "nodes": nodes, "measured": in_series})
        if comp.designator in acked:
            acked_used.add(comp.designator)
        if in_series and comp.designator in acked:
            # Explicitly acknowledged: still visible, no longer blocking. The
            # message keeps the full context so the acceptance stays honest.
            warnings.append(Finding(
                code="SIM_PART_SKIPPED",
                severity=Severity.WARNING,
                message=f"{comp.designator} is not in the deck ({status}"
                + (f": {note}" if note else "")
                + f") but it connects measured node(s) {', '.join(hit)} to "
                f"{', '.join(n for n in nodes if n not in hit) or 'nothing else'}"
                " — acknowledged via sim.json acknowledged_unmodeled, "
                "downgraded to WARNING; the measured value still excludes "
                "this part",
                refs=[comp.designator, *hit],
            ))
        elif in_series:
            warnings.append(Finding(
                code="SIM_PART_SKIPPED",
                severity=Severity.ERROR,
                message=f"{comp.designator} is not in the deck ({status}"
                + (f": {note}" if note else "")
                + f") but it connects measured node(s) {', '.join(hit)} to "
                f"{', '.join(n for n in nodes if n not in hit) or 'nothing else'}"
                " — the measured value excludes this series element, so a PASS "
                "would be meaningless; give it Sim.Device/Sim.Params or a "
                "spec.models entry, acknowledge it in sim.json "
                "acknowledged_unmodeled, or stop asserting on that node",
                refs=[comp.designator, *hit],
            ))
        elif warn:
            warnings.append(Finding(
                code="SIM_PART_SKIPPED",
                severity=Severity.WARNING,
                message=f"{comp.designator} is not in the deck ({status}"
                + (f": {note}" if note else "") + ")",
                refs=[comp.designator],
            ))

    for comp in sch.components:
        if comp.designator.startswith("#"):
            continue  # virtual power symbol: contributes a net name, not a device
        card = models.resolve(comp, spec)
        status = getattr(card, "status", "ok")
        note = getattr(card, "note", "") or ""

        if status == "skip":
            elements.append(f"* skip {comp.designator}: {note}".rstrip())
            _strand(comp)
            _record_dropped(comp, "skip", note)
            continue
        if status == "unmodeled":
            unmodeled.append(comp.designator)
            _strand(comp)
            _record_dropped(comp, "unmodeled", note, warn=False)
            warnings.append(
                Finding(
                    code="SIM_UNMODELED",
                    severity=Severity.WARNING,
                    message=f"{comp.designator}: no SPICE model — omitted from deck"
                    + (f" ({note})" if note else ""),
                    refs=[comp.designator],
                )
            )
            elements.append(
                f"* unmodeled {comp.designator}: {note}".rstrip()
            )
            continue

        # status == "ok": map pins -> nodes and emit the element line.
        if getattr(card, "pin_order_assumed", False):
            warnings.append(
                Finding(
                    code="SIM_PIN_ORDER_ASSUMED",
                    severity=Severity.WARNING,
                    message=f"{comp.designator}: pin names do not identify the "
                    "SPICE terminal order — emitted in schematic pin-number "
                    "order; verify polarity (set Sim.Pins or a spec.models "
                    "pin_order if reversed)",
                    refs=[comp.designator],
                )
            )
        value_note = getattr(card, "value_note", "") or ""
        if value_note:
            warnings.append(
                Finding(
                    code="SIM_VALUE_NORMALIZED",
                    severity=Severity.WARNING,
                    message=f"{comp.designator}: {value_note} — only the leading "
                    "value token reaches ngspice; move ratings/tolerance into "
                    "their own field if that is not what you meant",
                    refs=[comp.designator],
                )
            )
        nodes = _element_nodes(comp, card, pin_node, warnings)
        letter = (getattr(card, "letter", "") or "").upper()
        name = _element_name(letter, comp.designator)
        tail = getattr(card, "model_name", None) or getattr(card, "value", None) or ""
        if letter and letter[0] in "RCL" and not getattr(card, "model_name", None):
            # a zero-valued passive is a solver trap: R=0 is a hard short
            # (singular matrix in some analyses), C=0 / L=0 silently deletes
            # the part's effect — almost always a placeholder value
            try:
                from ..calc.si import parse_value as _pv
                zero = _pv(str(tail).rstrip("FfHh")) == 0.0
            except (ValueError, TypeError):
                zero = False
            if zero:
                warnings.append(
                    Finding(
                        code="SIM_ZERO_PASSIVE",
                        severity=Severity.WARNING,
                        message=f"{comp.designator}: value {tail!r} parses to 0 "
                        f"— a zero {letter[0]} is a solver trap (short / no-op); "
                        "set the real value or Sim.Enable=0 to skip it",
                        refs=[comp.designator],
                    )
                )
        elements.append(" ".join([name, *nodes, str(tail)]).rstrip())
        _add_card(getattr(card, "model_card", None))
        if letter:
            for nd in nodes:
                if nd != "0":
                    node_ok_letters.setdefault(nd, set()).add(letter[0])

    # A stale acknowledged_unmodeled entry (naming a part that is present in
    # the deck, or gone from the schematic) must not rot silently — a later
    # regression on that designator would be pre-forgiven.
    for des in sorted(acked - acked_used):
        warnings.append(Finding(
            code="SIM_UNMODELED_ACK_UNUSED",
            severity=Severity.NOTE,
            message=f"acknowledged_unmodeled entry {des!r} matched no part "
            "absent from the deck — the part is modeled or no longer exists; "
            "remove the stale entry",
            refs=[des],
        ))

    # --- 3. stimuli -------------------------------------------------------- #
    stim_lines: list[str] = []
    net_names = [net.name for net in sch.nets if net.name]
    node_any_stim: set[str] = set()      # nodes touched by any independent source
    node_vsource_stim: set[str] = set()  # nodes touched by a voltage source

    def _mark_stim(nodes: tuple[str, ...], kind: str) -> None:
        for nd in nodes:
            if nd != "0":
                node_any_stim.add(nd)
                if kind == "vsource":
                    node_vsource_stim.add(nd)

    def _node_for(raw: object, stim_name: str) -> str:
        if raw is None:
            return "0"
        text = str(raw).strip()
        if text == "0" or text == "":
            return "0"
        node = name_lookup.get(text.lower())
        if node is not None:
            return node
        # Unknown net: still emit (deck-only workflows lean on this) but warn,
        # with close-match suggestions so a typo is obvious.
        suggestions = difflib.get_close_matches(text, net_names, n=3)
        hint = f" (did you mean {', '.join(suggestions)}?)" if suggestions else ""
        warnings.append(
            Finding(
                code="SIM_UNKNOWN_STIMULUS_NODE",
                severity=Severity.WARNING,
                message=f"stimulus {stim_name or '?'}: node {text!r} matches no "
                f"net — a new dangling node was created{hint}",
                refs=[stim_name] if stim_name else [],
            )
        )
        return _sanitize(text)

    for stim in stimuli:
        kind = str(stim.get("kind", "")).lower()
        name = str(stim.get("name", ""))
        n1 = _node_for(stim.get("node"), name)
        n2 = _node_for(stim.get("node2", "0"), name)
        if n1 == n2:
            # both terminals on one node (usually both resolved to ground):
            # the source drives nothing — a silent no-op trap
            warnings.append(
                Finding(
                    code="SIM_STIMULUS_SHORTED",
                    severity=Severity.WARNING,
                    message=f"stimulus {name or '?'}: both terminals resolve to "
                    f"node {n1!r} — the source drives nothing (check node/"
                    "node2 and the --gnd mapping)",
                    refs=[name] if name else [],
                )
            )
        if kind == "vsource":
            stim_lines.append(f"{_element_name('V', name)} {n1} {n2} {stim.get('value', '')}".rstrip())
            _mark_stim((n1, n2), kind)
        elif kind == "isource":
            stim_lines.append(f"{_element_name('I', name)} {n1} {n2} {stim.get('value', '')}".rstrip())
            _mark_stim((n1, n2), kind)
        elif kind == "bsource":
            quantity = str(stim.get("quantity", "I")).upper()
            quantity = quantity if quantity in ("I", "V") else "I"
            expr = _strip_ws(stim.get("expr", stim.get("value", "")))
            stim_lines.append(f"{_element_name('B', name)} {n1} {n2} {quantity}={expr}")
            _mark_stim((n1, n2), kind)
        else:
            warnings.append(
                Finding(
                    code="SIM_BAD_STIMULUS",
                    severity=Severity.WARNING,
                    message=f"stimulus {name or '?'}: unknown kind {kind!r} — skipped",
                    refs=[name] if name else [],
                )
            )

    # extra model/subckt cards from the spec, appended once and deduplicated.
    for extra in options.get("extra_cards", []) or []:
        _add_card(str(extra))

    # --- 4. floating-node + undriven-rail diagnostics ---------------------- #
    # A node is *floating* when it has no independent source and its only
    # remaining (non-skipped) element connections are capacitors — or nothing at
    # all because every device on it was skipped/unmodeled. ngspice returns a
    # singular matrix for such a node; the fix is a large rshunt to ground.
    floating_nodes: list[str] = []
    for node, disp in node_display.items():
        if node in node_any_stim:
            continue
        letters = node_ok_letters.get(node, set())
        if letters - {"C"}:
            continue  # a resistor/inductor/source/etc. gives it a DC path
        floating_nodes.append(node)
        stranders = sorted(set(node_stranders.get(node, [])))
        detail = (
            f"stranded by skipped/unmodeled {stranders}"
            if stranders
            else "only capacitor(s) / no source attached"
        )
        warnings.append(
            Finding(
                code="SIM_FLOATING_NODE",
                severity=Severity.WARNING,
                message=f"net {disp!r} (node {node}) has no DC path to ground — "
                f"{detail}; ngspice will see a singular matrix without an rshunt",
                refs=[disp, *stranders],
            )
        )

    # Undriven power rail: a power-named net with no voltage-source stimulus and
    # no non-skipped source element (V/I/B/X) attached reads ~0 silently. Only
    # meaningful once the deck is actually driven — a deck-only build with no
    # stimuli at all would flag every rail, so gate on the spec having any.
    for node, names in (node_power_names.items() if stimuli else ()):
        if not any(_POWER_NAME_RX.match(n) for n in names):
            continue
        if node in node_vsource_stim:
            continue
        if node_ok_letters.get(node, set()) & _DRIVER_LETTERS:
            continue
        disp = node_display.get(node, node)
        stranders = sorted(set(node_stranders.get(node, [])))
        detail = (
            f" (its only source {stranders} was skipped/unmodeled)" if stranders else ""
        )
        warnings.append(
            Finding(
                code="SIM_UNDRIVEN_RAIL",
                severity=Severity.WARNING,
                message=f"power rail {disp!r} (node {node}) has no voltage-source "
                f"drive{detail} — it will read ~0; add a vsource stimulus",
                refs=[disp, *stranders],
            )
        )

    # --- 5. options (rshunt) ----------------------------------------------- #
    # spec.options.rshunt: absent/"auto" -> add rshunt only when >=1 floating
    # node was found; false -> never; a number/string -> always emit that value.
    option_lines: list[str] = []
    rshunt = options.get("rshunt", "auto")
    if rshunt is False:
        pass
    elif rshunt is True or (isinstance(rshunt, str) and rshunt.strip().lower() == "auto"):
        if floating_nodes:
            option_lines.append(
                f"* akcli: rshunt auto-added — {len(floating_nodes)} floating "
                f"node(s) detected ({', '.join(floating_nodes)})"
            )
            option_lines.append(f".option rshunt={_AUTO_RSHUNT}")
            warnings.append(
                Finding(
                    code="SIM_RSHUNT_ADDED",
                    severity=Severity.NOTE,
                    message=f".option rshunt={_AUTO_RSHUNT} auto-added for "
                    f"{len(floating_nodes)} floating node(s): "
                    f"{', '.join(floating_nodes)}",
                    refs=list(floating_nodes),
                )
            )
    else:
        option_lines.append(f".option rshunt={_fmt_rshunt(rshunt)}")

    # --- 6. analyses ------------------------------------------------------- #
    analysis_lines: list[str] = []
    if options.get("inline_analyses", True):
        for an, params in analyses.items():
            params = (params or "").strip()
            if not an:
                continue
            if params.startswith("."):
                analysis_lines.append(params)
            elif params.split(" ", 1)[0].lower() == an.lower():
                analysis_lines.append("." + params)
            elif params:
                analysis_lines.append(f".{an} {params}")
            else:
                analysis_lines.append(f".{an}")

    # --- 7. layout --------------------------------------------------------- #
    lines: list[str] = [f"* akcli sim: {sch.source_path}"]
    lines += elements
    lines += stim_lines
    lines += model_cards
    lines += option_lines
    lines += analysis_lines
    lines.append(".end")
    text = "\n".join(lines) + "\n"

    return Deck(text=text, node_of=node_of, warnings=warnings,
                unmodeled=unmodeled, skipped=skipped)


# SPICE element letter -> exact terminal count ngspice expects on the element
# line.  ``X`` (subcircuit) is deliberately absent: its port count is defined
# by the .subckt model, unknowable from the letter alone.
_EXPECTED_TERMINALS: dict[str, int] = {
    "R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2,
    "Q": 3,
}


def _element_nodes(
    comp: Component,
    card: object,
    pin_node: dict[tuple[str, str], str],
    warnings: list[Finding],
) -> list[str]:
    """Ordered SPICE node list for a component's terminals.

    ``card.pin_order`` (pin numbers in SPICE terminal order) is honored when
    given; otherwise the schematic pin order is used.  A pin that is on no net
    gets a unique dangling node ``NC_<ref>_<pin>`` and a ``SIM_DANGLING_PIN``
    warning so it never silently shorts to another terminal.

    Raises :class:`~akcli.errors.AkcliError` ``SIM_NODE_COUNT_MISMATCH`` when
    the node count disagrees with the element letter's fixed terminal count
    (e.g. a 2-terminal equivalent model mapped onto a 3+-pad part with no
    ``pin_order``) — ngspice would otherwise reject the whole deck with only
    a cryptic ``circuit not parsed``.
    """
    pin_order = getattr(card, "pin_order", None)
    if pin_order:
        numbers = [str(p) for p in pin_order]
    else:
        numbers = [p.number for p in comp.pins]

    letter = (getattr(card, "letter", "") or "").upper()[:1]
    expected = _EXPECTED_TERMINALS.get(letter)
    if expected is not None and len(numbers) != expected:
        source = "pin_order" if pin_order else "schematic pin count"
        raise AkcliError(
            "SIM_NODE_COUNT_MISMATCH",
            f"{comp.designator}: resolves to a {expected}-terminal "
            f"{letter!r} element but {len(numbers)} node(s) would be emitted "
            f"({source}) — ngspice rejects such a line with a bare 'circuit "
            f"not parsed'; set the model's pin_order to exactly {expected} "
            f"pin number(s) naming which pads are the SPICE terminals, e.g. "
            f'"pin_order": ["1", "3"]',
        )

    nodes: list[str] = []
    for num in numbers:
        node = pin_node.get((comp.designator, num))
        if node is None:
            node = f"NC_{comp.designator}_{num}"
            warnings.append(
                Finding(
                    code="SIM_DANGLING_PIN",
                    severity=Severity.WARNING,
                    message=f"{comp.designator}.{num} is on no net — tied to "
                    f"dangling node {node}",
                    refs=[comp.designator],
                )
            )
        nodes.append(node)
    return nodes
