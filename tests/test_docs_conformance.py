"""Documentation-conformance gate — the anti-drift CI net.

Static gates over the human-facing docs (``README.md``, ``INSTALL.md``,
``ROADMAP.md``, the localized READMEs, ``docs/**/*.md`` — recursive, so
``docs/recipes/`` is covered; ``docs/assets/`` is not documentation and stays
out — ``skills/*/SKILL.md`` and the plugin slash commands ``commands/*.md``);
none runs a handler, touches the network, or starts an engine:

* **Fence gate.** Every fenced ``akcli …`` command line is shlex-split (angle
  ``<placeholders>`` and ``$VARS`` first swapped for dummies) and checked
  against a live ``build_parser()``: the subcommand path must exist and every
  ``--flag`` must be a registered option of that subparser. A line whose fenced
  text is illustrative rather than runnable (a usage synopsis, a pyproject
  snippet) opts out with a trailing ``# doc-noqa``.
* **Count gate.** Wherever the docs claim "N ops", "N macros" or "N
  calculators" (and the zh ``N 種 op`` / ``N 個離線計算器`` equivalents), N must
  equal the live registry (``ops.OP_NAMES`` / ``ops.MACRO_OPS`` / the ``calc``
  ``CALCS`` table). The same holds for "N skills" / "N commands" (file
  counts), "N schemas" (files in ``schemas/`` — every one of them is a name
  ``akcli schema`` serves, so ``ops.capabilities.json`` counts), "N rules"
  (the review rule registry) and "N … eval/design tasks" (task directories
  under ``tools/agent_eval/tasks/``). Only DIGIT claims are gated: a word-form
  count ("fourteen schemas") is invisible, so write counts in digits.
* **Recipe gate.** A workflow under ``docs/recipes/`` runs ``akcli`` from YAML
  ``run:`` lines the fence gate cannot see (``run: akcli …`` does not start
  with ``akcli``). Each recipe therefore repeats its commands as bare lines in
  a fenced ``bash`` block — which the fence gate validates — and every
  ``akcli`` invocation in the recipe's YAML must appear in that block.

If a gate fires, the matcher is the first suspect — the docs were audited clean
— so widen/narrow the regex before touching a doc; only edit a doc line when it
is genuine drift.
"""

from __future__ import annotations

import argparse
import functools
import re
import shlex
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# file set (shared by the gates)
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"
# Directories under docs/ that hold no documentation (the README banner source
# and image live in docs/assets/): never scanned, even if a .md lands there.
_DOCS_SKIP_DIRS = frozenset({"assets"})


def _docs_tree() -> list[Path]:
    """Every ``docs/**/*.md`` outside the non-documentation directories."""
    return sorted(
        p for p in _DOCS.rglob("*.md")
        if not _DOCS_SKIP_DIRS.intersection(p.relative_to(_DOCS).parts[:-1])
    )


def _doc_files() -> list[Path]:
    files = [_ROOT / "README.md", _ROOT / "INSTALL.md", _ROOT / "ROADMAP.md"]
    files += sorted((_ROOT / ".github").glob("README.*.md"))
    files += _docs_tree()
    files += sorted((_ROOT / "skills").glob("*/SKILL.md"))
    files += sorted((_ROOT / "commands").glob("*.md"))
    return [f for f in files if f.is_file()]


def test_doc_file_set_is_recursive_under_docs() -> None:
    """The file set reaches nested docs (``docs/recipes/``) and the docs index,
    and never pulls in ``docs/assets/``."""
    rels = {f.relative_to(_ROOT).as_posix() for f in _doc_files()}
    assert "docs/README.md" in rels
    assert "docs/cli-reference.md" in rels
    nested = {r for r in rels if r.startswith("docs/") and r.count("/") > 1}
    assert any(r.startswith("docs/recipes/") for r in nested), (
        "docs/recipes/*.md fell out of the docs file set")
    assert not any("/assets/" in r for r in rels)
    on_disk = {p.relative_to(_ROOT).as_posix() for p in _DOCS.rglob("*.md")
               if "assets" not in p.relative_to(_DOCS).parts}
    assert on_disk <= rels, f"docs pages outside the gates: {sorted(on_disk - rels)}"


