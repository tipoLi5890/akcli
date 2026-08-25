"""Project scaffolding engine: which KiCad files a directory is missing, and
how to put them there without ever half-writing a project.

:mod:`.writers.blank` knows what a blank KiCad document looks like; this module
knows *which* documents a project needs, *whether* each one is already there,
and how to land the whole set atomically. ``akcli project new`` and ``akcli
project init`` are two argument sets over the same two functions:

* :func:`plan` is **pure** — it reads the directory (and, for ``init``, the
  existing root schematic's UUID) and returns a :class:`Plan` describing what
  each file's fate would be. It touches nothing. ``--dry-run`` is therefore
  not a second code path pretending to be the first: it is the first one,
  stopped before :func:`apply`.
* :func:`apply` writes, all-or-nothing.

Why all-or-nothing
------------------
A KiCad project is a *set* of files that reference each other: the
``.kicad_pro``'s ``sheets[0][0]`` must be the root ``.kicad_sch``'s own
``(uuid ...)``. A partial scaffold — pro written, schematic not — is worse
than no scaffold at all, because KiCad opens it and silently repairs it into
something that no longer matches what akcli recorded. So:

1. **Preflight the whole set.** Any planned ``conflict`` refuses with
   ``PROJECT_EXISTS`` before a single byte is written (and before ``mkdir``).
2. **Stage every file, then promote.** Each write goes through
   :func:`.writers.atomic.prepare` (binary temp in the target's own directory,
   fsync'd) and only then through ``commit`` — the two-phase-commit shape the
   multi-sheet write transaction already uses.
3. **Roll back on a partial commit.** If commit N fails, the ``created`` files
   already promoted are unlinked and the ``overwritten`` ones are restored
   from the backup ``commit`` itself just took. The directory ends up as it
   started, and the original error propagates.

This is NOT ``writers/txn.py``: that transaction is bound to an op-list run
over a sheet tree (net gate, per-file snapshots, run ids). Scaffolding has no
op-list, no nets and no prior content to diff — sharing the machinery would
mean teaching it that a ``.gitignore`` is a schematic.

Never-overwrite rules
---------------------
``.gitignore`` is user-managed content: an existing one is ``kept`` even under
``--force`` (same rule :func:`.safety.ensure_state_root_ignore` follows). An
existing ``.kicad_pro`` is ``kept`` by ``init`` too — but it is *validated*
(``json.loads``) first, so ``init`` never reports a healthy project over a
truncated file; a malformed one is ``PROJECT_FILE_INVALID``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from . import examples as _examples
from . import journal, project, safety
from .errors import AkcliError, fail
from .writers import atomic, blank, identity

__all__ = [
    "ACTIONS",
    "FILE_KINDS",
    "Plan",
    "PlannedFile",
    "apply",
    "plan",
    "record",
    "sheet_names",
    "status_of",
]

# Commit order, and the order files are reported in. The project file goes
# first so that a crash between commits leaves at worst a project pointing at
# a schematic that is about to appear (KiCad recreates a missing sheet), never
# a schematic no project knows about.
FILE_KINDS: tuple[str, ...] = (
    "pro", "sch", "sheet", "pcb", "sym-lib-table", "fp-lib-table", "ops",
    "gitignore",
)
_KIND_RANK = {kind: i for i, kind in enumerate(FILE_KINDS)}

# A planned file's fate. ``kept`` and ``conflict`` are both "it is already
# there"; the difference is whether this mode is allowed to leave it alone.
ACTIONS: tuple[str, ...] = ("created", "kept", "overwritten", "conflict")

Mode = Literal["new", "init"]


@dataclass(frozen=True)
class PlannedFile:
    """One file's fate: its name, what kind of KiCad file it is, and its bytes.

    ``content`` is ``None`` for ``kept``/``conflict`` entries — nothing will be
    written, so nothing was rendered.
    """

    name: str
    kind: str
    action: str
    content: str | None = None

    def to_json(self, directory: Path) -> dict:
        return {
            "path": (directory / self.name).as_posix(),
            "name": self.name,
            "kind": self.kind,
            "action": self.action,
        }


@dataclass
class Plan:
    """What :func:`apply` would do to ``directory`` — the whole set, at once."""

    directory: Path
    name: str
    mode: str
    force: bool
    project_file: Path
    root_sch: Path
    root_uuid: str
    files: list[PlannedFile] = field(default_factory=list)

    def _names(self, action: str) -> list[str]:
        return [f.name for f in self.files if f.action == action]

    @property
    def created(self) -> list[str]:
        return self._names("created")

    @property
    def kept(self) -> list[str]:
        return self._names("kept")

    @property
    def overwritten(self) -> list[str]:
        return self._names("overwritten")

    @property
    def conflicts(self) -> list[str]:
        return self._names("conflict")

    @property
    def writes(self) -> list[PlannedFile]:
        """The files :func:`apply` would actually write, in commit order."""
        return [f for f in self.files if f.action in ("created", "overwritten")]

    def to_json(self) -> dict:
        """JSON view. Every path is ``as_posix()`` — a Windows backslash in a
        payload breaks byte-stable snapshots and JSON-escapes into ``\\\\``."""
        return {
            "mode": self.mode,
            "directory": self.directory.as_posix(),
            "name": self.name,
            "project_file": self.project_file.as_posix(),
            "root_sch": self.root_sch.as_posix(),
            "root_uuid": self.root_uuid,
            "files": [f.to_json(self.directory) for f in self.files],
            "created": self.created,
            "kept": self.kept,
            "overwritten": self.overwritten,
            "conflicts": self.conflicts,
        }


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _keep_sch(p: Path) -> bool:
    """Ignore KiCad's own churn when looking for the root schematic."""
    name = p.name
    return (p.is_file()
            and not name.startswith("~")
            and not name.startswith("_autosave-")
            and not name.endswith("-save.kicad_sch"))


