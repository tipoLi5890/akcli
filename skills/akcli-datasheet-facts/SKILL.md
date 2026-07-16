---
name: akcli-datasheet-facts
description: >-
  Extract audited, PDF-pinned component facts into the akcli datasheet facts
  store (`akcli review facts`) so review findings upgrade from heuristic to
  datasheet_backed. Use this skill whenever the task involves: reading a
  component datasheet to record its Vref / crystal load capacitance / absolute
  maximum / thermal numbers; building or verifying `datasheets/extracted/`
  facts files; upgrading `akcli review analyze` findings to datasheet_backed;
  or checking why a finding says "verify against the datasheet". Triggers on:
  datasheet facts, facts store, extract datasheet, Vref, load capacitance,
  absolute maximum, theta_ja, 資料表, 元件參數擷取, datasheet_backed,
  review facts.
---

# akcli-datasheet-facts — audited facts extraction, the agent half of the loop

You (the agent) are the extractor: you can read PDFs natively, and the facts
store is designed around that. akcli owns the *discipline* — every fact is
pinned to its source PDF by sha256 + page (+ verbatim quote), and
`review facts verify` audits what you wrote. Your job is to fill the store
honestly; akcli's job is to catch you if you don't.

**The prize:** a heuristic finding ("verify Vref against the datasheet")
becomes a `datasheet_backed` judgement with the exact page as evidence — and
`review propose` can then emit an auto-applicable fix. No fact, no claim.

## Extraction doctrine (non-negotiable)

- **Read the actual PDF.** Never fill a fact from memory, a search snippet, or
  a "typical value" you believe. If you cannot open the PDF, stop and say so.
- **Page-accurate, quote-verbatim.** The `page` is where the number IS, not
  where the section starts. A `quote` is copied character-for-character from
  that page — `verify` runs a text match against the PDF (when `pdftotext` is
  installed) and a mismatch is a WARNING with your name on it.
- **min/typ/max as printed.** Record the row as the table gives it; do not
  average, do not pick a bound and call it `value`. Conditions
  (temperature, load) go in `conditions`.
- **Absence is honest.** A part whose datasheet does not state a number gets
  NO fact for it. Detectors fall back to their heuristics — never to a guess.
- **One MPN, one file.** Facts are keyed by the exact MPN; a variant with
  different numbers (voltage options, grades) is its own facts file.

## Workflow

### Step 1 — Get the PDF (hash included)

```bash
akcli jlc datasheet board.kicad_sch --fetch --out datasheets   # whole-BOM batch into ./datasheets/
akcli jlc datasheet C2984661 --fetch --out datasheets          # or one LCSC part
```

PDFs land under `--out` (default: `AKCLI_DATASHEET_DIR` or
`~/.cache/akcli/datasheets` — pass `--out datasheets` to land them where
`review facts`/`review analyze --facts` auto-discover) named
`C<lcsc>_<MPN>.pdf`. Without `--fetch`, the command only resolves datasheet
URLs and does not download anything. A vendor PDF you were given by the user
works too — put it in `datasheets/` yourself.

### Step 2 — Read the PDF and extract

Open the PDF with your Read tool. Find the electrical-characteristics /
recommended-operating tables. Extract the fact keys the detectors consume:

| key | what | unit | upgrades |
|---|---|---|---|
| `vref` | regulator feedback reference | V | FB-divider review → `REVIEW_FB_DIVIDER_VREF_MISMATCH` + auto retune proposal |
| `load_capacitance` | crystal CL | F | crystal review → `REVIEW_XTAL_LOAD_MISMATCH` + cap-value proposal |
| `abs_max_io` | pin absolute maximum | V | voltage-domain review → verified-tolerant / confirmed-violation |
| `theta_ja` | junction-ambient thermal resistance | K/W | junction-temperature estimate |
| `power_dissipation` | design dissipation (from the review context) | W | junction-temperature estimate |
| `t_j_max` | junction limit | °C | junction-temperature limit |

Any other `lower_snake_case` key is schema-legal — record what the design
needs; detectors ignore what they don't consume yet.

### Step 3 — Record, with the page

```bash
akcli review facts add TPS61023DRLR --pdf datasheets/C123456_TPS61023DRLR.pdf --set vref=0.6V@5
akcli review facts add ABM8-8.000MHZ --pdf datasheets/C123_ABM8.pdf --set load_capacitance=10pF@3 --method llm
```

`--set KEY=VALUE[UNIT]@PAGE` handles engineering notation (`0.6V`, `12pF`,
`3mA`). **Always pass `--method llm` when you extracted the numbers** — the
store records HOW numbers arrived, and lying about it defeats the audit.
For min/typ/max rows and quotes, edit the JSON directly
(`datasheets/extracted/<MPN>.json` — fields `min`/`typ`/`max`/`quote`/
`conditions` per fact), then verify.

### Step 4 — Verify (the honesty gate)

```bash
akcli review facts verify
akcli review facts lookup TPS61023DRLR vref
```

`verify` audits: schema shape, the PDF exists, **sha256 staleness** (the PDF
changed since extraction → ERROR: re-verify every fact), page bounds, and
quote presence via the optional `pdftotext` driver. `FACTS_QUOTE_UNVERIFIED`
(NOTE) means the tool is absent — the check did not run; say so in your
report rather than claiming verification.

### Step 5 — Re-analyze and watch findings upgrade

```bash
akcli review analyze board.kicad_sch --facts datasheets --json
```

(`--facts` is auto-discovered when `datasheets/extracted/` sits next to the
schematic.) Compare confidence fields before/after: `heuristic` findings that
consumed your facts now read `datasheet_backed`, carry
`evidence.datasheet.{sha256,page,quote}`, and — for value fixes — produce
auto-applicable `review propose` drafts whose contract fragments cite the
same page. That is the sedimentation chain: PDF → fact → finding → proposal
→ contract evidence.

## Failure modes to refuse

- Filling `vref=1.25V` "because most shunt references are" — that is a guess
  wearing a fact's clothes; the whole store exists to prevent it.
- Quoting a *search result* or another part's datasheet.
- Bumping a stale sha256 by re-running `facts add` without re-checking every
  recorded number against the NEW pdf revision.
- Marking `--method manual` for numbers you extracted (audit trail lies).

## When NOT to use this skill

- Sourcing/purchasability questions → akcli-parts-sourcing.
- Producing LLM *review findings* (not facts) → akcli-deep-review; facts feed
  it, but candidates go through `review validate`, not the facts store.
- Running the review itself → akcli-schematic-review.
