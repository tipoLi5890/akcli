"""MCU pin -> net mapping + optional cross-check (GENERIC) (SPEC §3.6).

This check is deliberately **generic**: it emits the MCU's ``pin -> net`` table
from the (authoritative) schematic and, when given an *external* expected
``pin -> signal`` table, cross-checks the two and flags divergence.

What this module does NOT do (by design): it never parses a DTS, a board
pinout, or any firmware artifact. Producing the ``expected`` dict from such
sources lives in ``adapters/`` (``dts.py`` / ``pinout_md.py``) so this engine
stays reusable across projects. The ``expected`` table is passed in already
shaped as ``{pin_key: signal_name}``.

Authority model (LOCKED by SPEC): the **schematic is authoritative**; the
``expected`` table is **advisory**. A divergence is therefore reported as a
WARNING whose message states the schematic value wins -- it never mutates the
schematic and never escalates above WARNING.

Pin-name ``Pn.mm`` parser: GPIO pin names such as ``"P0.25"`` (and the common
``"P0_25"`` variant, with an optional trailing ``/alt-function`` suffix) are
normalized to a canonical ``"P<port>.<pin>"`` key so an expected table keyed by
either pin *number* or pin *name* still matches.
"""

from __future__ import annotations

import re

from ..model import Component, Net, Schematic
from ..report import Finding, Severity

# ``P<port>.<pin>`` / ``P<port>_<pin>`` with an optional trailing alt-function
# (e.g. ``P0.25/AIN1``). Anchored at the start so only true GPIO names parse.
_PIN_NAME_RE = re.compile(r"^P(\d+)[._](\d+)(?:\b|$)", re.IGNORECASE)


