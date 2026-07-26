"""Documentation-conformance gate — the anti-drift CI net.

Two static gates over the human-facing docs (``README.md``, ``INSTALL.md``,
``ROADMAP.md``, the two localized READMEs, ``docs/*.md``, ``skills/*/SKILL.md``
and the plugin slash commands ``commands/*.md``); neither runs a handler,
touches the network, or starts an engine:

* **Fence gate.** Every fenced ``akcli …`` command line is shlex-split (angle
  ``<placeholders>`` and ``$VARS`` first swapped for dummies) and checked
  against a live ``build_parser()``: the subcommand path must exist and every
  ``--flag`` must be a registered option of that subparser. A line whose fenced
  text is illustrative rather than runnable (a usage synopsis, a pyproject
  snippet) opts out with a trailing ``# doc-noqa``.
* **Count gate.** Wherever the docs claim "N ops", "N macros" or "N
  calculators" (and the zh ``N 種 op`` / ``N 個離線計算器`` equivalents), N must
  equal the live registry (``ops.OP_NAMES`` / ``ops.MACRO_OPS`` / the ``calc``
  ``CALCS`` table).

If a gate fires, the matcher is the first suspect — the docs were audited clean
— so widen/narrow the regex before touching a doc; only edit a doc line when it
is genuine drift.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# file set (shared by both gates)
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[1]


def _doc_files() -> list[Path]:
    files = [_ROOT / "README.md", _ROOT / "INSTALL.md", _ROOT / "ROADMAP.md"]
    files += sorted((_ROOT / ".github").glob("README.*.md"))
    files += sorted((_ROOT / "docs").glob("*.md"))
    files += sorted((_ROOT / "skills").glob("*/SKILL.md"))
    files += sorted((_ROOT / "commands").glob("*.md"))
    return [f for f in files if f.is_file()]


def _fenced_lines(path: Path) -> list[tuple[int, str]]:
    """``(lineno, text)`` for every line inside a ``````` fence."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append((i, raw))
    return out


# --------------------------------------------------------------------------- #
# fence gate
# --------------------------------------------------------------------------- #
# Angle placeholders (<sch>, <C-number>, <file_a>) and $VARS are swapped for a
# neutral token so shlex sees a plain word, never an empty/odd argument.
_PLACEHOLDER = re.compile(r"<[^>]+>")
_VAR = re.compile(r"\$\{?\w+\}?")


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _resolve(
    parser: argparse.ArgumentParser, tokens: list[str], path: list[str]
) -> tuple[argparse.ArgumentParser, list[str], str | None]:
    """Descend into subparsers by matching the first non-flag token at each level.

    Returns the deepest resolved parser, its command path, and an error string
    (unknown subcommand) or ``None``.
    """
    sub = _subparsers_action(parser)
    if sub is None:
        return parser, path, None
    for idx, tok in enumerate(tokens):
        if tok.startswith("-"):
            continue  # a global flag (or its value) before the subcommand
        if tok in sub.choices:
            return _resolve(sub.choices[tok], tokens[idx + 1 :], [*path, tok])
        return parser, path, f"unknown subcommand {tok!r}"
    # no positional token at all (e.g. ``akcli --version``): stay put
    return parser, path, None


def _flag_tokens(tokens: list[str]) -> list[str]:
    """The option-looking tokens (``--foo``, ``--foo=bar``, ``-o``), normalized.

    A bare ``-`` (stdin) and negative numbers are not options.
    """
    flags: list[str] = []
    for tok in tokens:
        if not tok.startswith("-") or tok == "-":
            continue
        if re.fullmatch(r"-\d.*", tok):  # negative number, not a flag
            continue
        flags.append(tok.split("=", 1)[0])
    return flags


def _fence_cases() -> list[tuple[str, int, str]]:
    cases: list[tuple[str, int, str]] = []
    for path in _doc_files():
        rel = path.relative_to(_ROOT).as_posix()
        for lineno, raw in _fenced_lines(path):
            text = raw.strip()
            if not text.startswith("akcli "):
                continue
            if "doc-noqa" in raw:
                continue
            cases.append((rel, lineno, raw))
    return cases


@pytest.mark.parametrize(
    "rel,lineno,raw", _fence_cases(), ids=lambda v: v if isinstance(v, str) else str(v)
)
def test_fenced_commands_are_valid(rel: str, lineno: int, raw: str) -> None:
    from akcli.cli import build_parser

    parser = build_parser()
    where = f"{rel}:{lineno}"

    # Substitute placeholders, drop a trailing line-continuation backslash.
    text = _VAR.sub("X", _PLACEHOLDER.sub("x", raw.strip()))
    text = re.sub(r"\\\s*$", "", text)

    try:
        tokens = shlex.split(text, comments=True)
    except ValueError as exc:  # pragma: no cover - would be a genuine doc break
        pytest.fail(f"{where}: cannot shlex-split fenced command ({exc}): {text!r}")

    assert tokens and tokens[0] == "akcli", f"{where}: not an akcli command: {text!r}"
    body = tokens[1:]

    resolved, cmd_path, err = _resolve(parser, body, [])
    assert err is None, f"{where}: {err} (in {text!r})"

    valid = _option_strings(resolved)
    cmd = "akcli " + " ".join(cmd_path) if cmd_path else "akcli"
    for flag in _flag_tokens(body):
        assert flag in valid, (
            f"{where}: `{cmd}` has no option {flag!r} "
            f"(fenced line: {raw.strip()!r})"
        )


# --------------------------------------------------------------------------- #
# count gate
# --------------------------------------------------------------------------- #
def _registry_counts() -> dict[str, int]:
    from akcli import ops
    from akcli.calc import CALCS

    return {
        "ops": len(ops.OP_NAMES),
        "macros": len(ops.MACRO_OPS),
        "calculators": len(CALCS),
    }


# Each entry: (kind, compiled regex whose group(1) is the claimed integer).
# Deliberately tight so only genuine "N <noun>" claims are captured:
#   * the number must be its own token (no leading digit/dot -> skips "v0.2 ops")
#   * the noun must be a standalone word (no "-list" -> skips "op-list")
_COUNT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # English
    ("ops", re.compile(r"(?<![\w.])(\d+)[-\s]ops?(?![\w-])")),
    ("macros", re.compile(r"(?<![\w.])(\d+)[-\s]macros?(?![\w-])")),
    ("calculators", re.compile(r"(?<![\w.])(\d+)\s+(?:[a-z-]+\s+){0,3}calculators?\b")),
    ("calculators", re.compile(r"(?<![\w.])(\d+)\s+design questions\b")),
    # zh (Hant/Hans share the CJK forms below)
    ("ops", re.compile(r"(\d+)\s*[種种]\s*op(?![\w-])")),
    ("macros", re.compile(r"(\d+)\s*[種种]\s*(?:巨集|宏)")),
    ("calculators", re.compile(r"(\d+)\s*[個个種种][一-鿿]{0,10}(?:計算|计算)")),
    # ja — the katakana/kana forms the zh patterns cannot reach ("10 マクロ",
    # "60 個の規格準拠計算機"; the bare "22 op" is already caught in English).
    ("macros", re.compile(r"(\d+)\s*(?:個\s*の\s*)?マクロ")),
    ("calculators", re.compile(r"(\d+)\s*個の[ぁ-んァ-ヴ一-鿿]{0,12}計算機")),
]


def _count_cases() -> list[tuple[str, int, str, int]]:
    """``(rel, lineno, kind, claimed)`` for every count claim in the file set.

    Emphasis markers are stripped first: the patterns want the noun to follow
    the digits across whitespace, so a bolded ``**60** calculators`` (which is
    how every README writes its `calc` table row) was invisible to the gate.
    """
    cases: list[tuple[str, int, str, int]] = []
    for path in _doc_files():
        rel = path.relative_to(_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            plain = line.replace("**", "").replace("__", "")
            for kind, pat in _COUNT_PATTERNS:
                for m in pat.finditer(plain):
                    cases.append((rel, lineno, kind, int(m.group(1))))
    return cases


def test_count_gate_found_claims() -> None:
    """Guard the guard: if this drops to zero the matchers silently rotted."""
    assert len(_count_cases()) >= 15


@pytest.mark.parametrize(
    "rel,lineno,kind,claimed",
    _count_cases(),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_documented_counts_match_registry(
    rel: str, lineno: int, kind: str, claimed: int
) -> None:
    expected = _registry_counts()[kind]
    assert claimed == expected, (
        f"{rel}:{lineno}: doc claims {claimed} {kind}, "
        f"registry has {expected}"
    )


# --------------------------------------------------------------------------- #
# config-surface gate: every accepted top-level akcli.toml table must be
# documented in SPEC §3.11 AND exercised by examples/akcli.toml.example —
# a new config table cannot ship undocumented.
# --------------------------------------------------------------------------- #
def _toml_tables(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"\s*\[\[?([a-z_]+)", line)
        if m:
            out.add(m.group(1))
    return out


def test_config_tables_documented_in_spec_and_example() -> None:
    from akcli.config import _TOP_KEYS, load_config

    spec = (_ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    m = re.search(r"### 3\.11 Config schema.*?```toml\n(.*?)```", spec, re.S)
    assert m, "SPEC.md lost its §3.11 config schema toml block"
    spec_tables = _toml_tables(m.group(1))

    example_path = _ROOT / "examples" / "akcli.toml.example"
    example_tables = _toml_tables(example_path.read_text(encoding="utf-8"))

    missing_spec = set(_TOP_KEYS) - spec_tables
    missing_example = set(_TOP_KEYS) - example_tables
    assert not missing_spec, \
        f"config tables absent from SPEC §3.11: {sorted(missing_spec)}"
    assert not missing_example, \
        f"config tables absent from examples/akcli.toml.example: {sorted(missing_example)}"
    # and the example must actually parse under the real validator
    load_config(example_path)


# --------------------------------------------------------------------------- #
# schema-table gate: every published schemas/*.json appears in SPEC §3.12.
# --------------------------------------------------------------------------- #
def test_schema_files_listed_in_spec() -> None:
    spec = (_ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    m = re.search(r"### 3\.12 Schemas.*?(?=\n## |\n---)", spec, re.S)
    assert m, "SPEC.md lost its §3.12 schema section"
    section = m.group(0)
    published = sorted(f.name for f in (_ROOT / "schemas").glob("*.json"))
    missing = [name for name in published if f"`{name}`" not in section]
    assert not missing, f"schemas absent from SPEC §3.12 table: {missing}"


# --------------------------------------------------------------------------- #
# release-notes gate: the shipped version has its own CHANGELOG section.
#
# The release workflow renders `## [X.Y.Z]` into the GitHub Release body
# (`tools/changelog_extract.py --strict`). Bumping pyproject without moving
# `[Unreleased]` used to be invisible until the notes were already published.
# --------------------------------------------------------------------------- #
def test_pyproject_version_has_a_changelog_section() -> None:
    import tomllib

    with (_ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]

    text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [ln.strip() for ln in text.splitlines() if ln.startswith("## ")]
    wanted = f"## [{version}]"
    assert any(h.startswith(wanted) for h in headings), (
        f"CHANGELOG.md has no '{wanted}' heading for the pyproject version "
        f"{version} (headings: {headings[:6]}). Move the [Unreleased] section "
        f"to [{version}] before tagging — the release workflow extracts that "
        "exact section as the GitHub Release body.")

    # ...and it must be non-empty, which is what --strict actually enforces.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "changelog_extract", _ROOT / "tools" / "changelog_extract.py")
    assert spec and spec.loader
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    body = ce.extract(text, version, strict=True)
    assert body, f"CHANGELOG.md '{wanted}' section is empty"
