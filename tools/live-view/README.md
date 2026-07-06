# live-view — a local dashboard that watches a `.kicad_sch` while akcli draws it

Every time the watched schematic changes on disk (e.g. an `akcli draw --apply`),
the watcher exports an SVG with `kicad-cli`, runs KiCad's ERC, counts parts/nets
with `akcli`, and appends a step to a timeline. The dashboard renders the SVG
**inline** (auto-cropped to the drawn content via `getBBox()`), with zoom/pan,
per-step ERC badges, and a follow-live mode.

## Run

```bash
# 1. serve the dashboard (from this directory)
python3 -m http.server 8765 --bind 127.0.0.1 --directory .

# 2. watch a schematic (writes state.json + step-N.svg next to this script)
python3 watcher.py /path/to/board.kicad_sch

# 3. open http://127.0.0.1:8765
```

- Requires `kicad-cli` (KiCad 8+) for SVG export + JSON ERC. Auto-detected from
  `PATH` or the macOS app bundle; override with `KICAD_CLI=/path/to/kicad-cli`.
- `akcli` resolves to this repo's `bin/akcli`; override with `AKCLI=...`.
- Annotate the NEXT step by writing a one-line `note.txt` next to the watcher
  before applying (consumed once, only by a step that has content).
- `AUTO_REVERT=1` additionally asks an open KiCad Schematic Editor to
  File→Revert after each step (macOS AppleScript; needs Accessibility
  permission for your terminal, otherwise it is silently skipped).

State is plain files (`state.json`, `step-N.svg`) — delete them to reset the
timeline. Everything binds to localhost only.
