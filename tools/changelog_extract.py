#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a given version to stdout.

Usage:
    python tools/changelog_extract.py [--strict] <version>

Looks for a ``## [<version>]`` heading; falls back to ``## [Unreleased]``; then to a
one-line default. Used by the release workflow to build the GitHub Release notes.

``--strict`` disables both fallbacks and exits non-zero when the exact
``## [<version>]`` section is missing or empty. The release workflow uses it:
without it, forgetting to move ``[Unreleased]`` to ``[X.Y.Z]`` silently ships the
*next* version's unreleased notes as the release body, exit 0, no warning.
Stdlib-only, no dependencies.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

# Markdown link-reference definitions, e.g. ``[Unreleased]: https://...`` — kept out
# of the rendered release notes.
_LINK_DEF = re.compile(r"^\[[^\]]+\]:\s+\S")


def _section(lines: list[str], prefixes: list[str]) -> str | None:
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if any(low.startswith(p) for p in prefixes):
            out: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                if _LINK_DEF.match(nxt):
                    continue
                out.append(nxt)
            body = "\n".join(out).strip()
            if body:
                return body
    return None


def extract(text: str, version: str, *, strict: bool = False) -> str | None:
    """Return the changelog body for *version*.

    Non-strict: falls back to the ``Unreleased`` section, then to a one-liner.
    Strict: returns ``None`` when the exact version section is missing/empty.
    """
    lines = text.splitlines()
    exact = _section(lines, [f"## [{version.lower()}]"])
    if strict:
        return exact
    return exact or _section(lines, ["## [unreleased]"]) or f"Release {version}."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version", help="version to extract, without the leading 'v'")
    ap.add_argument("--strict", action="store_true",
                    help="fail instead of falling back to [Unreleased] / a generated one-liner")
    args = ap.parse_args(argv)

    changelog = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8") if changelog.exists() else ""
    body = extract(text, args.version, strict=args.strict)
    if body is None:
        print(
            f"changelog_extract: no '## [{args.version}]' section with content in "
            f"{changelog} — move the [Unreleased] section to [{args.version}] before releasing.",
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
