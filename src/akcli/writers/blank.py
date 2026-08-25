"""Blank-document bootstrap: the smallest valid KiCad *project* akcli authors.

``akcli new`` needs a document ``draw`` can immediately append to, which is a
*writer* concern (it emits KiCad s-expressions), not a CLI one — the command
module only picks the paper size and the title. Keeping the emitter here puts
it next to the executor that appends to its output, and next to the ONE
quoting authority (:func:`..sexpr.build._q`) it must share: a hand-rolled
escaper in the command module once wrote a title eeschema refused to open
while every akcli gate reported success.

``akcli project new`` needs the same thing for the four *other* files a KiCad
project is made of (``.kicad_pro``, ``.kicad_pcb``, the two lib tables) plus a
``.gitignore``, so they live here too — one module owns "what a blank KiCad
document looks like", and :mod:`..scaffold` owns "which of them to write".

Ground truth, not invention
---------------------------
Every byte below is transcribed from a KiCad 10 install, because a
hand-guessed header is exactly the class of claim akcli refuses to make
(fixture-first: ``tests/fixtures/kicad/blank_project/``, proven against a real
``kicad-cli`` in ``tests/test_e2e_project.py``):

* :data:`_PRO_TEMPLATE` is KiCad's own ``template/kicad.kicad_pro`` — the 552
  bytes "File > New Project" copies — with only ``meta.filename`` and
  ``sheets`` filled in. Everything else (netclasses, board design settings)
  KiCad materialises itself on first open; writing our guesses instead would
  be worse than writing nothing.
* the board header and :data:`_PCB_LAYERS` are transcribed from
  ``template/Arduino_Mega/Arduino_Mega.kicad_pcb`` (file version 20241229,
  the *new* layer numbering: F.Cu 0, B.Cu 2, …). The older 20240108 (KiCad 8)
  encoding is deliberately NOT used: its layer IDs have no local oracle here,
  and an unprovable header is not shipped. The cost is documented — KiCad 8
  cannot open this board (the schematic stays KiCad 8 format and opens fine).

The board is a **container, not a layout**: zero footprints, zero tracks, no
outline. akcli does not author PCB content (SPEC non-goal); it only makes sure
that opening the project in pcbnew finds a board instead of a prompt.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

# ONE quoting authority for every s-expression atom akcli writes (see
# tests/test_sexpr_quoting.py — this import IS the gate's subject).
from ..sexpr.build import _q as _sexpr_quote

# KiCad standard paper sizes (eeschema "paper" token). Portrait names only; the
# format matches what `read`/`draw` accept and what eeschema writes.
KICAD_PAPERS: tuple[str, ...] = (
    "A0", "A1", "A2", "A3", "A4", "A5",
    "A", "B", "C", "D", "E",
    "USLetter", "USLegal", "USLedger",
)
# Mirror the KiCad 8 header the reader/writer already round-trip (see fixtures).
NEW_SCH_VERSION = 20231120
NEW_SCH_GENERATOR = "eeschema"
NEW_SCH_GENERATOR_VERSION = "8.0"


def blank_sch(paper: str, title: str | None, *, uuid: str | None = None) -> str:
    """Render a minimal valid ``.kicad_sch`` the reader and ``draw`` accept.

    version/generator/uuid/paper plus an empty ``lib_symbols`` and a root
    ``sheet_instances`` page — the smallest document eeschema opens without a
    repair prompt and that ``draw`` can immediately append symbols/wires to.

    ``uuid`` pins the root-sheet UUID instead of minting a fresh one. That is
    what binds a scaffolded project together: the ``.kicad_pro``'s
    ``sheets[0][0]`` must be the root schematic's own ``(uuid ...)``, and the
    only way to write both files in one pass is for the caller to choose the
    value once and hand it to both emitters.
    """
    import uuid as _uuid

    root_uuid = uuid or str(_uuid.uuid4())
    lines = [
        "(kicad_sch",
        f"\t(version {NEW_SCH_VERSION})",
        f'\t(generator "{NEW_SCH_GENERATOR}")',
        f'\t(generator_version "{NEW_SCH_GENERATOR_VERSION}")',
        f'\t(uuid "{root_uuid}")',
        f'\t(paper "{paper}")',
    ]
    if title:
        lines += [
            "\t(title_block",
            f'\t\t(title {_sexpr_quote(title)})',
            "\t)",
        ]
    lines += [
        "\t(lib_symbols)",
        '\t(sheet_instances',
        '\t\t(path "/"',
        '\t\t\t(page "1")',
        "\t\t)",
        "\t)",
        ")",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# .kicad_pro — KiCad's own blank-project template, verbatim
# --------------------------------------------------------------------------- #
# Transcribed from <KiCad>/share/kicad/template/kicad.kicad_pro (KiCad 10).
# DO NOT "improve" this: an empty `net_settings.classes` is what makes KiCad
# materialise its own Default netclass on first open, and inventing board
# design settings here would freeze guesses into every project akcli creates.
# `meta.version` of the project-file schema itself (KiCad 6..10 all write 1).
NEW_PRO_META_VERSION = 1
# The name KiCad gives the root sheet's entry in `sheets` (page 1).
ROOT_SHEET_NAME = "Root"

_PRO_TEMPLATE: dict[str, Any] = {
    "board": {
        "design_settings": {
            "defaults": {},
            "diff_pair_dimensions": [],
            "drc_exclusions": [],
            "rules": {},
            "track_widths": [],
            "via_dimensions": [],
        },
    },
    "boards": [],
    "libraries": {
        "pinned_footprint_libs": [],
        "pinned_symbol_libs": [],
    },
    "meta": {"filename": "kicad.kicad_pro", "version": NEW_PRO_META_VERSION},
    "net_settings": {"classes": [], "meta": {"version": 0}},
    "pcbnew": {"page_layout_descr_file": ""},
    "sheets": [],
    "text_variables": {},
}


def blank_pro(project_filename: str, root_sch_uuid: str) -> str:
    """Render a minimal valid ``.kicad_pro`` bound to ``root_sch_uuid``.

    ``project_filename`` is the *bare name* KiCad stores in ``meta.filename``
    (e.g. ``"demo.kicad_pro"``), never a path — KiCad writes the basename and
    warns about a stale one.

    ``sheets`` is the load-bearing field: ``[[<root schematic uuid>, "Root"]]``
    is the binding that makes this a project rather than a directory of files,
    and it is why :func:`blank_sch` takes a ``uuid`` — the same value must
    appear in both documents.

    Rendered with ``sort_keys=True`` because KiCad writes its own JSON key-
    sorted at two-space indent: matching that exactly means a project akcli
    creates and a project KiCad rewrites differ only where content differs,
    so a diff is readable and this emitter is byte-stable.
    """
    doc = copy.deepcopy(_PRO_TEMPLATE)
    doc["meta"]["filename"] = project_filename
    doc["sheets"] = [[root_sch_uuid, ROOT_SHEET_NAME]]
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# .kicad_pcb — a blank board CONTAINER (never a layout)
# --------------------------------------------------------------------------- #
NEW_PCB_VERSION = 20241229
NEW_PCB_GENERATOR = "pcbnew"
NEW_PCB_GENERATOR_VERSION = "9.0"
# Default board thickness in mm (KiCad's own default; the template's value).
NEW_PCB_THICKNESS = 1.6

# The two-layer stack, transcribed verbatim from the KiCad 10 template
# (new-style layer numbering: copper is even-from-0 front→back, so F.Cu 0 /
# B.Cu 2, and the technical layers keep their template order — pcbnew is
# order-sensitive when it re-reads the block). Kept as a module constant so
# adding inner copper later is a pure ADDITION here, with its own fixture.
_PCB_LAYERS: tuple[str, ...] = (
    '(0 "F.Cu" signal)',
    '(2 "B.Cu" signal)',
    '(9 "F.Adhes" user "F.Adhesive")',
    '(11 "B.Adhes" user "B.Adhesive")',
    '(13 "F.Paste" user)',
    '(15 "B.Paste" user)',
    '(5 "F.SilkS" user "F.Silkscreen")',
    '(7 "B.SilkS" user "B.Silkscreen")',
    '(1 "F.Mask" user)',
    '(3 "B.Mask" user)',
    '(17 "Dwgs.User" user "User.Drawings")',
    '(19 "Cmts.User" user "User.Comments")',
    '(21 "Eco1.User" user "User.Eco1")',
    '(23 "Eco2.User" user "User.Eco2")',
    '(25 "Edge.Cuts" user)',
    '(27 "Margin" user)',
    '(31 "F.CrtYd" user "F.Courtyard")',
    '(29 "B.CrtYd" user "B.Courtyard")',
    '(35 "F.Fab" user)',
    '(33 "B.Fab" user)',
)


def blank_pcb(paper: str = "A4") -> str:
    """Render an empty two-layer ``.kicad_pcb`` container.

    Zero footprints, zero tracks, no board outline — this is the file pcbnew
    opens instead of prompting, NOT a layout. akcli does not author PCB
    content; `kicad-cli pcb drc` on this board reports the one expected
    ``invalid_outline`` violation (there is no outline to have), which is why
    the e2e gate asserts the tool's exit status, not an empty report.

    ``(setup ...)`` is deliberately minimal: pcbnew fills in the stackup,
    plot params and origins on first save, and any value invented here would
    silently become every akcli project's default.
    """
    lines = [
        "(kicad_pcb",
        f"\t(version {NEW_PCB_VERSION})",
        f'\t(generator "{NEW_PCB_GENERATOR}")',
        f'\t(generator_version "{NEW_PCB_GENERATOR_VERSION}")',
        "\t(general",
        f"\t\t(thickness {NEW_PCB_THICKNESS})",
        "\t\t(legacy_teardrops no)",
        "\t)",
        f'\t(paper "{paper}")',
        "\t(layers",
    ]
    lines += [f"\t\t{row}" for row in _PCB_LAYERS]
    lines += [
        "\t)",
        "\t(setup",
        "\t\t(pad_to_mask_clearance 0)",
        "\t\t(allow_soldermask_bridges_in_footprints no)",
        "\t)",
        '\t(net 0 "")',
        ")",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# sym-lib-table / fp-lib-table
# --------------------------------------------------------------------------- #
# KiCad 8 and 10 both write `(version 7)` in a project-local table.
LIB_TABLE_VERSION = 7
_LIB_TABLE_TAG = {"sym": "sym_lib_table", "fp": "fp_lib_table"}


def blank_lib_table(kind: Literal["sym", "fp"]) -> str:
    """Render an empty project-local ``sym-lib-table`` / ``fp-lib-table``.

    Empty is the point: an EXISTING table with zero entries parses to zero
    libraries (``libtable.read_table``) and silences the ``LIB_TABLE_MISSING``
    note, while global-table nicknames keep resolving exactly as before. A
    project-local table is also the file a later ``library`` command edits, so
    creating it now means the first added library is an append, not a
    bootstrap.
    """
    tag = _LIB_TABLE_TAG[kind]
    return f"({tag}\n\t(version {LIB_TABLE_VERSION})\n)\n"


# --------------------------------------------------------------------------- #
# .gitignore
# --------------------------------------------------------------------------- #
# KiCad's own churn: autosaves, GUI locks, backups, the per-user .kicad_prl,
# the footprint cache — plus akcli's `.akcli/` state root (which self-ignores
# too, so this entry is a courtesy for readers of the file, not a requirement).
_GITIGNORE_LINES: tuple[str, ...] = (
    "# KiCad churn (autosaves, GUI locks, backups, per-user state)",
    "*-backups/",
    "*.kicad_prl",
    "fp-info-cache",
    "~*.lck",
    "_autosave-*",
    "*-save.kicad_*",
    "*.kicad_pcb-bak",
    "*.kicad_sch-bak",
    "",
    "# akcli workspace state (journal + rotated backups)",
    ".akcli/",
)


def kicad_gitignore() -> str:
    """Render the ``.gitignore`` body for a KiCad project directory.

    Only ever written when the directory has NO ``.gitignore``: an existing
    one is user-managed content and is left untouched (the same rule
    :func:`..safety.ensure_state_root_ignore` follows for the state root).
    """
    return "\n".join(_GITIGNORE_LINES) + "\n"
