"""Machine-code census gates — docs ↔ code-registry anti-drift nets.

Sibling of ``test_docs_conformance.py`` (fence/count/config/schema/release
gates). These gates lock the drift classes the 2026-08 consistency sweep
found by hand, so they cannot regrow silently:

* **Error-registry census.** The frozen ``errors.ERROR_CODES`` recital in
  SPEC §3.1 and the exit-grouped table in cli-reference.md each missed two
  newly added codes. Both regions are now asserted equal (two-way) to the
  live registry.
* **Ghost-code gate.** ``agent-state.md`` shipped a finding code that never
  existed (``PWR_NO_DECOUPLING`` for ``POWER_NO_DECOUPLING``). Every
  code-shaped token in the docs whose family prefix belongs to a real code
  family must name a real code.
* **Family censuses.** ``docs/review-rules.md`` declares itself the single
  source of truth for ``REVIEW_*`` rules, and ``docs/sim.md`` documents the
  ``SIM_*`` roster — both were audited to exactly match the code (52/52 and
  22/22); the two-way censuses keep it that way.
* **JLC CSV header.** The documented ``jlc bom --csv`` header spent months
  describing a 4-column shape while the code wrote 11 columns. The exact
  header string must now appear in docs/jlc.md, as must every
  ``diff_lock``/``bom_diff`` drift-kind literal.

If a gate fires, the matcher is the first suspect — widen/narrow the
extraction before touching a doc; only edit a doc when it is genuine drift.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "akcli"

# --------------------------------------------------------------------------- #
# ground truth: every machine code the code base can emit or name
# --------------------------------------------------------------------------- #
# Codes are constructed three ways in the tree: ``code="X"`` kwargs,
# positional helper calls (``_f("FACTS_STALE", ...)``) and module constants
# (``SIM_ASSERT_FAIL = "SIM_ASSERT_FAIL"``). A code-shaped STRING LITERAL
# scan covers all three; over-collection is safe for these gates (it can
# only silence, never invent, a ghost).
_STR_LITERAL = re.compile(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"')
# Module-level UPPER assignments (``OP_NAMES: frozenset = ...``,
# ``MAX_FILE_BYTES: int = ...``): SPEC quotes these public constant names in
# its API column — they are code symbols, not finding codes, and must not
# read as ghosts. The optional group eats a type annotation.
_UPPER_ASSIGN = re.compile(
    r"^\s*([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\s*(?::[^=\n]+)?=", re.M)
# Finding/error codes at their construction sites — the narrow set that
# defines which code FAMILIES the ghost gate polices.
_EMIT_SITES = (
    re.compile(r'code\s*=\s*"([A-Z][A-Z0-9_]+)"'),
    re.compile(r'fail\(\s*"([A-Z][A-Z0-9_]+)"'),
    re.compile(r'Finding\(\s*"([A-Z][A-Z0-9_]+)"'),
    re.compile(r'_f\(\s*"([A-Z][A-Z0-9_]+)"'),
    re.compile(r'^\s*([A-Z][A-Z0-9_]+)\s*=\s*"\1"', re.M),  # NAME = "NAME"
)


def _finding_codes(root: Path = _SRC) -> set[str]:
    out: set[str] = set()
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        for pat in _EMIT_SITES:
            out.update(pat.findall(text))
    return out


def _emitted_codes(root: Path = _SRC) -> set[str]:
    """Code-shaped string literals anywhere under ``root``."""
    out: set[str] = set()
    for py in root.rglob("*.py"):
        out.update(_STR_LITERAL.findall(
            py.read_text(encoding="utf-8", errors="replace")))
    return out


def _known_codes() -> set[str]:
    from akcli.errors import ERROR_CODES, EXIT, EXIT_REMEDIATION

    known = set(ERROR_CODES) | _emitted_codes()
    known |= set(EXIT)                    # exit-category names (QUERY_MISS, ...)
    known |= set(EXIT_REMEDIATION)        # envelope pseudo-codes (FILE_NOT_FOUND)
    known |= {"USAGE"}                    # argparse envelope pseudo-code
    for py in _SRC.rglob("*.py"):         # public constant names (OP_NAMES, ...)
        known.update(_UPPER_ASSIGN.findall(
            py.read_text(encoding="utf-8", errors="replace")))
    return known


# Doc tokens that legitimately look like codes but are not finding/error codes.
# Keep this list short and commented — every entry is a judged exception.
_NOT_A_CODE = {
    "KICAD_CONFIG_HOME",        # KiCad's own env var (kicad-format-gotchas)
    "KICAD_SYMBOL_DIR",         # KiCad's own env var (setup skill trigger list)
    "KICAD_CLI",                # akcli env var naming the kicad-cli binary
    "NET_A", "NET_B",           # de-sensitized example net names
    "SIM_PARAMS",               # prose reference to the Sim.Params field family
    "REVIEW_LLM_BOOTSTRAP_RACE",  # deep-review skill's EXAMPLE candidate code —
                                  # LLM candidates mint free-form REVIEW_* codes
                                  # by design (G4 only bans registered-rule
                                  # collisions)
    "POWER_PIN", "SHEET_PIN",   # rungs of EESCHEMA's own driver-priority
                                # ladder (mirrored in netbuild.py's comment),
                                # quoted by docs/SPEC.md's naming-priority
                                # row — eeschema enum names, not akcli codes
    "BOM_LCSC_MISMATCH",        # deliberately NOT a Finding: a BOM-line note
                                # string (parts/bom_jlc.py) — docs describe it
                                # as such; drop this entry if it is ever
                                # promoted to a real `check` finding
}


def _doc_files() -> list[Path]:
    files = [_ROOT / "README.md", _ROOT / "INSTALL.md", _ROOT / "ROADMAP.md"]
    files += sorted((_ROOT / ".github").glob("README.*.md"))
    files += sorted((_ROOT / "docs").glob("*.md"))
    files += sorted((_ROOT / "skills").glob("*/SKILL.md"))
    files += sorted((_ROOT / "commands").glob("*.md"))
    return [f for f in files if f.is_file()]


_TOKEN = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _doc_code_tokens(text: str) -> set[str]:
    """Code-shaped tokens, skipping glob stubs (``FAB_COST_*``), templates
    (``NC_<ref>_<pin>``) and mid-identifier fragments (``${KICAD*_SYMBOL_DIR}``)."""
    out: set[str] = set()
    for m in _TOKEN.finditer(text):
        end, start = m.end(), m.start()
        if end < len(text) and text[end] in "*_<":
            continue
        if start > 0 and text[start - 1] in "*_":
            continue
        out.add(m.group(0))
    return out


# --------------------------------------------------------------------------- #
# gate 1 — the two frozen error-registry recitals match errors.py exactly
# --------------------------------------------------------------------------- #
def test_spec_error_registry_recital_matches() -> None:
    from akcli.errors import ERROR_CODES

    spec = (_ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    m = re.search(r"\| `errors\.py` \|.*?\| Codes: `([^`]+)`", spec)
    assert m, "SPEC.md lost its errors.py registry row"
    recited = set(re.findall(r"[A-Z][A-Z0-9_]+", m.group(1)))
    assert recited == set(ERROR_CODES), (
        f"SPEC §3.1 errors.py row drifted: missing {sorted(set(ERROR_CODES) - recited)}, "
        f"ghosts {sorted(recited - set(ERROR_CODES))}")


def test_cli_reference_exit_table_matches() -> None:
    from akcli.errors import _CODE_EXIT

    text = (_ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    m = re.search(
        r"grouped by the exit code they\nsurface as:\n\n(.*?)\n\n", text, re.S)
    assert m, "cli-reference.md lost its exit-grouped error-code table"
    rows = re.findall(r"^\| `(\d+)` [^|]*\| (.+) \|$", m.group(1), re.M)
    assert rows, "exit-grouped table matched no rows"
    documented: dict[str, int] = {}
    for exit_code, cell in rows:
        for code in re.findall(r"`([A-Z][A-Z0-9_]+)`", cell):
            documented[code] = int(exit_code)
    assert documented == dict(_CODE_EXIT), (
        f"cli-reference exit table drifted: "
        f"missing {sorted(set(_CODE_EXIT) - set(documented))}, "
        f"ghosts {sorted(set(documented) - set(_CODE_EXIT))}, "
        f"wrong-exit {sorted(c for c in documented if c in _CODE_EXIT and documented[c] != _CODE_EXIT[c])}")


# --------------------------------------------------------------------------- #
# gate 2 — no ghost codes anywhere in the docs
# --------------------------------------------------------------------------- #
def test_no_ghost_codes_in_docs() -> None:
    from akcli.errors import ERROR_CODES

    known = _known_codes()
    families = {c.split("_", 1)[0]
                for c in (set(ERROR_CODES) | _finding_codes()) if "_" in c}
    ghosts: list[str] = []
    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for tok in _doc_code_tokens(line):
                if tok in known or tok in _NOT_A_CODE:
                    continue
                if tok.split("_", 1)[0] in families:
                    rel = path.relative_to(_ROOT).as_posix()
                    ghosts.append(f"{rel}:{lineno}: {tok}")
    assert not ghosts, (
        "doc names a code no code path emits (rename? typo? add to _NOT_A_CODE "
        "only if it is genuinely not a code):\n" + "\n".join(sorted(set(ghosts))))


# --------------------------------------------------------------------------- #
# gate 3 — family censuses: review-rules.md and sim.md stay exactly complete
# --------------------------------------------------------------------------- #
def test_review_rules_census_two_way() -> None:
    emitted = {c for c in _emitted_codes(_SRC / "review")
               if c.startswith(("REVIEW_", "FACTS_"))}
    doc = (_ROOT / "docs" / "review-rules.md").read_text(encoding="utf-8")
    documented = {t for t in _doc_code_tokens(doc)
                  if t.startswith(("REVIEW_", "FACTS_"))}
    assert emitted == documented, (
        f"review-rules.md census drifted: undocumented {sorted(emitted - documented)}, "
        f"ghosts {sorted(documented - emitted)}")


def test_sim_codes_census_two_way() -> None:
    from akcli.errors import ERROR_CODES

    emitted = {c for c in _emitted_codes(_SRC / "sim") if c.startswith("SIM_")}
    emitted |= {c for c in _emitted_codes(_SRC / "commands") if c.startswith("SIM_")}
    emitted |= {c for c in ERROR_CODES if c.startswith("SIM_")}
    doc = (_ROOT / "docs" / "sim.md").read_text(encoding="utf-8")
    documented = {t for t in _doc_code_tokens(doc) if t.startswith("SIM_")}
    assert emitted == documented, (
        f"sim.md census drifted: undocumented {sorted(emitted - documented)}, "
        f"ghosts {sorted(documented - emitted)}")


# --------------------------------------------------------------------------- #
# gate 4 — the jlc bom CSV header and drift-kind vocabularies are quoted
#          exactly (docs/jlc.md described a 4-column header for months while
#          the code wrote 11 columns)
# --------------------------------------------------------------------------- #
def test_jlc_csv_header_documented_exactly() -> None:
    from akcli.parts.bom_jlc import _JLC_CSV_HEADER

    doc = (_ROOT / "docs" / "jlc.md").read_text(encoding="utf-8")
    # The doc may wrap the header across lines; compare whitespace-insensitively.
    flat_doc = re.sub(r"\s+", " ", doc)
    wanted = ",".join(_JLC_CSV_HEADER)
    variants = (wanted, ", ".join(_JLC_CSV_HEADER))
    assert any(re.sub(r"\s+", " ", v) in flat_doc for v in variants), (
        f"docs/jlc.md no longer quotes the exact jlc bom --csv header: {wanted!r}")


def _kind_literals(func) -> set[str]:
    return set(re.findall(r'"kind":\s*"([a-z_]+)"', inspect.getsource(func)))


def test_lock_drift_kinds_documented() -> None:
    from akcli.parts.bom_jlc import bom_diff, diff_lock

    doc = (_ROOT / "docs" / "jlc.md").read_text(encoding="utf-8")
    missing = {k for k in _kind_literals(diff_lock) if f"`{k}`" not in doc}
    assert not missing, f"docs/jlc.md missing --against-lock drift kinds: {sorted(missing)}"
    # bom_diff's (diff --bom) vocabulary lives in cli-reference/diff docs; here
    # we only pin that the two vocabularies genuinely differ (the docs warn
    # readers not to conflate them — keep that warning honest).
    assert _kind_literals(diff_lock) != _kind_literals(bom_diff)
