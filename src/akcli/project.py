"""Sheet-tree topology: root discovery + tree staging for hierarchy-aware gates.

A ``.kicad_sch`` names its children (``Sheetfile``) but not its parent, so a
child sheet cannot know its own root — yet the connectivity/net-diff gates are
only honest when they evaluate the WHOLE tree (a child read standalone is a
truncated graph: its global labels lose every member contributed by sibling
sheets). This module answers two questions for the draw pipeline:

* :func:`resolve_root` — given any ``.kicad_sch``, which file is the root of
  the tree it belongs to? (Reverse index over the project directory; explicit
  ``--root`` always wins; several claiming roots is a refusal, never a guess.)
* :func:`stage_tree` — copy the tree into a shadow directory preserving the
  relative layout, so a dry-applied child can be read back THROUGH its root
  (``Sheetfile`` paths are relative; a lone temp next to the target cannot
  represent "the tree with this one file changed").

Read-only by design: nothing here writes to a schematic. The multi-file write
transaction builds on this layer (0.21 track).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .errors import AkcliError, fail
from .readers import kicad as _kreader

__all__ = ["resolve_root", "stage_tree", "tree_files"]

# Same cap as the reader's sheet recursion (a real design is 2-4 deep).
_MAX_DEPTH = 16

# How far above the target to look for the project directory (`.kicad_pro`).
# A child sheet normally lives in the project directory itself or one
# subdirectory down; an unbounded climb could rglob someone's whole home.
_MAX_PRO_CLIMB = 3


def _refs_of(path: Path) -> list[Path]:
    """Resolved child paths referenced by ``path`` (unreadable file -> [])."""
    try:
        _root_uuid, refs = _kreader.sheet_refs(path)
    except (AkcliError, OSError, UnicodeDecodeError):
        # A broken NEIGHBOR must never block the target's own flow: the
        # reverse index simply doesn't learn this file's children.
        return []
    out: list[Path] = []
    for _uuid, _name, sfile in refs:
        if sfile:
            try:
                out.append((path.parent / sfile).resolve())
            except OSError:  # pragma: no cover - unresolvable path text
                continue
    return out


def tree_files(root: Path) -> list[Path]:
    """Every reachable ``.kicad_sch`` from ``root`` (root first, then children).

    Cycle- and depth-guarded; a referenced child that does not exist on disk is
    skipped here — :func:`readers.kicad.read_sch` surfaces the miss with its
    own precise error when the tree is actually read.
    """
    start = Path(root).resolve()
    seen: list[Path] = []

    def walk(p: Path, depth: int) -> None:
        if p in seen or depth > _MAX_DEPTH:
            return
        seen.append(p)
        for child in _refs_of(p):
            if child.exists():
                walk(child, depth + 1)

    walk(start, 0)
    return seen


def _keep(f: Path) -> bool:
    """Exclude editor droppings (``~`` locks, dotted temps) and ``.akcli``."""
    return not f.name.startswith((".", "~")) and ".akcli" not in f.parts


def _scan_scope(target: Path) -> tuple[list[Path], list[Path]]:
    """Files that may parent ``target``: ``(kicad_pro files, schematics)``.

    Recursive scan of the project directory (nearest ancestor holding a
    ``.kicad_pro``, else the target's own directory) PLUS a shallow,
    non-recursive look at the bounded ancestor chain — a root normally sits at
    the project top while its children live in subdirectories, and the shallow
    climb finds it without ever rglob-ing someone's home directory.
    """
    chain: list[Path] = []
    anc = target.parent
    for _ in range(_MAX_PRO_CLIMB):
        chain.append(anc)
        if anc.parent == anc:
            break
        anc = anc.parent
    base = next((d for d in chain if any(d.glob("*.kicad_pro"))), target.parent)
    files = {f.resolve() for f in base.rglob("*.kicad_sch") if _keep(f)}
    pros = {f.resolve() for f in base.glob("*.kicad_pro")}
    for d in chain:
        files.update(f.resolve() for f in d.glob("*.kicad_sch") if _keep(f))
        pros.update(f.resolve() for f in d.glob("*.kicad_pro"))
    return sorted(pros), sorted(files)


def resolve_root(target: Path, explicit: Path | None = None) -> Path:
    """The root of the sheet tree ``target`` belongs to.

    ``explicit`` (the ``--root`` flag) always wins but is validated: naming a
    root whose tree does not contain the target is ``PROJECT_ROOT_MISMATCH``.
    Otherwise the project directory is reverse-indexed; a file no other file
    references is its own root (the flat case — zero behaviour change), and
    several claiming roots refuse with ``PROJECT_ROOT_AMBIGUOUS`` unless a
    single ``.kicad_pro`` stem picks one (the KiCad-project tie-break).
    """
    target = Path(target).resolve()
    if explicit is not None:
        explicit = Path(explicit).resolve()
        if target not in tree_files(explicit):
            fail("PROJECT_ROOT_MISMATCH",
                 f"{target.name} is not in the sheet tree rooted at {explicit}")
        return explicit

    pros, candidates = _scan_scope(target)
    parents: dict[Path, set[Path]] = {}
    for f in candidates:
        for child in _refs_of(f):
            parents.setdefault(child, set()).add(f)

    if target not in parents:
        return target

    roots: set[Path] = set()

    def climb(p: Path, depth: int, trail: frozenset) -> None:
        ups = {u for u in parents.get(p, set()) if u not in trail}
        if not ups or depth >= _MAX_DEPTH:
            roots.add(p)
            return
        for u in ups:
            climb(u, depth + 1, trail | {u})

    climb(target, 0, frozenset({target}))
    # Forward validation: the reverse index can hold stale references, so a
    # claimed root must actually reach the target through readable files.
    roots = {r for r in roots if r != target and target in tree_files(r)}
    if not roots:
        return target
    if len(roots) == 1:
        return next(iter(roots))

    if len(pros) == 1:
        matched = [r for r in roots if r.stem == pros[0].stem]
        if len(matched) == 1:
            return matched[0]
    names = ", ".join(sorted(r.name for r in roots))
    fail("PROJECT_ROOT_AMBIGUOUS",
         f"{target.name} is instantiated by several roots ({names}); "
         "pass --root FILE to choose the tree to evaluate")
    raise AssertionError("unreachable")  # fail() always raises


def stage_tree(files: list[Path], base: Path) -> dict[Path, Path]:
    """Copy ``files`` into ``base`` preserving their relative layout.

    Returns ``real -> staged`` (resolved keys). The anchor is the files'
    common ancestor, so ``Sheetfile "../common/x.kicad_sch"`` references keep
    resolving inside the stage. Bytes are copied verbatim (binary), never
    re-encoded. Raises ``ValueError`` when no common anchor exists (e.g.
    different drives on Windows) — the caller degrades honestly.
    """
    resolved = [Path(f).resolve() for f in files]
    anchor = Path(os.path.commonpath([str(f) for f in resolved]))
    if anchor.is_file():  # single-file tree: commonpath is the file itself
        anchor = anchor.parent
    mapping: dict[Path, Path] = {}
    for f in resolved:
        dest = base / f.relative_to(anchor)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        mapping[f] = dest
    return mapping