def _fenced_blocks(path: Path) -> list[tuple[str, list[tuple[int, str]]]]:
    """``(language, [(lineno, text), ...])`` for every ``````` fenced block.

    ``language`` is the fence's info string, lower-cased (``""`` when bare).
    """
    blocks: list[tuple[str, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.lstrip()
        if stripped.startswith("```"):
            if current is None:
                current = []
                blocks.append((stripped[3:].strip().lower(), current))
            else:
                current = None
            continue
        if current is not None:
            current.append((i, raw))
    return blocks


def _fenced_lines(path: Path) -> list[tuple[int, str]]:
    """``(lineno, text)`` for every line inside a ``````` fence."""
    return [line for _lang, lines in _fenced_blocks(path) for line in lines]


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
_EVAL_DIR = _ROOT / "tools" / "agent_eval"


@functools.cache
def _registry_counts() -> dict[str, int]:
    from akcli import ops
    from akcli.calc import CALCS
    from akcli.review import rules_index

    return {
        "ops": len(ops.OP_NAMES),
        "macros": len(ops.MACRO_OPS),
        "calculators": len(CALCS),
        # not registry tables — these two noun classes are counted straight off
        # the filesystem (the plugin/skill surface itself), same "count claim
        # must match reality" contract as the op/macro/calc registries above.
        "skills": len(list((_ROOT / "skills").glob("*/SKILL.md"))),
        "commands": len(list((_ROOT / "commands").glob("*.md"))),
        # the published JSON contracts. Every file in schemas/ is a name
        # `akcli schema` serves (tests/test_schema_cmd.py walks the same glob),
        # so the op-capability matrix ops.capabilities.json COUNTS even though
        # it is not itself a JSON Schema — "N schemas" means "what `akcli
        # schema` lists".
        "schemas": len(list((_ROOT / "schemas").glob("*.json"))),
        # registered review rules — what `review explain` can print.
        "rules": len(rules_index()),
        # agent-eval tasks, counted the way the harness discovers them
        # (tools/agent_eval/run_eval.py, tests/test_agent_eval.py). Read from
        # disk on purpose: the corpus grows, and a hard-coded number here
        # would be a second place to forget.
        "tasks": sum(1 for d in (_EVAL_DIR / "tasks").iterdir()
                     if (d / "task.md").is_file()),
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
    # skills/commands claims are usually "N <optional single `code-span`> noun"
    # (e.g. "8 `/akcli:circuit-*` commands", "12 `akcli` skills") — the
    # optional backtick group absorbs that without opening the noun class up.
    ("skills", re.compile(r"(?<![\w.])(\d+)\s+(?:`[^`\n]*`\s+)?skills?(?![\w-])")),
    ("commands", re.compile(
        r"(?<![\w.])(\d+)\s+(?:slash\s+)?(?:`[^`\n]*`\s+)?commands?\b")),
    # zh (Hant/Hans share the CJK forms below)
    ("ops", re.compile(r"(\d+)\s*[種种]\s*op(?![\w-])")),
    ("macros", re.compile(r"(\d+)\s*[種种]\s*(?:巨集|宏)")),
    ("calculators", re.compile(r"(\d+)\s*[個个種种][一-鿿]{0,10}(?:計算|计算)")),
    ("skills", re.compile(r"(?<![\w.])(\d+)\s*[個个](?:の)?\s*skills\b")),
    ("commands", re.compile(r"(?<![\w.])(\d+)\s*[個个].{0,40}?(?:指令|命令)")),
    # ja — the katakana/kana forms the zh patterns cannot reach ("10 マクロ",
    # "60 個の規格準拠計算機"; the bare "22 op" is already caught in English).
    ("macros", re.compile(r"(\d+)\s*(?:個\s*の\s*)?マクロ")),
    ("calculators", re.compile(r"(\d+)\s*個の[ぁ-んァ-ヴ一-鿿]{0,12}計算機")),
    ("commands", re.compile(r"(?<![\w.])(\d+)\s*つの.{0,40}?コマンド")),
    # ---- schemas / review rules / agent-eval tasks ------------------------- #
    # Tighter than the classes above: "rules" and "tasks" are everyday words,
    # so a claim must look like a claim.
    #   * a leading "-" is refused too -> "IPC-2221 rules" is a standard's name
    #   * schemas: "14 schemas", "14 JSON schemas", "14 published schemas"
    #   * rules:   "44 rules" / "44 review rules". A qualified count such as
    #              "8 pre-compliance rules" is one FAMILY's count, not the
    #              registry total, and is deliberately not a claim.
    #   * tasks:   only with an eval qualifier ("10 ground-truthed design
    #              tasks", "10 eval tasks"); a bare "3 tasks" is prose.
    ("schemas", re.compile(
        r"(?<![\w.-])(\d+)\s+(?:(?:published|packaged|JSON)\s+){0,2}"
        r"[Ss]chemas(?![\w-])")),
    ("rules", re.compile(r"(?<![\w.-])(\d+)\s+(?:review\s+)?rules(?![\w-])")),
    ("tasks", re.compile(
        r"(?<![\w.-])(\d+)\s+(?:ground-truthed\s+)?"
        r"(?:design|eval|agent-eval)\s+tasks(?![\w-])")),
    # zh / ja forms of the same three. The CJK qualifier is REQUIRED (審查規則,
    # レビュールール, 評測任務) so an unrelated "3 條規則" never reads as a claim;
    # (?<![\d.]) keeps a version number ("0.14 の…") from donating its tail —
    # \w cannot be used here because it also matches the CJK text before a count.
    ("schemas", re.compile(r"(?<![\d.])(\d+)\s*[個个份]\s*(?:JSON\s*)?[Ss]chemas?(?![\w-])")),
    ("rules", re.compile(
        r"(?<![\d.])(\d+)\s*[條条項项]\s*(?:設計|设计)?(?:審查|审查|評審|评审)(?:規則|规则)")),
    ("tasks", re.compile(
        r"(?<![\d.])(\d+)\s*[個个項项]\s*(?:評測|评测|評估|评估)(?:任務|任务)")),
    ("schemas", re.compile(r"(?<![\d.])(\d+)\s*(?:個|つ)\s*の\s*(?:JSON\s*)?スキーマ")),
    ("rules", re.compile(r"(?<![\d.])(\d+)\s*(?:個|つ)\s*の\s*(?:設計)?レビュー\s*ルール")),
    ("tasks", re.compile(r"(?<![\d.])(\d+)\s*(?:個|つ)\s*の\s*評価\s*タスク")),
]

# Count sources OUTSIDE the docs file set, each with its own patterns only (the
# general patterns above are not applied to them, and they are not fence-gated).
# The agent-eval README states the size of its own corpus; a bare "N tasks" is
# unambiguous there and nowhere else.
_EXTRA_COUNT_SOURCES: list[tuple[Path, list[tuple[str, re.Pattern[str]]]]] = [
    (_EVAL_DIR / "README.md",
     [("tasks", re.compile(
         r"(?<![\w.-])(\d+)\s+(?:ground-truthed\s+)?"
         r"(?:(?:design|eval|agent-eval)\s+)?tasks(?![\w-])"))]),
]


def _line_claims(
    line: str, patterns: list[tuple[str, re.Pattern[str]]]
) -> list[tuple[str, int]]:
    """``(kind, claimed)`` for every count claim on one line.

    Emphasis markers are stripped first: the patterns want the noun to follow
    the digits across whitespace, so a bolded ``**60** calculators`` (which is
    how every README writes its `calc` table row) was invisible to the gate.
    Two patterns of one kind matching the same digits yield one claim.
    """
    plain = line.replace("**", "").replace("__", "")
    seen: dict[tuple[str, int], int] = {}
    for kind, pat in patterns:
        for m in pat.finditer(plain):
            seen.setdefault((kind, m.start(1)), int(m.group(1)))
    return [(kind, claimed) for (kind, _pos), claimed in seen.items()]


def _count_cases() -> list[tuple[str, int, str, int]]:
    """``(rel, lineno, kind, claimed)`` for every count claim in the file set."""
    sources = [(path, _COUNT_PATTERNS) for path in _doc_files()]
    sources += [(p, pats) for p, pats in _EXTRA_COUNT_SOURCES if p.is_file()]
    cases: list[tuple[str, int, str, int]] = []
    for path, patterns in sources:
        rel = path.relative_to(_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            for kind, claimed in _line_claims(line, patterns):
                cases.append((rel, lineno, kind, claimed))
    return cases


def test_count_gate_found_claims() -> None:
    """Guard the guard: if this drops to zero the matchers silently rotted."""
    cases = _count_cases()
    assert len(cases) >= 15
    # ...per kind too: a matcher that stops seeing ITS noun is as blind as an
    # empty gate. If a kind's last claim was removed from the docs on purpose,
    # drop that kind here; otherwise fix the matcher (or write the count in
    # digits — a word-form "ten tasks" is invisible).
    missing = set(_registry_counts()) - {kind for _r, _l, kind, _c in cases}
    assert not missing, f"no count claim found for: {sorted(missing)}"


# Matcher self-test: the three "everyday word" kinds must catch a real claim
# and leave look-alike prose alone. Synthetic lines, so the test does not depend
# on what the docs happen to say today.
@pytest.mark.parametrize("line,expected", [
    ("prints any of the 14 schemas in `schemas/`", [("schemas", 14)]),
    ("**14** published JSON schemas ship in the wheel", [("schemas", 14)]),
    ("### 3.12 Schemas (`schemas/`)", []),                 # section number
    ("validated against schema 2.0 schemas", []),          # version, not a count
    ("gerber; **44 rules**) emit findings", [("rules", 44)]),
    ("the 44 review rules", [("rules", 44)]),
    ("emc: 8 pre-compliance rules; gerber", []),           # one family's count
    ("sized by the IPC-2221 rules", []),                   # a standard's name
    ("since v0.14 rules carry a version", []),             # release number
    ("10 ground-truthed design tasks, scored", [("tasks", 10)]),
    ("the corpus has 12 eval tasks", [("tasks", 12)]),
    ("split the work into 3 tasks", []),                   # bare: prose
    ("共 44 條審查規則", [("rules", 44)]),
    ("内置 44 条设计评审规则", [("rules", 44)]),
    ("這 3 條規則", []),                                     # unqualified zh
    ("44 個のレビュールール", [("rules", 44)]),
    ("14 個 JSON Schema", [("schemas", 14)]),
    ("14 個の JSON スキーマ", [("schemas", 14)]),
    ("10 個評測任務", [("tasks", 10)]),
    ("10 個の評価タスク", [("tasks", 10)]),
])
def test_new_count_matchers_are_tight(
    line: str, expected: list[tuple[str, int]]
) -> None:
    new_kinds = {"schemas", "rules", "tasks"}
    got = [(k, n) for k, n in _line_claims(line, _COUNT_PATTERNS) if k in new_kinds]
    assert got == expected, f"{line!r}: matched {got}, wanted {expected}"


def test_eval_readme_bare_task_count_is_gated() -> None:
    """In the eval README a bare ``N tasks`` IS the corpus-size claim."""
    patterns = dict(_EXTRA_COUNT_SOURCES)[_EVAL_DIR / "README.md"]
    assert _line_claims("- 10 tasks: six small analog blocks", patterns) == [("tasks", 10)]
    assert _line_claims("- 10 ground-truthed design tasks", patterns) == [("tasks", 10)]
    assert _line_claims("- Eight tasks: six small analog blocks", patterns) == []


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
# recipe gate: a docs/recipes/ workflow cannot run an akcli command the fence
# gate never validated.
#
# The fence gate only sees a fenced line that STARTS with ``akcli`` — a YAML
# ``run: akcli check …`` line is invisible to it. So a recipe lists its
# commands again as bare lines in a fenced ``bash`` block (validated flag by
# flag against the live parser), and this gate pins the YAML to that block:
# every ``akcli`` invocation in a recipe's ``yaml`` fences must appear,
# character for character, among its ``bash``-fenced lines.
# --------------------------------------------------------------------------- #
_YAML_LANGS = frozenset({"yaml", "yml"})
_SHELL_LANGS = frozenset({"bash", "sh", "shell"})
# ``run: akcli …`` / ``- run: akcli …`` (single-line step).
_RUN_INLINE = re.compile(r"^(?:-\s+)?run:\s*(akcli\s.*)$")
# ``akcli`` used as a command word (not ``akcli-kicad``, ``akcli.sarif``, a
# job id ``akcli:`` …).
_AKCLI_WORD = re.compile(r"(?<![\w.-])akcli\s")


def _recipe_commands(path: Path) -> tuple[dict[str, int], set[str], list[int]]:
    """``(yaml_cmds, bash_cmds, stray)`` for one recipe page.

    ``yaml_cmds`` maps each ``akcli`` invocation found in a ``yaml`` fence (a
    ``run:`` one-liner or a line of a ``run: |`` block) to its line number;
    ``bash_cmds`` is every fence-gated ``akcli`` line of the shell fences
    (``# doc-noqa`` lines are NOT validated, so they do not count); ``stray``
    lists YAML lines that mention the ``akcli`` command in a position this
    extractor cannot isolate (``run: cd hw && akcli …``) — a recipe must keep
    each invocation at the start of its line so it can be checked.
    """
    yaml_cmds: dict[str, int] = {}
    bash_cmds: set[str] = set()
    stray: list[int] = []
    for lang, lines in _fenced_blocks(path):
        for lineno, raw in lines:
            text = raw.strip()
            if lang in _YAML_LANGS:
                inline = _RUN_INLINE.match(text)
                if inline:
                    text = inline.group(1).strip()
                if text.startswith("akcli "):
                    yaml_cmds.setdefault(text, lineno)
                elif _AKCLI_WORD.search(text) and not text.startswith("#"):
                    stray.append(lineno)
            elif lang in _SHELL_LANGS:
                if text.startswith("akcli ") and "doc-noqa" not in raw:
                    bash_cmds.add(text)
    return yaml_cmds, bash_cmds, stray


def _recipe_pages() -> list[Path]:
    return sorted((_DOCS / "recipes").glob("*.md"))


def _recipe_problems(path: Path) -> list[str]:
    yaml_cmds, bash_cmds, stray = _recipe_commands(path)
    rel = path.relative_to(_ROOT).as_posix() if path.is_relative_to(_ROOT) else path.name
    problems = [
        f"{rel}:{lineno}: `akcli` is not at the start of the run line — "
        "put each invocation on its own line so it can be validated"
        for lineno in stray
    ]
    problems += [
        f"{rel}:{lineno}: workflow runs `{cmd}` but the recipe's fenced bash "
        "block does not list that exact line (so no gate validated its flags)"
        for cmd, lineno in sorted(yaml_cmds.items(), key=lambda kv: kv[1])
        if cmd not in bash_cmds
    ]
    return problems


def test_recipe_workflow_commands_are_fence_validated() -> None:
    fence_gated = {(rel, raw.strip()) for rel, _lineno, raw in _fence_cases()}
    checked = 0
    for path in _recipe_pages():
        problems = _recipe_problems(path)
        assert not problems, "\n".join(problems)
        yaml_cmds, _bash, _stray = _recipe_commands(path)
        rel = path.relative_to(_ROOT).as_posix()
        # ...and the block really is in the fence gate's case list (the page
        # is inside _doc_files(), the line is not opted out).
        unvalidated = [c for c in yaml_cmds if (rel, c) not in fence_gated]
        assert not unvalidated, f"{rel}: not reached by the fence gate: {unvalidated}"
        checked += len(yaml_cmds)
    assert checked, (
        "no docs/recipes/ workflow runs an akcli command — the recipe moved or "
        "the extractor rotted")


def test_recipe_gate_detects_drift(tmp_path: Path) -> None:
    """Guard the guard: a YAML command missing from the bash block, and an
    ``akcli`` buried mid-line, must both be reported."""
    page = tmp_path / "recipe.md"
    page.write_text(
        "```yaml\n"
        "steps:\n"
        "  - run: akcli check \"$SCH\" --format sarif > out.sarif\n"
        "  - run: pip install akcli-kicad\n"
        "  - name: drift\n"
        "    run: |\n"
        "      akcli review diff a.json b.json --fail-on-new\n"
        "      akcli gate \"$SCH\"\n"
        "  - run: cd hw && akcli check board.kicad_sch\n"
        "```\n\n"
        "```bash\n"
        "akcli check \"$SCH\" --format sarif > out.sarif\n"
        "akcli review diff a.json b.json --fail-on-new\n"
        "akcli gate \"$SCH\"  # doc-noqa\n"
        "```\n",
        encoding="utf-8", newline="\n")
    yaml_cmds, bash_cmds, stray = _recipe_commands(page)
    assert set(yaml_cmds) == {
        'akcli check "$SCH" --format sarif > out.sarif',
        "akcli review diff a.json b.json --fail-on-new",
        'akcli gate "$SCH"',
    }
    assert 'akcli gate "$SCH"' not in bash_cmds      # opted out = unvalidated
    assert stray == [9]                              # the `cd hw && akcli` line
    problems = _recipe_problems(page)
    assert len(problems) == 2
    assert any("not at the start" in p for p in problems)
    assert any('akcli gate "$SCH"' in p for p in problems)


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
