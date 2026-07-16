"""CI wiring for the review-rule calibration harness (tools/corpus_replay.py).

Replays ``review analyze --profile deep`` over the committed corpus and
compares fingerprint sets + severity histograms against the committed
baseline. A drift is a real signal, not noise: either a detector changed
behavior (regenerate the baseline IN THE SAME PR, so the diff documents the
calibration decision) or a regression slipped in. This is the artifact trail
behind the ``release preflight --review-policy`` promotion path — a rule must
show a stable corpus record before it is allowlisted to block a release.

Regenerate deliberately::

    python tools/corpus_replay.py tests/fixtures/corpus \
        --write-baseline tests/golden/corpus_replay_baseline.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "corpus"
BASELINE = ROOT / "tests" / "golden" / "corpus_replay_baseline.json"

sys.path.insert(0, str(ROOT / "tools"))
import corpus_replay  # noqa: E402


def test_baseline_is_committed():
    assert BASELINE.is_file(), (
        "missing tests/golden/corpus_replay_baseline.json — generate it with "
        "tools/corpus_replay.py --write-baseline (see module docstring)"
    )


def test_corpus_matches_baseline():
    current = corpus_replay._snapshot(CORPUS)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(current) == set(baseline), (
        f"corpus file set drifted: only-current="
        f"{sorted(set(current) - set(baseline))} "
        f"only-baseline={sorted(set(baseline) - set(current))}"
    )
    for name in sorted(current):
        cur, base = current[name], baseline[name]
        assert "error" not in cur, f"{name}: review analyze failed: {cur}"
        assert cur == base, (
            f"{name}: review findings drifted from the committed baseline.\n"
            f"  baseline codes: {base.get('codes')}\n"
            f"  current  codes: {cur.get('codes')}\n"
            f"  baseline hist:  {base.get('severity_hist')}\n"
            f"  current  hist:  {cur.get('severity_hist')}\n"
            "If the change is intentional, regenerate the baseline in this "
            "same PR: python tools/corpus_replay.py tests/fixtures/corpus "
            "--write-baseline tests/golden/corpus_replay_baseline.json"
        )
