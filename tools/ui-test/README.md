# akcli view — browser UI regression

Drives the **system Chrome** (via `puppeteer-core`, no browser download)
against a fixture-seeded `akcli view` server and asserts ~30 behaviors of both
dashboards: auto-compute, engineering-notation parsing, palette, op-list
export button, diagram annotations, ERC panel/markers/NEW tags, diff ghost,
sheet tabs, note posting, timeline clear, theme, keyboard.

## One-time setup

```sh
cd tools/ui-test && npm install
```

## Run

Via pytest (spins up the server itself; auto-skips when node / puppeteer-core /
Chrome are missing):

```sh
python -m pytest tests/test_webui_browser.py -v
```

Or standalone against any running server:

```sh
AKCLI_VIEW_URL=http://127.0.0.1:8765 node browser_test.mjs
```

Env knobs: `CHROME_PATH` (default: the macOS Google Chrome bundle),
`SHOT_DIR` (write step screenshots there; off when unset).
