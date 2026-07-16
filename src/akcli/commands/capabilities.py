"""`akcli capabilities` — one machine-readable manifest of the whole CLI surface.

The single root document an agent reads to drive the tool blind: every
subcommand + flag (introspected from the live ``argparse`` parser, so it cannot
drift from reality), the frozen exit-code and error-code tables, the op-list
vocabulary, the calculator registry, and the packaged JSON Schemas with their
version fields. Environment probing stays in ``akcli doctor``; this command is
pure static surface description and never touches the filesystem outside the
package's own resources.
"""

from __future__ import annotations

import argparse
import json

from ..errors import ERROR_CODES, EXIT, exit_for_code
from ._shared import _dumps, _emit

CAPABILITIES_VERSION = "1.0"

# Option strings owned by the shared global-flags parent (reported once under
# ``global_flags``, filtered out of every per-command flag list).
_GLOBAL_OPTS = {"-C", "--config", "-v", "--verbose", "-q", "--quiet",
                "--json", "--no-color", "--debug"}

_EXIT_MEANING = {
    "OK": "success / no findings",
    "FINDINGS": "check findings present (lint-style; tune with --fail-on)",
    "USAGE": "usage / argument / config error",
    "PARSE": "parse error (corrupt OLE2 or S-expression)",
    "NOT_FOUND": "file not found",
    "UNSUPPORTED_FORMAT": "unsupported format",
    "OPLIST": "op-list or verify failure",
    "TOOL_MISSING": "required external tool missing or network failure",
    "QUERY_MISS": "named net/component does not exist in the (valid) file",
}


def _json_safe(value: object) -> object | None:
    """Defaults worth reporting; anything exotic (or SUPPRESS) becomes None."""
    if value is argparse.SUPPRESS or value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, (str, int, float, bool))]
    return None


def _describe_action(action: argparse.Action) -> dict:
    entry: dict = {}
    if action.option_strings:
        entry["flags"] = list(action.option_strings)
    else:
        entry["name"] = action.dest
        entry["required"] = action.nargs not in ("?", "*")
    if action.help and action.help is not argparse.SUPPRESS:
        entry["help"] = " ".join(str(action.help).split())
    if action.choices is not None:
        entry["choices"] = [str(c) for c in action.choices]
    if action.metavar:
        mv = action.metavar
        entry["metavar"] = list(mv) if isinstance(mv, tuple) else mv
    # nargs == 0 covers store_true/store_false/count: the flag takes no value.
    entry["takes_value"] = action.nargs != 0
    default = _json_safe(action.default)
    if default is not None:
        entry["default"] = default
    if getattr(action, "required", False) and action.option_strings:
        entry["required"] = True
    return entry


def _describe_parser(parser: argparse.ArgumentParser) -> tuple[list, list, list]:
    """Return ``(positionals, flags, subcommands)`` for one (sub)parser."""
    positionals: list = []
    flags: list = []
    subcommands: list = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public walk API
        if isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
            continue
        if isinstance(action, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in action._choices_actions}
            for name, sub in action.choices.items():
                subcommands.append(_describe_command(name, sub, helps.get(name)))
            continue
        if action.option_strings:
            if set(action.option_strings) & _GLOBAL_OPTS:
                continue  # reported once under global_flags
            flags.append(_describe_action(action))
        else:
            positionals.append(_describe_action(action))
    return positionals, flags, subcommands


def _describe_command(name: str, parser: argparse.ArgumentParser,
                      help_text: str | None) -> dict:
    positionals, flags, subcommands = _describe_parser(parser)
    entry: dict = {"name": name}
    if help_text:
        entry["help"] = " ".join(str(help_text).split())
    if positionals:
        entry["positionals"] = positionals
    if flags:
        entry["flags"] = flags
    if subcommands:
        entry["subcommands"] = subcommands
    return entry


