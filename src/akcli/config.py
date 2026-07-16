"""Discover, parse and validate ``akcli.toml`` (SPEC §3.10).

Discovery walks up from the cwd; ``-C/--config`` overrides. Paths in ``[paths]``
resolve relative to the TOML file's directory. Unknown keys are rejected with
``BAD_CONFIG`` so typos never silently disable a rule.

``[project] grid`` sets the schematic pin grid the net checks measure against:
a bare number is mils, a string carries its unit (``"50mil"``, ``"1.27mm"``,
``"0.5mm"`` — metric grids are first-class). Stored as exact integer
nanometres (``grid_nm``); default 50 mil.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import units
from .errors import fail

CONFIG_FILENAME = "akcli.toml"
# The tool was once named altium-kicad-cli; keep reading a legacy config file so
# an existing checkout does not silently lose its settings after the rename.
_LEGACY_CONFIG_FILENAME = "altium-kicad-cli.toml"

# Default schematic grid: 50 mil, in exact integer nanometres.
DEFAULT_GRID_NM: int = 50 * units.NM_PER_MIL

# Allowed top-level tables/keys and their allowed sub-keys.
_TOP_KEYS: frozenset[str] = frozenset(
    {"project", "rail", "paths", "erc_waiver", "waiver", "check"}
)
_CHECK_KEYS: frozenset[str] = frozenset(
    {"pairs", "pair_suffixes", "bus_min_family"}
)
_PROJECT_KEYS: frozenset[str] = frozenset({"mcu_designator", "grid", "backup_depth"})
_RAIL_KEYS: frozenset[str] = frozenset({"name", "voltage", "tolerance_pct"})
_WAIVER_KEYS: frozenset[str] = frozenset({"net", "rule", "reason"})
# Generic, checker-agnostic waiver (applied centrally, see report.apply_waivers).
_NEW_WAIVER_KEYS: frozenset[str] = frozenset({"code", "refs", "severity", "reason"})
_WAIVE_SEVERITIES: frozenset[str] = frozenset({"note", "info", "off"})


@dataclass
class Config:
    """Parsed project configuration.

    ``rails``, ``erc_waivers`` and ``waivers`` are lists of plain dicts; ``paths``
    maps a name (``schematic``/``dts``/``pinout_md``/...) to an absolute path
    string. ``grid_nm`` is the schematic pin grid in integer nanometres.
    ``waivers`` are the generic ``[[waiver]]`` entries applied centrally to every
    checker's findings (:func:`report.apply_waivers`); ``erc_waivers`` is the
    older ERC-only mechanism, kept as a back-compat alias.
    """

    mcu_designator: str | None = None
    rails: list[dict] = field(default_factory=list)
    paths: dict[str, str] = field(default_factory=dict)
    erc_waivers: list[dict] = field(default_factory=list)
    waivers: list[dict] = field(default_factory=list)
    source_path: str | None = None
    grid_nm: int = DEFAULT_GRID_NM
    backup_depth: int | None = None    # [project] backup_depth; None = writer default
    check: dict = field(default_factory=dict)  # [check] table (pairs, pair_suffixes, ...)


def _parse_grid(value: object) -> int:
    """``[project].grid`` -> integer nm. Number = mils; string carries a unit."""
    if isinstance(value, bool):
        fail("BAD_CONFIG", "[project].grid must be a number (mils) or 'Nmil'/'Nmm'")
    if isinstance(value, (int, float)):
        nm = units.mil_to_nm(float(value))
    elif isinstance(value, str):
        s = value.strip().lower()
        try:
            if s.endswith("mil"):
                nm = units.mil_to_nm(float(s[:-3]))
            elif s.endswith("mm"):
                nm = units.mm_to_nm(float(s[:-2]))
            else:
                nm = units.mil_to_nm(float(s))
        except ValueError:
            fail("BAD_CONFIG", f"[project].grid: cannot parse {value!r}")
    else:
        fail("BAD_CONFIG", "[project].grid must be a number (mils) or 'Nmil'/'Nmm'")
    if nm <= 0:
        fail("BAD_CONFIG", f"[project].grid must be positive, got {value!r}")
    return nm


def find_config(start: Path | str | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) returning the first config file found."""
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    if here.is_file():
        here = here.parent
    for d in [here, *here.parents]:
        for name in (CONFIG_FILENAME, _LEGACY_CONFIG_FILENAME):
            candidate = d / name
            if candidate.is_file():
                return candidate
    return None


