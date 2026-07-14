---
description: Diff two schematic revisions with akcli (nets matched by membership, components by UniqueID/signature) and summarize what changed.
argument-hint: <schematic_a> <schematic_b>
---

Diff two schematic revisions with `akcli diff` and summarize the changes.

Arguments: `$ARGUMENTS`
- First token = revision A (older), second = revision B (newer). Accept Altium `.SchDoc` and/or
  KiCad `.kicad_sch` (cross-format is allowed; flag low confidence for cross-revision matches).

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. Run `akcli diff <schematic_a> <schematic_b> --exit-zero` (add `--json` to parse the report).
2. If you need to explain a specific net or component change, pull detail with
   `akcli net <file> <name> --json` or `akcli component <file> <REF>`.

Report:
- Added / removed / changed **nets** — and note `akcli` matches nets by **membership**, not display
  name, so a pure rename shows as a name change on the same net, while a membership change is a real
  topology change. Call these out separately.
- Added / removed / changed **components** (matched by UniqueID, then `(value, footprint,
  pin-count)` signature, then refdes).
- State the confidence caveat for cross-revision / cross-format diffs.
- This command is read-only; do not modify files.