def _existing_pros(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.kicad_pro") if p.is_file())


def _existing_schs(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.kicad_sch") if _keep_sch(p))


def _read_pro(path: Path) -> dict:
    """Parse an existing ``.kicad_pro``; a malformed one is fatal, not ignored.

    ``init`` reports an existing project file as ``kept``, which is a claim
    about the *project*, not just about a filename — so the claim is checked.
    """
    text = safety.read_text_guarded(path)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        fail("PROJECT_FILE_INVALID",
             f"{path.name} is not valid JSON ({exc.msg} at line {exc.lineno})")
    if not isinstance(doc, dict):
        fail("PROJECT_FILE_INVALID",
             f"{path.name} is not a KiCad project object (got "
             f"{type(doc).__name__})")
    return doc


def _child_emitter(paper: str, child_uuid: str) -> Callable[[], str]:
    """Bind the per-child uuid eagerly (a bare lambda in the loop would not)."""
    return lambda: blank.blank_sch(paper, None, uuid=child_uuid)


def _root_uuid_of(sch: Path) -> str:
    """The root-sheet UUID of an existing schematic (``PROJECT_FILE_INVALID``
    when the file is not a ``kicad_sch`` document at all)."""
    from .sexpr import parse as _sexpr_parse
    from .sexpr import query as _q

    doc = _sexpr_parse(safety.read_text_guarded(sch))
    if doc.tag != "kicad_sch":
        fail("PROJECT_FILE_INVALID",
             f"{sch.name} is not a schematic document (root tag "
             f"{doc.tag!r}, expected 'kicad_sch')")
    return _q.require_root_uuid(doc)


def _resolve_root_sch(directory: Path, explicit: Path | None) -> Path:
    """The root schematic ``init`` should bind the project to."""
    if explicit is not None:
        root = explicit if explicit.is_absolute() else directory / explicit
        if not root.is_file():
            fail("PROJECT_FILE_MISSING", f"--root {root} does not exist")
        return root
    candidates = _existing_schs(directory)
    if not candidates:
        fail("PROJECT_FILE_MISSING",
             f"no .kicad_sch in {directory} — `akcli project new` creates a "
             "project from scratch, `project init` only completes an existing one")
    if len(candidates) == 1:
        return candidates[0]
    roots = [c for c in candidates if project.resolve_root(c) == c.resolve()]
    if len(roots) == 1:
        return roots[0]
    names = ", ".join(sorted(c.name for c in (roots or candidates)))
    fail("PROJECT_ROOT_AMBIGUOUS",
         f"{directory} holds several root schematics ({names}); pass "
         "--root FILE to name the one the project should open")


def _resolve_name(directory: Path, mode: str, explicit: str | None,
                  root_sch: Path | None) -> str:
    """Project name: ``--name`` > the existing project/root stem > dir name."""
    if explicit:
        return explicit
    if mode == "init":
        pros = _existing_pros(directory)
        if len(pros) == 1:
            return pros[0].stem
        if root_sch is not None:
            return root_sch.stem
    return directory.name or "project"


def _fate(path: Path, *, mode: str, force: bool, never_overwrite: bool = False) -> str:
    """What happens to ``path``: created / kept / overwritten / conflict."""
    if not path.exists():
        return "created"
    if mode == "init" or never_overwrite:
        return "kept"
    return "overwritten" if force else "conflict"


def plan(
    directory: Path | str,
    *,
    mode: Mode = "new",
    name: str | None = None,
    paper: str = "A4",
    title: str | None = None,
    force: bool = False,
    no_pcb: bool = False,
    no_lib_tables: bool = False,
    no_gitignore: bool = False,
    sheets: Sequence[str] = (),
    root: Path | None = None,
    example: str | None = None,
) -> Plan:
    """Decide the fate of every file in a scaffolded project. Writes nothing.

    ``mode="new"`` renders a fresh project (a fresh root UUID); ``mode="init"``
    adopts what is already there — the existing root schematic keeps its UUID,
    so the ``.kicad_pro`` this creates points at the design the user already
    has instead of orphaning it.

    ``example`` names a packaged op-list (:mod:`.examples`) to drop in as
    ``ops.json``. It is planned like every other file — an existing ``ops.json``
    is a ``conflict``, not a silent overwrite — because that file is the user's
    design intent, and the one thing a scaffolding command must never do is
    replace it.
    """
    directory = Path(directory).resolve()

    root_sch: Path
    root_uuid: str
    if mode == "init":
        root_sch = _resolve_root_sch(directory, root)
        root_uuid = _root_uuid_of(root_sch)
        name = _resolve_name(directory, mode, name, root_sch)
    else:
        name = _resolve_name(directory, mode, name, None)
        root_sch = directory / f"{name}.kicad_sch"
        # An existing root schematic keeps its identity even under --force:
        # re-minting the UUID would orphan every `instances` path already
        # written into the design's child sheets. Best-effort on purpose —
        # `new` is authoritative, so an unreadable file there must not change
        # WHICH error the caller sees (a conflict, or nothing at all under
        # --force) into a parse error about a file being replaced anyway.
        root_uuid = str(_uuid.uuid4())
        if root_sch.is_file():
            try:
                root_uuid = _root_uuid_of(root_sch)
            except AkcliError:
                pass

    project_file = directory / f"{name}.kicad_pro"
    files: list[PlannedFile] = []
    seen: set[str] = set()

    def add(path: Path, kind: str, render: Callable[[], str], *,
            never_overwrite: bool = False) -> None:
        # One entry per FILE, first claim wins: `--sheets demo` on project
        # `demo` names the root sheet, and planning it twice would stage two
        # writes to one path (the second clobbering the first mid-commit).
        if path.name in seen:
            return
        seen.add(path.name)
        action = _fate(path, mode=mode, force=force,
                       never_overwrite=never_overwrite)
        content = render() if action in ("created", "overwritten") else None
        files.append(PlannedFile(path.name, kind, action, content))

    # An existing .kicad_pro that `init` keeps is validated, never assumed.
    if mode == "init" and project_file.is_file():
        _read_pro(project_file)
    add(project_file, "pro",
        lambda: blank.blank_pro(project_file.name, root_uuid))
    add(root_sch, "sch", lambda: blank.blank_sch(paper, title, uuid=root_uuid))
    for sheet in sheets:
        child = directory / f"{sheet}.kicad_sch"
        # Content-seeded like every other identity akcli mints: the same seed
        # `add_sheet {"create": true}` uses, so a child pre-created here and one
        # created in-transaction from the same root carry the same root uuid.
        child_uuid = identity.node_uuid(
            root_uuid, identity.seed_sheet_file(child.name))
        add(child, "sheet", _child_emitter(paper, child_uuid))
    if not no_pcb:
        add(directory / f"{name}.kicad_pcb", "pcb", lambda: blank.blank_pcb(paper))
    if not no_lib_tables:
        add(directory / "sym-lib-table", "sym-lib-table",
            lambda: blank.blank_lib_table("sym"))
        add(directory / "fp-lib-table", "fp-lib-table",
            lambda: blank.blank_lib_table("fp"))
    if example:
        add(directory / "ops.json", "ops",
            lambda: _examples.ops_text(str(example)))
    if not no_gitignore:
        # user-managed content: never overwritten, not even by --force
        add(directory / ".gitignore", "gitignore", blank.kicad_gitignore,
            never_overwrite=True)

    files.sort(key=lambda f: (_KIND_RANK[f.kind], f.name))
    return Plan(directory=directory, name=name, mode=mode, force=force,
                project_file=project_file, root_sch=root_sch,
                root_uuid=root_uuid, files=files)


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #
def status_of(p: Plan, *, dry_run: bool = False) -> str:
    """``dry-run`` / ``unchanged`` / ``applied`` for one plan."""
    if dry_run:
        return "dry-run"
    return "applied" if p.writes else "unchanged"


def record(p: Plan, status: str, *, dry_run: bool = False) -> None:
    """Append the workspace-journal entry for this scaffold run.

    Skipped when the target directory does not exist yet: the journal lives
    INSIDE the workspace (``<dir>/.akcli/``), so recording a ``--dry-run`` of a
    not-yet-created project would be the one thing a dry run must never do —
    create it. There is no workspace to journal into until :func:`apply` runs.
    """
    if not p.directory.is_dir():
        return
    journal.record(
        p.project_file, f"project {p.mode}", status,
        created=p.created or None,
        kept=p.kept or None,
        overwritten=p.overwritten or None,
        conflicts=p.conflicts or None,
        root_uuid=p.root_uuid,
        dry_run=True if dry_run else None,
    )


def _restore(path: Path, backup_dir: Path) -> None:
    """Put back the pre-write bytes ``commit`` snapshotted for ``path``."""
    import shutil

    bak = backup_dir / atomic.backup_name(path.name, 1)
    if bak.is_file():
        shutil.copy2(bak, path)


def _rollback(done: list[tuple[PlannedFile, Path]], backup_dir: Path | None) -> None:
    """Undo the commits that DID land, newest first."""
    for planned, path in reversed(done):
        try:
            if planned.action == "created":
                os.unlink(path)
            elif backup_dir is not None:
                _restore(path, backup_dir)
        except OSError:
            # Best-effort: the caller re-raises the original failure, which is
            # the one that explains the state. Backups stay on disk either way.
            pass


def apply(p: Plan, *, backup_depth: int = atomic.DEFAULT_BACKUP_DEPTH) -> None:
    """Write the plan, all-or-nothing.

    Refuses the whole run on any ``conflict`` (``PROJECT_EXISTS``) before
    touching the disk, refuses to race the KiCad GUI when it would overwrite a
    file it has open, stages every write before promoting the first, and rolls
    back to the starting state if a promote fails partway.
    """
    if p.conflicts:
        fail("PROJECT_EXISTS",
             f"{p.directory} already has {', '.join(p.conflicts)}")

    writes = p.writes
    overwrites = [f for f in writes if f.action == "overwritten"]
    if overwrites:
        safety.refuse_if_gui_locked(
            [p.directory / f.name for f in overwrites],
            what=f"project {p.mode}")

    p.directory.mkdir(parents=True, exist_ok=True)
    if not writes:
        record(p, "unchanged")
        return

    backup_dir: Path | None = None
    if overwrites:
        backup_dir = journal.ensure_backups_dir(p.project_file)

    prepared: list[tuple[PlannedFile, Path, atomic.PreparedWrite]] = []
    try:
        for planned in writes:
            path = p.directory / planned.name
            assert planned.content is not None  # writes always carry bytes
            prepared.append((planned, path,
                             atomic.prepare(path, planned.content,
                                            _snapshot(path))))
    except BaseException:
        for _planned, _path, pw in prepared:
            atomic.abort(pw)
        raise

    done: list[tuple[PlannedFile, Path]] = []
    try:
        for planned, path, pw in prepared:
            atomic.commit(
                pw,
                backup_dir if planned.action == "overwritten" else None,
                backup_depth)
            done.append((planned, path))
    except BaseException:
        for planned, _path, pw in prepared:
            if not any(planned is d[0] for d in done):
                atomic.abort(pw)
        _rollback(done, backup_dir)
        raise

    record(p, "applied")


def _snapshot(path: Path) -> str:
    """``sha256`` of ``path``'s current bytes (empty when it does not exist).

    :func:`.writers.atomic.prepare` re-checks this immediately before writing,
    so a file that changed between :func:`plan` and :func:`apply` is refused
    (``VERIFY_FAILED``) instead of silently clobbered.
    """
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def sheet_names(raw: Iterable[str] | str | None) -> list[str]:
    """Parse ``--sheets a,b`` into stems, dropping blanks and duplicates."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        for part in str(item).split(","):
            stem = part.strip()
            if stem.lower().endswith(".kicad_sch"):
                stem = stem[: -len(".kicad_sch")]
            if stem and stem not in out:
                out.append(stem)
    return out
