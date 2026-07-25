---
description: Resolve and download a part's datasheet PDF (LCSC/EasyEDA-backed), then optionally pin audited numbers into the project facts store so reviews become datasheet_backed. Networked + writes files — user-triggered only.
argument-hint: <C-number|MPN|board.kicad_sch> [--facts "vref=0.6V@5 ..."] [--out datasheets]
disable-model-invocation: true
---

Fetch the datasheet(s) for a part — or every BOM line of a schematic — with `akcli jlc
datasheet`, and optionally turn read numbers into audited facts. **Networked and writes
files — user-triggered only.**

Arguments: `$ARGUMENTS`
- First token = an LCSC `C`-number, an exact MPN, or a `.kicad_sch` (whole-BOM mode).
- `--facts "key=value@page ..."` = after reading the PDF, pin these numbers into the
  facts store (each value carries its page; the PDF's sha256 is recorded).
- `--out DIR` = download directory (default here: `datasheets`, project-local).

Steps (use the Bash tool; `akcli` is on PATH, else `PYTHONPATH=src python3 -m akcli`):

1. Resolve first, fetch second:
   `akcli jlc datasheet <target>` shows the PDF URL + MPN/manufacturer;
   `akcli jlc datasheet <target> --fetch --out datasheets` downloads into the
   **project-local** `./datasheets` (that is where `review analyze` auto-discovers facts —
   never leave review-decisive PDFs in a personal cache). MPN-only BOM lines get a catalog
   lookup with `--resolve-mpn`.
2. Read the PDF (the Read tool renders it) for the numbers the design depends on:
   absolute maximums, recommended operating conditions, reference voltages, pinout.
3. To make those numbers review-grade, add them as audited facts
   (`akcli review facts add <MPN> --pdf datasheets/<file>.pdf --set vref=0.6V@5 ...`) —
   each fact is sha256+page pinned, and `akcli review analyze` upgrades matching findings
   to `datasheet_backed`. The component must carry the MPN (parameter or exact Value);
   `review analyze` reports `REVIEW_FACTS_UNMATCHED` when a facts file matches nothing.

Report:
- Which PDFs landed where (or `no-link`/`fetch-failed` rows, verbatim), which facts were
  pinned with their pages, and any `REVIEW_FACTS_UNMATCHED` warnings.
- Never quote a datasheet number you did not read from the PDF.