def _schema_inventory() -> list[dict]:
    """Packaged JSON Schemas with their declared version fields."""
    from importlib import resources

    out: list[dict] = []
    root = resources.files("akcli.schemas")
    for res in sorted(root.iterdir(), key=lambda r: r.name):
        if not res.name.endswith(".json"):
            continue
        try:
            doc = json.loads(res.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - packaged data
            continue
        entry: dict = {"name": res.name}
        if isinstance(doc, dict):
            if doc.get("title"):
                entry["title"] = doc["title"]
            for field in ("protocol_version", "proposals_version", "facts_version"):
                if field in doc:
                    entry["version_field"] = field
                    entry["version"] = doc[field]
                    break
            else:
                props = doc.get("properties", {})
                sv = props.get("schema_version", {})
                if "const" in sv:
                    entry["version_field"] = "schema_version"
                    entry["version"] = sv["const"]
                elif sv:
                    entry["version_field"] = "schema_version"
                    entry["version"] = "additive (no const pin)"
        out.append(entry)
    return out


def build_manifest() -> dict:
    """The full capabilities document (also the seam tests introspect)."""
    from .. import __version__
    from .. import cli as _cli   # lazy: cli imports this module at registration
    from .. import ops as _ops
    from ..calc import CALCS

    parser = _cli.build_parser()
    _, top_flags, commands = _describe_parser(parser)
    # The shared parent's flags are filtered out of every per-command list;
    # describe them once from the parent parser itself (+ top-level --version).
    global_flags = [
        _describe_action(a) for a in _cli._global_flags()._actions  # noqa: SLF001
        if not isinstance(a, argparse._HelpAction)
    ] + top_flags

    macro_names = sorted(_ops.MACRO_OPS)
    core_names = sorted(_ops._CORE_OPS)   # noqa: SLF001 - single source of truth
    sugar_names = sorted(_ops._SUGAR_OPS)  # noqa: SLF001

    return {
        "schema_version": CAPABILITIES_VERSION,
        "akcli_version": __version__,
        "protocol_version": _ops.PROTOCOL_VERSION,
        "exit_codes": [
            {"code": code, "name": name,
             "meaning": _EXIT_MEANING.get(name, name.lower())}
            for name, code in sorted(EXIT.items(), key=lambda kv: kv[1])
        ],
        "error_codes": [
            {"code": code, "exit": exit_for_code(code)}
            for code in sorted(ERROR_CODES)
        ],
        "global_flags": global_flags,
        "commands": commands,
        "ops": {
            "protocol_version": _ops.PROTOCOL_VERSION,
            "core": core_names,
            "sugar": sugar_names,
            "macros": macro_names,
            "capabilities": _ops.load_capabilities().get("ops", {}),
            # Hard vocabulary limits, published here so an agent can branch
            # BEFORE attempting an op instead of learning each constraint
            # from a failed draw (BAD_ANGLE / NON_ORTHOGONAL_WIRE /
            # HIERARCHICAL_UNSUPPORTED / OFF_GRID). Values come from ops.py's
            # own validator constants — the manifest cannot drift from what
            # the validator actually enforces.
            "constraints": {
                "rotation_enum": sorted(_ops._VALID_ROTATIONS),  # noqa: SLF001
                "wire_orthogonal_only": True,
                "grid_mil": int(_ops._GRID_MIL),  # noqa: SLF001
                "hierarchy": "flat_v1_only",
                "hierarchy_note": "ops cannot target a child sheet; add_sheet "
                                  "creates the sheet symbol, but the child "
                                  ".kicad_sch must be authored as its own "
                                  "draw target",
            },
            # HONESTY: the per-op "altium" support matrix describes the
            # (experimental, Windows-only) live-bridge executor, which has NO
            # CLI wiring today — plan/draw always build target_format "kicad".
            # Read from ops.capabilities.json (the single source of truth) so
            # wiring the bridge up cannot leave this manifest stale.
            "altium_live_wired": bool(
                _ops.load_capabilities().get("altium_live_wired", False)),
        },
        "calculators": {"count": len(CALCS), "names": sorted(CALCS)},
        "schemas": _schema_inventory(),
        "conventions": {
            "stdout": "data", "stderr": "logs",
            "json_flag": "--json is accepted by every subcommand",
            "dry_run": "plan never writes; draw writes only with --apply",
            "version_stamps": "every JSON object payload carries "
                              "schema_version or a family version field "
                              "(protocol_version, journal_version); the "
                              "documented exceptions are array payloads "
                              "(net, jlc search, calc batch) and name-keyed "
                              "tables (calc list, expected)",
            "json_errors": "with --json, a failing command that would "
                           "otherwise leave stdout empty emits "
                           '{"error": {code, message, exit, remediation}} '
                           "on stdout; plan/draw op-list errors emit the "
                           "normal draw-result shape with status refused",
        },
    }


def _cmd_capabilities(args: argparse.Namespace) -> int:
    manifest = build_manifest()
    if args.json:
        _emit(_dumps(manifest))
        return EXIT["OK"]

    lines = [
        f"akcli {manifest['akcli_version']} (protocol {manifest['protocol_version']}, "
        f"capabilities {manifest['schema_version']})",
        "",
        f"commands ({len(manifest['commands'])}):",
    ]
    for cmd in manifest["commands"]:
        subs = ""
        if cmd.get("subcommands"):
            subs = "  [" + " ".join(s["name"] for s in cmd["subcommands"]) + "]"
        lines.append(f"  {cmd['name']:<16} {cmd.get('help', '')}{subs}")
    ops = manifest["ops"]
    lines += [
        "",
        "exit codes: " + " · ".join(
            f"{e['code']} {e['name']}" for e in manifest["exit_codes"]),
        f"ops: {len(ops['core'])} core + {len(ops['sugar'])} sugar "
        f"+ {len(ops['macros'])} macros (protocol {ops['protocol_version']})",
        f"calculators: {manifest['calculators']['count']}",
        "schemas: " + ", ".join(s["name"] for s in manifest["schemas"]),
        "",
        "full manifest: akcli capabilities --json",
    ]
    _emit("\n".join(lines))
    return EXIT["OK"]


def register(sub, common) -> None:
    p = sub.add_parser(
        "capabilities", parents=[common],
        help="print the full machine-readable CLI surface manifest")
    p.set_defaults(handler=_cmd_capabilities)
