"""Meta-gates: the structural gates themselves must not rot.

Three failure classes this file freezes, all discovered while planning the
0.21 architecture rework:

* **Scan-surface drift** — an AST gate whose file collection is not
  recursive (or roots a directory that later moves) stops covering new
  modules *silently*: the gate stays green while the invariant it guards
  erodes.  ``test_ast_gates_cover_every_product_module`` pins the union of
  every AST gate's collection against the real tree.
* **Depth-sensitive path anchors** — ``Path(__file__).parents[N]`` encodes
  the module's directory depth.  Splitting a module into a package changes
  ``N`` semantics without any test noticing locally (the wheel path keeps
  working; only the editable/source-tree fallback breaks, i.e. CI-only).
  ``test_parents_anchor_sites_are_whitelisted`` makes every such site
  explicit and shrink-only.
* **SPEC module-table ghosts** — ``docs/SPEC.md`` names modules in prose
  tables with no gate, so a moved/renamed file leaves a ghost row behind.
  ``test_spec_module_rows_point_at_real_files`` fails on rows naming
  ``*.py`` files that no longer exist in the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "akcli"


def _product_files() -> set[Path]:
    return {p for p in SRC.rglob("*.py")
            if "_vendor" not in p.parts and "__pycache__" not in p.parts}


def test_ast_gates_cover_every_product_module():
    from tests import test_json_version_stamps as stamps
    from tests import test_text_io_portability as textio

    product = _product_files()
    textio_cover = {p for p in textio._py_files() if SRC in p.parents}
    stamps_cover = set(stamps._py_files())

    for name, cover in (("test_text_io_portability", textio_cover),
                        ("test_json_version_stamps", stamps_cover)):
        missing = sorted(str(p.relative_to(ROOT)) for p in product - cover)
        assert not missing, (
            f"{name} no longer scans these product modules (non-recursive "
            f"glob? moved root?): {missing}"
        )


# Every site that derives a filesystem anchor from its own module depth.
# SHRINK-ONLY: replacing one with a depth-independent anchor (packaged
# resource lookup, ``with_name``) removes its row; adding a NEW ``parents[N]``
# site is refused — a later repackaging silently breaks it (source-tree
# fallback only, so wheel smoke stays green while editable CI turns red).
_PARENTS_WHITELIST = {
    "src/akcli/__init__.py": 1,      # parents[2] -> repo root (pyproject.toml)
    "tools/schemas.py": 1,
    "tools/examples_mirror.py": 1,   # parents[1] -> repo root (examples corpus)
    "tools/golden_regen.py": 1,
    "tools/agent_eval/run_eval.py": 1,
    "tools/equiv_snapshot.py": 1,    # parents[1] -> repo root (fixture corpus)
}

_PARENTS_RX = re.compile(r"\.parents\[\d+\]")


def test_parents_anchor_sites_are_whitelisted():
    found: dict[str, int] = {}
    for base in (SRC, ROOT / "tools", ROOT / "hooks"):
        for py in sorted(base.rglob("*.py")):
            if "_vendor" in py.parts or "__pycache__" in py.parts:
                continue
            n = len(_PARENTS_RX.findall(py.read_text(encoding="utf-8")))
            if n:
                found[py.relative_to(ROOT).as_posix()] = n
    new = {k: v for k, v in found.items()
           if k not in _PARENTS_WHITELIST or v > _PARENTS_WHITELIST[k]}
    assert not new, (
        "new Path(...).parents[N] anchor site(s) — depth-sensitive; use a "
        f"packaged-resource or with_name() anchor instead: {new}"
    )
    stale = {k: v for k, v in _PARENTS_WHITELIST.items()
             if found.get(k, 0) < v}
    assert not stale, (
        f"stale _PARENTS_WHITELIST entries (shrink the list): {stale}"
    )


_SPEC_ROW_RX = re.compile(r"^\|\s*`([A-Za-z0-9_/]+\.py)`\s*\|", re.MULTILINE)


def test_spec_module_rows_point_at_real_files():
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    ghosts = []
    for name in _SPEC_ROW_RX.findall(spec):
        base = ROOT if name.split("/", 1)[0] in ("tools", "hooks", "bin") else SRC
        hits = [p for p in SRC.rglob(name.split("/")[-1])
                if "_vendor" not in p.parts]     # vendored files must not
        if not (base / name).is_file() and not hits:  # shadow a ghost row
            ghosts.append(name)
    assert not ghosts, (
        "docs/SPEC.md module-table row(s) name files that no longer exist "
        f"under src/akcli — update the table: {ghosts}"
    )
