"""``akcli doctor`` — one-shot environment report for humans, agents, and CI.

akcli itself is zero-dependency, but four optional capabilities depend on the
machine: **kicad-cli** (advisory ERC, SVG export for ``view live``, parity
tests), **libngspice** (``akcli sim`` execution — ``--deck-only`` needs
nothing), the **symbol libraries** (``place_component Device:R`` and
``akcli pins`` against standard nicknames), and **network** (the ``jlc``
family only). ``doctor`` probes each one the same way the features themselves
do, prints a table with a remediation hint per missing item, and — via
``--require`` — turns the report into a CI gate (exit 1 when a named
capability is absent).

Discovery mirrors the feature code paths on purpose: kicad-cli follows the
``KICAD_CLI`` env → ``PATH`` → known install locations ladder used by
``akcli view``; ngspice defers to :func:`akcli.sim.engine.available`
(``AKCLI_NGSPICE`` honored); symbols go through the very lib-table walk
``akcli pins`` uses; network is probed against the same base URL the ``jlc``
client uses (opt-in with ``--network`` — doctor stays offline by default like
the rest of the CLI).
"""

from __future__ import annotations

import argparse
import sys

from ._shared import _dumps, _emit, _gitignored, _stamp

# Capabilities a CI gate may demand via --require.
_REQUIRABLE = ("python", "kicad-cli", "ngspice", "config", "symbols", "network")

# The nickname:name probed by _check_symbols — the most universally present
# entry in KiCad's default symbol libraries.
_SYMBOL_PROBE = "Device:R"

def _find_kicad_cli() -> str | None:
    """Thin seam over the single shared ladder in :mod:`..drivers.kicad_cli`
    (kept as a module attribute so tests can monkeypatch doctor alone)."""
    from ..drivers import kicad_cli as _driver
    return _driver.find()


def _check_python() -> tuple[bool, str, str]:
    v = sys.version_info
    ok = v >= (3, 11)
    return (ok, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
            "" if ok else "akcli needs Python >= 3.11 (stdlib tomllib)")


def _check_akcli() -> tuple[bool, str, str]:
    from .. import __version__
    import akcli
    mode = "editable/source" if "site-packages" not in (akcli.__file__ or "") \
        else "installed wheel"
    return True, f"{__version__} ({mode})", ""


def _check_kicad_cli() -> tuple[bool, str, str]:
    path = _find_kicad_cli()
    if path is None:
        return (False, "not found",
                "install KiCad (kicad.org) or set KICAD_CLI=/path/to/kicad-cli "
                "— optional: enables advisory ERC, view-live SVG, parity tests")
    from ..drivers import kicad_cli as _driver
    reason = _driver.unsupported()
    if reason:
        return False, f"{path} (unsupported)", f"{reason}; upgrade from kicad.org"
    return True, path, ""


def _check_ngspice() -> tuple[bool, str, str]:
    from ..sim import engine
    path = engine.available()
    if path is None:
        return (False, "not found",
                "install KiCad (bundles libngspice) or set "
                "AKCLI_NGSPICE=/path/to/libngspice — optional: `akcli sim` "
                "runs need it; --deck-only works without")
    return True, str(path), ""


def _check_config() -> tuple[bool, str, str]:
    from .. import config as cfgmod
    found = cfgmod.find_config()
    if found is None:
        return (False, "no akcli.toml found (walking up from cwd)",
                "optional: only pinmap/rails/waivers/grid need one")
    return True, str(found), ""


def _check_schemas() -> tuple[bool, str, str]:
    try:
        from .. import ops
        n = len(ops.load_capabilities().get("ops", {}))
        return True, f"ops capabilities load ({n} ops)", ""
    except Exception as exc:  # broken install (missing package data)
        return (False, f"schema load failed: {exc}",
                "reinstall akcli — package data appears incomplete")


def _check_workspace() -> tuple[bool, str, str]:
    """Hygiene of the CURRENT directory as an akcli schematic workspace.

    Advisory (never CI-gating): flags legacy pre-0.12 beside-the-file backup
    stacks, leftover KiCad GUI lock files, and an untracked-state `.akcli/`
    that no reachable .gitignore excludes.
    """
    from pathlib import Path
    cwd = Path.cwd()
    issues: list[str] = []
    hints: list[str] = []

    legacy = sorted(p.name for p in cwd.glob("*.kicad_sch.bak*"))
    if legacy:
        issues.append(f"{len(legacy)} legacy beside-the-file backup(s) "
                      f"(e.g. {legacy[0]})")
        hints.append("new backups live in .akcli/backups/ — `akcli undo "
                     "--list` still finds the legacy stack; delete it once "
                     "that history is no longer needed")

    locks = sorted(p.name for p in cwd.glob("~*.lck"))
    if locks:
        issues.append(f"{len(locks)} KiCad GUI lock file(s) "
                      f"(e.g. {locks[0]})")
        hints.append("close KiCad, or delete the ~*.lck if no KiCad is "
                     "running (a stale lock makes writes demand --allow-open)")

    if (cwd / ".akcli").is_dir():
        # 0.14+ state roots self-ignore via .akcli/.gitignore; the walk-up
        # check only matters for legacy roots created before that.
        ignored = ((cwd / ".akcli" / ".gitignore").is_file()
                   or _gitignored(cwd, ".akcli"))
        if not ignored:
            issues.append(".akcli/ not covered by any .gitignore "
                          "(pre-0.14 state root)")
            hints.append("any akcli write command now drops a self-ignoring "
                         ".akcli/.gitignore; or add `.akcli/` to .gitignore — "
                         "journal and undo backups are derived state, not "
                         "design sources")

    # A directory of schematics with no .kicad_pro opens in KiCad as "no
    # project": no library tables, no board, and a prompt to create one. akcli
    # itself never needed the project file, which is exactly why this stayed
    # invisible until a human double-clicked the result.
    if any(cwd.glob("*.kicad_sch")) and not any(cwd.glob("*.kicad_pro")):
        issues.append("schematic(s) here but no .kicad_pro "
                      "(KiCad opens this as a project-less file)")
        hints.append("run `akcli project init .` to add the project file, "
                     "lib tables and a blank board around what is already here")

    if not issues:
        return True, "clean workspace", ""
    return False, "; ".join(issues), "; ".join(hints)


