"""Zephyr DTS / overlay + pinctrl -> expected pin->signal table (SPEC §3.8).

This is a **generic Zephyr adapter**, not a board-specific one: it reads a
devicetree source/overlay and extracts GPIO assignments into an *expected*
``pin -> signal`` table that :func:`checks.pinmap.run` can consume as its
``expected`` argument. Nothing here is hard-coded to a particular board.

Two assignment forms are recognized:

1. **phandle + pin** -- the portable devicetree form, e.g.::

       led_red: led_0 {
           gpios = <&gpio0 25 GPIO_ACTIVE_LOW>;
       };

   ``&gpio0 25`` -> controller ``gpio0`` (port 0), pin 25 -> canonical ``P0.25``.

2. **Nordic pinctrl** -- the ``nordic,nrf-psel`` binding using the
   ``NRF_PSEL(fn, port, pin)`` macro, e.g.::

       uart0_default: uart0_default {
           group1 {
               psels = <NRF_PSEL(UART_TX, 0, 6)>,
                       <NRF_PSEL(UART_RX, 0, 8)>;
           };
       };

   ``NRF_PSEL(UART_TX, 0, 6)`` -> function ``UART_TX``, port 0, pin 6 -> ``P0.6``.

Output orientation (so it actually feeds :func:`checks.pinmap.run`): pinmap's
``expected`` table is keyed by a *pin reference* (a pin number or a GPIO name
such as ``"P0.25"``) and its values are *signal/net names*. We therefore emit
``{ "P<port>.<pin>": signal }`` -- the **GPIO is the key**, the DTS-derived
signal/node name is the value. pinmap then looks up that physical pin in the
(authoritative) schematic and compares its net name against this advisory
signal.

Zero third-party dependencies (stdlib only).
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["parse_dts", "to_expected_table"]

# --- low-level regexes -------------------------------------------------------

# A GPIO controller phandle followed by a pin number: ``&gpio0 25`` / ``&gpio1 0x05``.
_GPIO_RE = re.compile(r"&gpio(\d+)\s+(0x[0-9a-fA-F]+|\d+)")

# NRF_PSEL(function, port, pin) -- the Nordic pinctrl macro.
_PSEL_RE = re.compile(
    r"NRF_PSEL\w*\(\s*([A-Za-z0-9_]+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)"
)

# GPIO-ish property suffixes we strip when deriving a signal name from a property.
_GPIO_SUFFIXES = ("-gpios", "-gpio", "_gpios", "_gpio")


# --- comment / structure scanning -------------------------------------------

def _strip_comments(text: str) -> str:
    """Remove C/C++ style comments (``/* */`` and ``//``) from devicetree text."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _parse_node_header(chunk: str) -> dict:
    """Parse a node header ``label: name@addr`` (or ``&ref`` / ``/``) into parts."""
    chunk = chunk.strip()
    label: str | None = None
    if ":" in chunk:
        label, _, chunk = chunk.partition(":")
        label = label.strip() or None
        chunk = chunk.strip()
    name = chunk.split("@", 1)[0].strip()
    return {"label": label, "name": name}


def _scan_statements(text: str) -> list[tuple[list[dict], str]]:
    """Walk braces/semicolons, returning ``(node_stack, statement_text)`` pairs.

    ``node_stack`` is the list of enclosing node descriptors (outermost first);
    ``statement_text`` is a property assignment such as ``gpios = <&gpio0 25 ...>``.
    String literals are skipped so a ``;`` or ``{`` inside a string never splits.
    """
    stack: list[dict] = []
    out: list[tuple[list[dict], str]] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            buf.append(c)
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i])
                    i += 1
                buf.append(text[i])
                i += 1
            if i < n:  # closing quote
                buf.append(text[i])
                i += 1
            continue
        if c in "{};":
            chunk = "".join(buf).strip()
            buf = []
            if c == "{":
                stack.append(_parse_node_header(chunk))
            elif c == "}":
                if chunk:
                    out.append((list(stack), chunk))
                if stack:
                    stack.pop()
            else:  # ';'
                if chunk:
                    out.append((list(stack), chunk))
            i += 1
            continue
        buf.append(c)
        i += 1
    return out


