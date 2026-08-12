"""Blank-sheet bootstrap: the smallest valid ``.kicad_sch`` akcli will author.

``akcli new`` needs a document ``draw`` can immediately append to, which is a
*writer* concern (it emits KiCad s-expressions), not a CLI one — the command
module only picks the paper size and the title. Keeping the emitter here puts
it next to the executor that appends to its output, and next to the ONE
quoting authority (:func:`..sexpr.build._q`) it must share: a hand-rolled
escaper in the command module once wrote a title eeschema refused to open
while every akcli gate reported success.
"""

from __future__ import annotations

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


def blank_sch(paper: str, title: str | None) -> str:
    """Render a minimal valid ``.kicad_sch`` the reader and ``draw`` accept.

    version/generator/uuid/paper plus an empty ``lib_symbols`` and a root
    ``sheet_instances`` page — the smallest document eeschema opens without a
    repair prompt and that ``draw`` can immediately append symbols/wires to.
    """
    import uuid as _uuid

    lines = [
        "(kicad_sch",
        f"\t(version {NEW_SCH_VERSION})",
        f'\t(generator "{NEW_SCH_GENERATOR}")',
        f'\t(generator_version "{NEW_SCH_GENERATOR_VERSION}")',
        f'\t(uuid "{_uuid.uuid4()}")',
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
