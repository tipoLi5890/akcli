"""Import-layering gate — the architecture's shape, frozen as a test.

Stdlib-only AST scan (same idiom as ``test_text_io_portability``) enforcing
four rules over ``src/akcli``:

1. **Rank monotonicity** — ``rank(importer) >= rank(imported)`` for every
   internal import, INCLUDING function-local (lazy) ones.  Lazy imports are
   not a layering exemption: they are exactly where the historical
   ``arrange <-> groupframe <-> checks.layout`` cycle hid.
2. **Module-level DAG** — iterative Tarjan SCC over module-level imports
   only; any SCC bigger than one module is a cycle and fails with the loop
   printed.  (Lazy edges are excluded here on purpose: they don't bite at
   import time; rule 1 already constrains their direction.)
3. **Full rank coverage** — every product module must match a ``_RANK``
   prefix: a new module must pick its layer consciously.
4. **Waivers shrink only** — each waived edge carries a reason and must
   still exist; a stale waiver fails the suite.

Ranks are assigned by ROLE, not by current directory: ``readers/sexpr`` and
``writers/geometry`` already carry their target-layer ranks (30/40), so the
0.21 package moves change the KEYS of this table, never its meaning.

    rank  layer
      5   package root (akcli/__init__ — no internal imports)
     10   base leaves: errors units safety kicad_escape classify
     15   packaged data: akcli.schemas akcli.examples akcli.fab_profiles
     20   contracts: model report config journal netdiff bom_policy ops
          symbol_spec
     30   sexpr IR (today: readers.sexpr; after the move: akcli.sexpr)
     40   spatial + net graph: writers.geometry (-> akcli.geometry), netbuild
     50   readers
     60   writers
     70   engines A: project libtable library_baseline exporters relink fab
          render_*
          arrange groupframe drivers adapters calc explain
     72   checks   73 sim   74 review   76 parts   78 intentgen
     80   pipeline
     84   commands._shared   85 commands   90 cli webui __main__
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "akcli"

# Longest-prefix wins.  Keys are dotted module paths relative to ``akcli``.
_RANK: dict[str, int] = {
    "__init__": 5,
    "errors": 10, "units": 10, "safety": 10, "kicad_escape": 10, "si_prefix": 10,
    "classify": 10,
    "schemas": 15, "examples": 15, "fab_profiles": 15,
    "model": 20, "report": 20, "config": 20, "journal": 20, "netdiff": 20,
    "bom_policy": 20, "ops": 20, "symbol_spec": 20, "opspec": 20,
    "contract": 20,
    "sexpr": 30, "readers.sexpr": 30,
    "geometry": 40, "writers.geometry": 40, "netbuild": 40,
    "readers": 50,
    "writers": 60,
    "project": 70, "scaffold": 70, "libtable": 70, "exporters": 70,
    "library_baseline": 70,
    "relink": 70, "fab": 70,
    "render_svg": 70, "render_art": 70, "arrange": 70, "groupframe": 70,
    "drivers": 70, "adapters": 70, "calc": 70, "explain": 70,
    "checks": 72,
    "sim": 73,
    "review": 74,
    "parts": 76,
    "intentgen": 78,
    "pipeline": 80,
    "webui": 80,   # app-service consumed by commands.view; imports engines
    "commands._shared": 84,
    "commands": 85,
    "cli": 90, "__main__": 90,
}

# (importer, imported) -> reason.  SHRINK-ONLY: a stale entry fails.
_WAIVERS: dict[tuple[str, str], str] = {
    ("commands.capabilities", "cli"):
        "PERMANENT: the capabilities manifest introspects the live parser — "
        "self-reference is the feature",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"] or ["__init__"]
    return ".".join(parts)


def _rank(mod: str) -> int | None:
    probe = mod
    while probe:
        if probe in _RANK:
            return _RANK[probe]
        probe = probe.rpartition(".")[0]
    return None


def _product_files() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py"))
            if "_vendor" not in p.parts and "__pycache__" not in p.parts]


def _resolve_relative(importer: str, node: ast.ImportFrom) -> list[str]:
    """Resolve ``from .x import a, b`` to akcli-relative dotted targets.

    ``from .. import model, units`` resolves to module ``""`` with the real
    targets hiding in ``names`` — each name must become its own edge or the
    whole contracts layer vanishes from the graph.
    """
    if node.level == 0:
        base = node.module or ""
        if base == "akcli":
            return [a.name for a in node.names]
        if base.startswith("akcli."):
            base = base[len("akcli."):]
            # names may be submodules (from akcli.writers import geometry);
            # _normalize trims attribute tails back to a known module.
            return [f"{base}.{a.name}" for a in node.names]
        return []
    parts = importer.split(".")
    # level=1 from inside a module means "this package": drop the leaf.
    anchor = parts[:-node.level] if len(parts) >= node.level else []
    if node.module:
        base = ".".join([*anchor, node.module])
        return [f"{base}.{a.name}" for a in node.names]
    return [".".join([*anchor, a.name]) for a in node.names]


def _edges(path: Path) -> list[tuple[str, bool, int]]:
    """(imported_module, is_module_level, lineno) for every internal import."""
    mod = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, bool, int]] = []
    toplevel = set(tree.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [a.name[len("akcli."):] for a in node.names
                       if a.name.startswith("akcli.")]
        elif isinstance(node, ast.ImportFrom):
            targets = _resolve_relative(mod, node)
        else:
            continue
        at_top = node in toplevel
        for t in targets:
            if t:
                out.append((t, at_top, node.lineno))
    return out


def _known_modules() -> set[str]:
    mods = set()
    for p in _product_files():
        m = _module_name(p)
        mods.add(m)
        while "." in m:
            m = m.rpartition(".")[0]
            mods.add(m)
    return mods


def _normalize(target: str, known: set[str]) -> str | None:
    """Trim attribute tails (``writers.kicad.apply`` -> ``writers.kicad``)."""
    probe = target
    while probe and probe not in known:
        probe = probe.rpartition(".")[0]
    return probe or None


def test_every_module_has_a_rank():
    unranked = sorted(m for m in (_module_name(p) for p in _product_files())
                      if _rank(m) is None)
    assert not unranked, (
        "module(s) without a layer rank — a new module must consciously "
        f"pick its layer in tests/test_layering.py:_RANK: {unranked}"
    )


def test_rank_monotonicity_including_lazy_imports():
    known = _known_modules()
    violations: list[str] = []
    seen_waived: set[tuple[str, str]] = set()
    for path in _product_files():
        importer = _module_name(path)
        r_imp = _rank(importer)
        assert r_imp is not None, importer
        for target, _at_top, lineno in _edges(path):
            t = _normalize(target, known)
            if t is None or t == importer:
                continue
            r_t = _rank(t)
            if r_t is None or r_t <= r_imp:
                continue
            key = next((k for k in _WAIVERS
                        if k[0] == importer and t.startswith(k[1])), None)
            if key is not None:
                seen_waived.add(key)
                continue
            violations.append(
                f"{path.relative_to(ROOT)}:{lineno}: akcli.{importer} "
                f"(rank {r_imp}) imports akcli.{t} (rank {r_t})")
    assert not violations, (
        "layering violation(s) — lower layers must not import higher "
        "ones:\n  " + "\n  ".join(violations)
    )
    stale = {k: v for k, v in _WAIVERS.items()
             if k not in seen_waived and not v.startswith("PERMANENT")}
    assert not stale, f"stale waiver(s) — shrink _WAIVERS: {stale}"


def test_module_level_import_graph_is_a_dag():
    known = _known_modules()
    graph: dict[str, set[str]] = {}
    for path in _product_files():
        importer = _module_name(path)
        deps = graph.setdefault(importer, set())
        for target, at_top, _ in _edges(path):
            if not at_top:
                continue
            t = _normalize(target, known)
            if t and t != importer:
                deps.add(t)

    # Iterative Tarjan (explicit stack — repo convention: no deep recursion).
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = [0]

    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, iter]] = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    sccs.append(sorted(comp))
    assert not sccs, f"module-level import cycle(s): {sccs}"


# --------------------------------------------------------------------------- #
# symbol-level boundary rules (0.21 R8: the extraction made these true;
# these asserts keep them true)
# --------------------------------------------------------------------------- #
def test_exitwith_and_argparse_stay_in_the_cli_layer():
    """Engines raise AkcliError; only the CLI layer owns exit codes/parsing.

    ``_ExitWith`` identifiers and ``import argparse`` may appear only under
    ``commands/`` and in ``cli.py`` (docstring PROSE elsewhere is fine — the
    check is AST-level, not text-level).
    """
    allowed = ("commands",)
    offenders: list[str] = []
    for path in _product_files():
        mod = _module_name(path)
        if mod.split(".")[0] in allowed or mod == "cli":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_ExitWith":
                offenders.append(f"{mod}: _ExitWith at line {node.lineno}")
            if isinstance(node, ast.Import) and any(
                    a.name == "argparse" for a in node.names):
                offenders.append(f"{mod}: import argparse at line {node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "argparse":
                offenders.append(f"{mod}: from argparse at line {node.lineno}")
    assert not offenders, (
        "CLI-layer symbols leaked into engine modules: " + "; ".join(offenders)
    )


def test_writers_kicad_stays_a_facade():
    """The 2,278-line monolith must not grow back: re-exports only."""
    tree = ast.parse((SRC / "writers" / "kicad.py").read_text(encoding="utf-8"))
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))]
    assert not defs, (
        "writers/kicad.py is a facade — put new code in the writers "
        f"submodules, not here: {[d.name for d in defs]}"
    )