def _node_name(stack: list[dict]) -> str:
    """Best human name for the innermost node (prefer label, else name)."""
    if not stack:
        return ""
    top = stack[-1]
    name = top["label"] or top["name"] or ""
    return name.lstrip("&")


def _nearest_label(stack: list[dict]) -> str:
    """Nearest labeled ancestor (used as the pinctrl *state* name)."""
    for node in reversed(stack):
        if node["label"]:
            return node["label"]
    return _node_name(stack)


def _signal_for_gpio(node: str, prop: str, index: int, count: int) -> str:
    """Derive a signal name from a GPIO property + its owning node.

    ``led_red`` + ``gpios`` -> ``led_red``; ``spi0`` + ``cs-gpios`` -> ``spi0_cs``;
    multiple entries in one property get a numeric suffix.
    """
    base = prop.strip()
    for suffix in _GPIO_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        if base in ("gpios", "gpio"):
            base = ""
    parts = [p for p in (node, base) if p]
    name = "_".join(parts) if parts else (node or prop)
    if count > 1:
        name = f"{name}_{index}"
    return name


# --- public API --------------------------------------------------------------

def parse_dts(path: str | Path) -> dict:
    """Parse a Zephyr DTS / overlay file into structured GPIO assignments.

    Args:
        path: path to a ``.dts`` / ``.overlay`` / ``.dtsi`` file.

    Returns:
        ``{"gpios": [...], "psels": [...]}`` where each entry is a dict with at
        least ``port`` (int), ``pin`` (int), ``gpio`` (``"P<port>.<pin>"``) and a
        derived ``signal`` (str). ``gpios`` entries also carry ``node``,
        ``property`` and ``flags``; ``psels`` entries carry ``function`` and the
        pinctrl ``state``.
    """
    text = _strip_comments(Path(path).read_text(encoding="utf-8", errors="replace"))
    gpios: list[dict] = []
    psels: list[dict] = []

    for stack, stmt in _scan_statements(text):
        prop, sep, value = stmt.partition("=")
        if not sep:  # boolean property or stray token -- nothing to map
            continue
        prop = prop.strip()
        node = _node_name(stack)

        gpio_hits = _GPIO_RE.findall(value)
        if gpio_hits and prop not in ("compatible", "label", "status"):
            for idx, (port_s, pin_s) in enumerate(gpio_hits):
                port, pin = int(port_s), int(pin_s, 0)
                gpios.append(
                    {
                        "node": node,
                        "property": prop,
                        "port": port,
                        "pin": pin,
                        "gpio": f"P{port}.{pin}",
                        "flags": value.strip(),
                        "signal": _signal_for_gpio(node, prop, idx, len(gpio_hits)),
                    }
                )

        state = _nearest_label(stack)
        for fn, port_s, pin_s in _PSEL_RE.findall(value):
            port, pin = int(port_s), int(pin_s)
            psels.append(
                {
                    "state": state,
                    "function": fn,
                    "port": port,
                    "pin": pin,
                    "gpio": f"P{port}.{pin}",
                    "signal": fn,
                }
            )

    return {"gpios": gpios, "psels": psels}


def to_expected_table(dts: dict) -> dict:
    """Flatten :func:`parse_dts` output into a ``{gpio: signal}`` expected table.

    The result is keyed by the canonical GPIO name (``"P<port>.<pin>"``) so it
    drops straight into :func:`checks.pinmap.run` as its ``expected`` argument
    (pinmap keys on pin number/name, values are signal names). First occurrence
    of a given GPIO wins; conflicting later assignments are ignored to keep the
    table deterministic.
    """
    table: dict[str, str] = {}
    for entry in (*dts.get("gpios", ()), *dts.get("psels", ())):
        gpio = entry.get("gpio")
        signal = entry.get("signal")
        if gpio and signal:
            table.setdefault(gpio, signal)
    return table