def _reject_unknown(table: dict, allowed: frozenset[str], where: str) -> None:
    extra = set(table) - allowed
    if extra:
        fail("BAD_CONFIG", f"unknown key(s) in {where}: {', '.join(sorted(extra))}")


def load_config(path: Path | str) -> Config:
    """Parse and validate a config file; unknown keys -> ``BAD_CONFIG``."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        fail("BAD_CONFIG", f"invalid TOML in {p}: {exc}")

    _reject_unknown(data, _TOP_KEYS, "top level")

    project = data.get("project", {})
    if not isinstance(project, dict):
        fail("BAD_CONFIG", "[project] must be a table")
    _reject_unknown(project, _PROJECT_KEYS, "[project]")

    rails: list[dict] = []
    for r in data.get("rail", []) or []:
        if not isinstance(r, dict):
            fail("BAD_CONFIG", "[[rail]] entries must be tables")
        _reject_unknown(r, _RAIL_KEYS, "[[rail]]")
        rails.append(dict(r))

    waivers: list[dict] = []
    for w in data.get("erc_waiver", []) or []:
        if not isinstance(w, dict):
            fail("BAD_CONFIG", "[[erc_waiver]] entries must be tables")
        _reject_unknown(w, _WAIVER_KEYS, "[[erc_waiver]]")
        waivers.append(dict(w))

    gen_waivers: list[dict] = []
    for w in data.get("waiver", []) or []:
        if not isinstance(w, dict):
            fail("BAD_CONFIG", "[[waiver]] entries must be tables")
        _reject_unknown(w, _NEW_WAIVER_KEYS, "[[waiver]]")
        code = w.get("code")
        if not code or not isinstance(code, str):
            fail("BAD_CONFIG", "[[waiver]].code is required (a finding code or fnmatch glob)")
        refs = w.get("refs")
        if refs is not None and not (
            isinstance(refs, str)
            or (isinstance(refs, list) and all(isinstance(r, str) for r in refs))
        ):
            fail("BAD_CONFIG", "[[waiver]].refs must be a string or list of strings")
        sev = w.get("severity", "off")
        if not isinstance(sev, str) or sev.lower() not in _WAIVE_SEVERITIES:
            fail("BAD_CONFIG", "[[waiver]].severity must be one of note/info/off")
        gen_waivers.append({**w, "severity": sev.lower()})

    raw_paths = data.get("paths", {})
    if not isinstance(raw_paths, dict):
        fail("BAD_CONFIG", "[paths] must be a table")
    base = p.resolve().parent
    paths: dict[str, str] = {}
    for key, val in raw_paths.items():
        if not isinstance(val, str):
            fail("BAD_CONFIG", f"[paths].{key} must be a string")
        paths[key] = str((base / val).resolve())

    grid_nm = DEFAULT_GRID_NM
    if "grid" in project:
        grid_nm = _parse_grid(project["grid"])

    backup_depth = None
    if "backup_depth" in project:
        raw_depth = project["backup_depth"]
        if not isinstance(raw_depth, int) or isinstance(raw_depth, bool) \
                or not (1 <= raw_depth <= 99):
            fail("BAD_CONFIG",
                 "[project] backup_depth must be an integer 1..99")
        backup_depth = raw_depth

    check_tbl = data.get("check", {})
    if not isinstance(check_tbl, dict):
        fail("BAD_CONFIG", "[check] must be a table")
    _reject_unknown(check_tbl, _CHECK_KEYS, "[check]")
    if "pairs" in check_tbl and not isinstance(check_tbl["pairs"], bool):
        fail("BAD_CONFIG", "[check].pairs must be a boolean")
    if "pair_suffixes" in check_tbl:
        ps = check_tbl["pair_suffixes"]
        if (not isinstance(ps, list)
                or any(not (isinstance(pair, list) and len(pair) == 2
                            and all(isinstance(s, str) and s for s in pair))
                       for pair in ps)):
            fail("BAD_CONFIG",
                 "[check].pair_suffixes must be a list of [\"_P\", \"_N\"]-style"
                 " two-string pairs")
    if "bus_min_family" in check_tbl:
        bmf = check_tbl["bus_min_family"]
        if not isinstance(bmf, int) or isinstance(bmf, bool) or bmf < 2:
            fail("BAD_CONFIG", "[check].bus_min_family must be an integer >= 2")

    return Config(
        mcu_designator=project.get("mcu_designator"),
        rails=rails,
        paths=paths,
        erc_waivers=waivers,
        waivers=gen_waivers,
        source_path=str(p.resolve()),
        grid_nm=grid_nm,
        backup_depth=backup_depth,
        check=dict(check_tbl),
    )
