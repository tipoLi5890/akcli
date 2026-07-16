#!/usr/bin/env python3
"""Dev-only corpus replay: review-finding drift across a schematic corpus.

Usage::

    python tools/corpus_replay.py CORPUS_DIR --write-baseline baseline.json
    python tools/corpus_replay.py CORPUS_DIR --baseline baseline.json

Runs ``akcli review analyze --profile deep --json`` on every ``*.kicad_sch``
under CORPUS_DIR and snapshots, per file, the fingerprint set and severity
histogram. With ``--baseline`` it compares against a stored snapshot and
reports drift (new/lost fingerprints, histogram shifts) — the calibration
gate for promoting a rule into a blocking ``--review-policy`` allowlist:
replay a corpus, measure the false-positive rate, THEN allowlist.

Not wheel-packaged, but CI-wired: ``tests/test_corpus_replay.py`` replays the
committed corpus against ``tests/golden/corpus_replay_baseline.json`` on every
run, so a rule change that shifts findings on the corpus leaves an auditable
baseline diff in the PR instead of an undocumented judgment call. Regenerate
deliberately with::

    python tools/corpus_replay.py tests/fixtures/corpus \
        --write-baseline tests/golden/corpus_replay_baseline.json
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))


def _analyze(fixture: Path) -> dict:
    # In-process (the golden-corpus tests' pattern): the CI replay pays one
    # interpreter start total instead of one per corpus file.
    from akcli.cli import main as _main  # lazy: after sys.path setup

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = _main(["review", "analyze", str(fixture),
                    "--profile", "deep", "--json"])
    if rc not in (0, 1):
        return {"error": stderr.getvalue().strip()[-500:]}
    doc = json.loads(stdout.getvalue())
    hist: dict[str, int] = {}
    for f in doc["findings"]:
        hist[f["severity"]] = hist.get(f["severity"], 0) + 1
    return {
        "fingerprints": sorted(f.get("fingerprint", "")
                               for f in doc["findings"]),
        "severity_hist": dict(sorted(hist.items())),
        "codes": sorted({f["code"] for f in doc["findings"]}),
    }


def _snapshot(corpus: Path) -> dict:
    files = sorted(corpus.rglob("*.kicad_sch"))
    return {str(f.relative_to(corpus)): _analyze(f) for f in files}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--write-baseline", type=Path)
    args = ap.parse_args()

    snap = _snapshot(args.corpus)
    print(f"corpus: {len(snap)} schematic(s)")
    if args.write_baseline:
        args.write_baseline.write_text(json.dumps(snap, indent=2) + "\n")
        print(f"baseline written: {args.write_baseline}")
        return 0
    if not args.baseline:
        for name, entry in snap.items():
            hist = entry.get("severity_hist", entry.get("error"))
            print(f"  {name}: {hist}")
        return 0

    base = json.loads(args.baseline.read_text())
    drift = 0
    for name in sorted(set(base) | set(snap)):
        b, n = base.get(name), snap.get(name)
        if b is None or n is None:
            print(f"  ± {name}: {'added to' if b is None else 'gone from'} "
                  "corpus")
            drift += 1
            continue
        new_fp = set(n.get("fingerprints", [])) - set(b.get("fingerprints", []))
        lost_fp = set(b.get("fingerprints", [])) - set(n.get("fingerprints", []))
        if new_fp or lost_fp:
            print(f"  ~ {name}: +{len(new_fp)} / -{len(lost_fp)} findings "
                  f"(hist {b.get('severity_hist')} -> "
                  f"{n.get('severity_hist')})")
            drift += 1
    print(f"drift: {drift} file(s)" if drift else "no drift vs baseline")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
