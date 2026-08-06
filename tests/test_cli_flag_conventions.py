"""CLI flag-convention census (field-hardening).

Every subcommand that writes a deliverable via ``--out`` must also accept the
``-o`` short alias — the surface had drifted into three conventions (bare
``--out``, ``-o/--out``, ``-o/--output``) and an agent flipping between
subcommands kept guessing wrong. This census walks the real parser tree so a
new ``--out`` flag without the alias fails here, not in the field.

``--output`` spellings (``check expected`` / ``render`` / ``export``) are
GRANDFATHERED: renaming them to ``--out`` would break existing scripts, so
they are pinned as a documented exception rather than silently multiplying.
"""

from __future__ import annotations

import argparse

from akcli.cli import build_parser


def _walk(parser: argparse.ArgumentParser):
    """Yield (name, parser) for every subparser, recursively."""
    for action in parser._actions:  # noqa: SLF001 — introspection is the point
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                yield name, sub
                yield from _walk(sub)


def test_every_out_flag_has_short_alias():
    missing = []
    for name, sub in _walk(build_parser()):
        for action in sub._actions:  # noqa: SLF001
            if "--out" in action.option_strings and "-o" not in action.option_strings:
                missing.append(name)
    assert missing == [], (
        f"subcommands with --out but no -o alias: {sorted(set(missing))}"
    )


def test_output_spelling_does_not_spread():
    # The three legacy --output flags are pinned; new deliverable flags must
    # spell --out (with -o), so this list may only ever SHRINK.
    grandfathered = {"expected", "render", "export"}
    found = set()
    for name, sub in _walk(build_parser()):
        for action in sub._actions:  # noqa: SLF001
            if "--output" in action.option_strings:
                found.add(name)
    assert found <= grandfathered, (
        f"new --output spellings (use -o/--out instead): "
        f"{sorted(found - grandfathered)}"
    )
