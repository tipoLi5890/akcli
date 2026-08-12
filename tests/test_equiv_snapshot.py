"""Cheap determinism probe for the equivalence harness itself.

``tools/equiv_snapshot.py`` is the mechanical proof used by refactor batches
("empty diff vs base = no behavior change").  That proof is only as good as
the harness's own determinism: if the harness smuggles in nondeterminism
(unpinned uuids, unnormalized paths/timestamps), an empty diff becomes a
false guarantee.  This probe runs a small corpus twice in a subprocess and
requires identical fingerprints.  The full-corpus run stays a manual/dev
step — this is the canary, not the proof.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_self_check_small_corpus_is_deterministic():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "equiv_snapshot.py"),
         "--self-check", "--limit", "6"],
        capture_output=True, text=True, timeout=300,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        f"equiv_snapshot --self-check failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "SELF-CHECK OK" in proc.stdout
