"""Human ``pinout.md`` table -> expected pin->signal table (SPEC §3.8).

Parses a GitHub-flavored markdown table (by *header*, not by column position)
into an expected ``pin -> signal`` table for :func:`checks.pinmap.run`. This is
an **advisory / untrusted** source: ``pinout.md`` is maintained by hand and may
drift from the schematic, so a divergence is low-severity by design (pinmap
treats the schematic as authoritative).

Header detection is keyword-based and bilingual (English + Traditional/Simplified
Chinese), so a table whose columns are, e.g.::

    | GPIO  | 網路名 (net-name) | 韌體節點 (firmware-node) |
    |-------|------------------|--------------------------|
    | P0.25 | LED1_GPIO_RD     | led_red                  |

is read as ``{ "P0.25": "LED1_GPIO_RD" }`` -- the pin/GPIO column becomes the
key, the net/signal column the value. Column order does not matter; explicit
``key_header`` / ``value_header`` overrides are accepted when auto-detection is
ambiguous.

Zero third-party dependencies (stdlib only).
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["parse_pinout_md"]

# Header keywords identifying the *pin* (key) column -- a physical pin / GPIO ref.
_KEY_HINTS = (
    "gpio", "pin", "psel", "port", "腳位", "接腳", "針腳", "引腳", "pad",
)
# Header keywords identifying the *signal* (value) column -- a net / signal name.
_VALUE_HINTS = (
    "net", "signal", "node", "firmware", "function", "func",
    "網路", "訊號", "節點", "韌體", "功能", "信號",
)

_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _split_row(line: str) -> list[str]:
    """Split a markdown table row on unescaped ``|`` and strip the cells."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [c.replace("\\|", "|").strip() for c in cells]


def _is_separator(cells: list[str]) -> bool:
    """True for the ``|---|:--:|`` divider row beneath a table header."""
    non_empty = [c for c in cells if c]
    return bool(non_empty) and all(_SEP_CELL_RE.match(c) for c in non_empty)


def _match_header(headers: list[str], hints: tuple[str, ...]) -> int | None:
    """Index of the first header whose (lower-cased) text contains any hint."""
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(hint in hl for hint in hints):
            return i
    return None


def _find_index(headers: list[str], wanted: str | None) -> int | None:
    """Resolve an explicit header override (exact, then substring, case-insensitive)."""
    if wanted is None:
        return None
    want = wanted.strip().lower()
    for i, h in enumerate(headers):
        if h.strip().lower() == want:
            return i
    for i, h in enumerate(headers):
        if want in h.lower():
            return i
    return None


def parse_pinout_md(
    path: str | Path,
    *,
    key_header: str | None = None,
    value_header: str | None = None,
) -> dict:
    """Parse a markdown pinout table into an expected ``{pin: signal}`` table.

    Args:
        path: path to the markdown file.
        key_header: optional explicit header for the pin/GPIO (key) column.
            When omitted, the first header matching a pin/GPIO keyword is used,
            falling back to the first column.
        value_header: optional explicit header for the signal/net (value)
            column. When omitted, the first header matching a net/signal keyword
            (other than the key column) is used, falling back to the next column.

    Returns:
        ``{pin_key: signal}`` -- ready to pass to :func:`checks.pinmap.run` as
        its advisory ``expected`` argument. Empty if no usable table is found.
    """
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()

    # Locate the first markdown table: a row of '|' cells followed by a separator.
    headers: list[str] | None = None
    body_start = 0
    for i in range(len(lines) - 1):
        if "|" not in lines[i]:
            continue
        cells = _split_row(lines[i])
        if len(cells) < 2:
            continue
        nxt = _split_row(lines[i + 1])
        if _is_separator(nxt):
            headers = cells
            body_start = i + 2
            break

    if not headers:
        return {}

    key_idx = _find_index(headers, key_header)
    if key_idx is None:
        key_idx = _match_header(headers, _KEY_HINTS)
    if key_idx is None:
        key_idx = 0

    val_idx = _find_index(headers, value_header)
    if val_idx is None or val_idx == key_idx:
        val_idx = None
        for i, h in enumerate(headers):
            if i == key_idx:
                continue
            if any(hint in h.lower() for hint in _VALUE_HINTS):
                val_idx = i
                break
    if val_idx is None:
        val_idx = next((i for i in range(len(headers)) if i != key_idx), None)
    if val_idx is None:
        return {}

    table: dict[str, str] = {}
    for line in lines[body_start:]:
        if "|" not in line:
            if line.strip() == "":
                continue
            break  # table ended
        cells = _split_row(line)
        if _is_separator(cells):
            continue
        if max(key_idx, val_idx) >= len(cells):
            continue
        key = cells[key_idx].strip()
        val = cells[val_idx].strip()
        if not key or not val:
            continue
        table.setdefault(key, val)

    return table