_SYMBOL_HINT = (
    "install the KiCad symbol libraries, or point akcli at them with "
    "`export KICAD10_SYMBOL_DIR=/path/to/symbols` (match the "
    "KICAD*_SYMBOL_DIR your sym-lib-table uses) — optional: only "
    "`akcli pins`/`place_component` against standard libraries need it"
)


def _check_symbols() -> tuple[bool, str, str]:
    """End-to-end symbol resolution through the library tables.

    Walks exactly the path ``akcli pins Device:R`` / ``place_component`` take:
    project + per-user global ``sym-lib-table`` -> ``${KICAD*_SYMBOL_DIR}``
    expansion (env, else KiCad's compiled-in default derived from the install)
    -> ``.kicad_sym`` load -> symbol lookup. A clean KiCad install exports none
    of those variables, which used to make every standard nickname a flat
    ``SYMBOL_NOT_FOUND``; this probe is how that failure becomes visible.
    """
    from pathlib import Path
    try:
        from ._shared import _table_symbol_sources
        sources = _table_symbol_sources([_SYMBOL_PROBE], [], Path.cwd())
    except Exception as exc:
        return (False, f"{_SYMBOL_PROBE}: lib-table lookup failed ({exc})",
                _SYMBOL_HINT)
    if not sources:
        return (False, f"{_SYMBOL_PROBE}: no sym-lib-table entry resolves to an "
                "existing .kicad_sym", _SYMBOL_HINT)
    where = Path(sources[0]).as_posix()
    try:
        from ..readers import kicad_lib
        kicad_lib.resolve(_SYMBOL_PROBE, [kicad_lib.read(s) for s in sources])
    except Exception as exc:
        return False, f"{_SYMBOL_PROBE}: {where} unusable ({exc})", _SYMBOL_HINT
    return True, f"{_SYMBOL_PROBE} via {where}", ""


def _check_network() -> tuple[bool, str, str]:
    import urllib.error
    import urllib.request
    from ..parts import search as parts_search
    url = parts_search.base_url()
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5):
            pass
        return True, url, ""
    except urllib.error.HTTPError as exc:
        # any HTTP answer proves reachability (the API may 4xx a bare HEAD)
        exc.close()     # an HTTPError is the open response itself: release it
        return True, f"{url} (HTTP {exc.code})", ""
    except Exception as exc:
        return (False, f"{url} unreachable: {exc}",
                "optional: only the `jlc` family is networked "
                "(AKCLI_JLC_BASE_URL overrides the endpoint)")


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, tuple[bool, str, str]] = {
        "python": _check_python(),
        "akcli": _check_akcli(),
        "schemas": _check_schemas(),
        "kicad-cli": _check_kicad_cli(),
        "ngspice": _check_ngspice(),
        "symbols": _check_symbols(),
        "config": _check_config(),
        "workspace": _check_workspace(),
    }
    if getattr(args, "network", False):
        checks["network"] = _check_network()

    required = []
    for item in (getattr(args, "require", None) or []):
        required.extend(p.strip() for p in item.split(",") if p.strip())
    unknown = [r for r in required if r not in _REQUIRABLE]
    if unknown:
        sys.stderr.write(
            f"ERROR: --require accepts {', '.join(_REQUIRABLE)}; "
            f"unknown: {', '.join(unknown)}\n")
        return 2
    if "network" in required and "network" not in checks:
        checks["network"] = _check_network()

    missing_required = [r for r in required if not checks[r][0]]

    if args.json:
        _emit(_dumps(_stamp({
            "checks": {k: {"ok": ok, "detail": detail,
                           **({"hint": hint} if hint else {})}
                       for k, (ok, detail, hint) in checks.items()},
            "required": required,
            "ok": not missing_required,
        })))
    else:
        _emit("# akcli doctor")
        for name, (ok, detail, hint) in checks.items():
            mark = "ok " if ok else "MISSING"
            _emit(f"  {mark:<8} {name:<10} {detail}")
            if hint and not ok:
                _emit(f"           {'':<10} hint: {hint}")
        if required:
            verdict = "all present" if not missing_required else \
                f"missing: {', '.join(missing_required)}"
            _emit(f"# required: {', '.join(required)} — {verdict}")

    return 1 if missing_required else 0


def register(sub, common) -> None:
    p = sub.add_parser(
        "doctor", parents=[common],
        help="environment report: python/kicad-cli/ngspice/symbols/config/"
             "network with remediation hints (--require gates CI)")
    p.add_argument("--network", action="store_true",
                   help="also probe the jlc endpoint (doctor is offline "
                        "by default)")
    p.add_argument("--require", action="append", metavar="CAPS",
                   help="comma-separated capabilities that must be present "
                        f"(exit 1 otherwise): {', '.join(_REQUIRABLE)}")
    p.set_defaults(handler=_cmd_doctor)