def parse_pin_name(name: str | None) -> tuple[int, int] | None:
    """Parse a ``Pn.mm`` GPIO pin name into ``(port, pin)``.

    Accepts ``P0.25``, ``P0_25`` and a trailing alt-function such as
    ``P0.25/AIN1``. Returns ``None`` for anything that is not a GPIO pin name.
    """
    if not name:
        return None
    m = _PIN_NAME_RE.match(name.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _canonical_pin_name(name: str | None) -> str | None:
    """Return the canonical ``P<port>.<pin>`` form, or ``None`` if not GPIO-shaped."""
    parsed = parse_pin_name(name)
    if parsed is None:
        return None
    return f"P{parsed[0]}.{parsed[1]}"


def _pin_keys(number: str | None, name: str | None) -> set[str]:
    """All (upper-cased) keys an expected-table entry may use to reference a pin."""
    keys: set[str] = set()
    if number:
        keys.add(number.strip().upper())
    if name:
        keys.add(name.strip().upper())
        canon = _canonical_pin_name(name)
        if canon:
            keys.add(canon.upper())
    return keys


def _normalize_expected_key(key: object) -> set[str]:
    """Normalize an expected-table key into the comparable key set."""
    k = str(key).strip()
    out = {k.upper()}
    canon = _canonical_pin_name(k)
    if canon:
        out.add(canon.upper())
    return out


def _net_name_set(net: Net | None) -> set[str]:
    """Case-folded set of every name a net answers to (name + aliases + sources)."""
    if net is None:
        return set()
    names = [net.name, *net.aliases, *net.source_names]
    return {n.strip().upper() for n in names if n}


def _find_mcu(sch: Schematic, designator: str) -> Component | None:
    target = designator.strip().upper()
    for comp in sch.components:
        if comp.designator and comp.designator.strip().upper() == target:
            return comp
    return None


def _pin_to_net_index(sch: Schematic) -> dict[tuple[str, str], Net]:
    """Map every ``(designator, pin_number)`` member to its owning net."""
    index: dict[tuple[str, str], Net] = {}
    for net in sch.nets:
        for member in net.members:
            index[member] = net
    return index


def _pin_sort_key(number: str | None) -> tuple[int, object]:
    """Sort pins numerically when possible, else lexicographically (numbers first)."""
    n = (number or "").strip()
    if n.isdigit():
        return (0, int(n))
    return (1, n.upper())


def _ref(designator: str, number: str | None) -> str:
    return f"{designator}.{number}" if number else designator


def run(sch: Schematic, cfg: object | None, expected: dict | None = None) -> list[Finding]:
    """Emit the MCU pin->net map and (optionally) cross-check ``expected``.

    Args:
        sch: the authoritative parsed schematic.
        cfg: a ``config.Config`` (or anything exposing ``mcu_designator``); may
            be ``None``.
        expected: optional external ``{pin_key: signal_name}`` table. Keys may be
            pin numbers (``"2"``) or pin names (``"P0.25"``/``"P0_25"``); values
            are the expected net/signal names. The schematic wins on conflict.

    Returns:
        A list of ``report.Finding`` (the MCU map as INFO findings, plus any
        cross-check WARNING/NOTE findings).
    """
    findings: list[Finding] = []

    mcu_designator = getattr(cfg, "mcu_designator", None)
    if not mcu_designator:
        findings.append(
            Finding(
                "PINMAP_NO_MCU",
                Severity.WARNING,
                "no MCU designator configured -- pass --mcu <REF> (e.g. "
                "`akcli pinmap main.SchDoc --mcu U3`) or set "
                "[project].mcu_designator in altium-kicad-cli.toml; "
                "cannot build a pin map",
                [],
            )
        )
        return findings

    mcu = _find_mcu(sch, mcu_designator)
    if mcu is None:
        findings.append(
            Finding(
                "PINMAP_MCU_NOT_FOUND",
                Severity.WARNING,
                f"configured MCU '{mcu_designator}' not found in schematic",
                [mcu_designator],
            )
        )
        return findings

    if not mcu.pins:
        findings.append(
            Finding(
                "PINMAP_NO_PINS",
                Severity.NOTE,
                f"MCU '{mcu.designator}' has no pins to map",
                [mcu.designator],
            )
        )
        return findings

    index = _pin_to_net_index(sch)
    pins = sorted(mcu.pins, key=lambda p: _pin_sort_key(p.number))

    # --- (1) GENERIC output: emit the MCU pin -> net table -------------------
    # Each entry records the pin, optional GPIO name, and its resolved net.
    pin_records: list[tuple[object, Net | None]] = []
    for pin in pins:
        net = index.get((mcu.designator, pin.number))
        pin_records.append((pin, net))
        canon = _canonical_pin_name(pin.name)
        name_part = ""
        if pin.name:
            name_part = f" ({pin.name})" if canon is None else f" ({canon})"
        if net is None:
            findings.append(
                Finding(
                    "PINMAP_FLOATING",
                    Severity.NOTE,
                    f"{_ref(mcu.designator, pin.number)}{name_part} -> "
                    "(no net / single-pin)",
                    [_ref(mcu.designator, pin.number)],
                )
            )
        else:
            findings.append(
                Finding(
                    "PINMAP",
                    Severity.INFO,
                    f"{_ref(mcu.designator, pin.number)}{name_part} -> {net.name}",
                    [_ref(mcu.designator, pin.number)],
                )
            )

    # --- (2) optional cross-check against the external expected table --------
    if expected:
        # Build a lookup from every key a pin answers to -> (pin, net).
        key_to_pin: dict[str, tuple[object, Net | None]] = {}
        for pin, net in pin_records:
            for key in _pin_keys(pin.number, pin.name):
                # First writer wins; pins should not collide, but be defensive.
                key_to_pin.setdefault(key, (pin, net))

        matched_pins: set[str] = set()
        for raw_key, signal in expected.items():
            exp_keys = _normalize_expected_key(raw_key)
            hit = next(
                ((key_to_pin[k]) for k in exp_keys if k in key_to_pin), None
            )
            if hit is None:
                findings.append(
                    Finding(
                        # WARNING (not NOTE) so an expected table that doesn't line up
                        # with the schematic fails the exit code instead of green-lighting
                        # silently (e.g. GPIO-number keys vs symbol pin names).
                        "PINMAP_EXPECTED_PIN_MISSING",
                        Severity.WARNING,
                        f"expected pin '{raw_key}' (-> '{signal}') is not present "
                        f"on MCU '{mcu.designator}'",
                        [str(raw_key)],
                    )
                )
                continue

            pin, net = hit
            matched_pins.add(pin.number)
            ref = _ref(mcu.designator, pin.number)
            exp_signal = str(signal).strip()
            actual_names = _net_name_set(net)

            if not actual_names:
                findings.append(
                    Finding(
                        "PINMAP_MISMATCH",
                        Severity.WARNING,
                        f"{ref}: expected signal '{exp_signal}' but schematic pin "
                        "is on no net (schematic is authoritative)",
                        [ref],
                    )
                )
            elif exp_signal.upper() in actual_names:
                findings.append(
                    Finding(
                        "PINMAP_MATCH",
                        Severity.INFO,
                        f"{ref}: expected '{exp_signal}' matches schematic net "
                        f"'{net.name}'",
                        [ref],
                    )
                )
            else:
                findings.append(
                    Finding(
                        "PINMAP_MISMATCH",
                        Severity.WARNING,
                        f"{ref}: expected signal '{exp_signal}' but schematic net "
                        f"is '{net.name}' (schematic is authoritative; expected "
                        "table is advisory)",
                        [ref],
                    )
                )

        # MCU pins with a net that the expected table never covered.
        for pin, net in pin_records:
            if net is None or pin.number in matched_pins:
                continue
            findings.append(
                Finding(
                    "PINMAP_UNEXPECTED",
                    Severity.NOTE,
                    f"{_ref(mcu.designator, pin.number)} -> '{net.name}' is not "
                    "listed in the expected table",
                    [_ref(mcu.designator, pin.number)],
                )
            )

    return findings
