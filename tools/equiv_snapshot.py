#!/usr/bin/env python3
"""Behavioral fingerprint differ — mechanical proof that a refactor batch
changed nothing observable.

Runs a fixed corpus (every schematic fixture x a read-only command matrix,
plus every known op-list applied twice) against ONE ``src`` tree in-process,
and writes a JSON map of SHA-256 fingerprints.  Two runs — one from a clean
base worktree, one from the working tree — are then compared:

    git worktree add ../akcli-base <base-ref>
    python3 tools/equiv_snapshot.py --src ../akcli-base/src --out /tmp/base.json
    python3 tools/equiv_snapshot.py --src src               --out /tmp/head.json
    python3 tools/equiv_snapshot.py --compare /tmp/base.json /tmp/head.json

An empty diff is the acceptance bar for every pure-move batch of the 0.21
rework (the 0.20 release did this by hand against 0.19 for 8 op-lists; this
productizes that check and widens the corpus).

Design constraints:

* The tool always executes from HEAD (the base worktree does not have it);
  ``--src`` selects which ``akcli`` gets imported.  One src per process.
* Fingerprints hash the in-memory text ``.encode("utf-8")`` — nothing is
  round-tripped through the filesystem, so Windows newline translation can
  not manufacture fake diffs.
* Output is normalized first: the per-case temp dir, the repo root, run ids,
  backup labels and timestamps are replaced by fixed tokens.
* ``--self-check`` runs the corpus twice in one process and requires
  identical fingerprints — guarding the harness itself against
  nondeterminism (a nondeterministic harness turns "empty diff" into a
  false guarantee).  ``--limit N`` bounds the corpus for the cheap CI probe.

The baseline JSON is intentionally NOT committed: freezing outputs is the
goldens' job; this tool's value is the cross-commit diff.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

_NORM_RULES = (
    (re.compile(r'"run_id"\s*:\s*"[^"]*"'), '"run_id": "<RUN>"'),
    (re.compile(r'"backup"\s*:\s*"[^"]*"'), '"backup": "<BAK>"'),
    (re.compile(r'"timestamp"\s*:\s*"[^"]*"'), '"timestamp": "<TS>"'),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[0-9.:+Z-]*"), "<TS>"),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str, tmp: Path) -> str:
    for token, repl in ((str(tmp), "<TMP>"), (tmp.as_posix(), "<TMP>"),
                        (str(ROOT), "<ROOT>"), (ROOT.as_posix(), "<ROOT>")):
        text = text.replace(token, repl)
    for rx, repl in _NORM_RULES:
        text = rx.sub(repl, text)
    return text


def _run(main, argv: list[str], tmp: Path) -> dict:
    out, err = io.StringIO(), io.StringIO()
    rc: int | None = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = int(main(argv) or 0)
        except SystemExit as e:                      # argparse error paths
            rc = int(e.code or 0) if not isinstance(e.code, str) else 2
        except Exception as e:                        # harness must not die
            rc = -1
            print(f"<CRASH {type(e).__name__}>", file=err)
    return {
        "rc": rc,
        "stdout_sha256": _sha(_norm(out.getvalue(), tmp)),
        "stdout_len": len(out.getvalue()),
        "stderr_sha256": _sha(_norm(err.getvalue(), tmp)),
    }


def _readonly_cases() -> list[tuple[str, Path, list[str]]]:
    cases: list[tuple[str, Path, list[str]]] = []
    schs = sorted(p for p in FIXTURES.rglob("*.kicad_sch"))
    docs = sorted(p for p in FIXTURES.rglob("*.SchDoc"))
    for p in schs + docs:
        rel = p.relative_to(ROOT).as_posix()
        cases.append((f"read:{rel}", p, ["read", str(p), "--json"]))
        cases.append((f"nets:{rel}", p, ["nets", str(p), "--json"]))
        cases.append((f"check:{rel}", p,
                      ["check", str(p), "--json", "--fail-on", "never"]))
    for p in sorted((FIXTURES / "corpus").glob("*.kicad_sch")):
        rel = p.relative_to(ROOT).as_posix()
        cases.append((f"review:{rel}", p,
                      ["review", "analyze", str(p), "--json",
                       "--profile", "deep", "--fail-on", "never"]))
    return cases


def _oplist_cases() -> list[Path]:
    pats = [ROOT / "examples" / "circuits", FIXTURES / "corpus",
            ROOT / "tools" / "agent_eval" / "tasks"]
    out: list[Path] = []
    for base in pats:
        if base.is_dir():
            out += sorted(p for p in base.rglob("*.json")
                          if p.name in ("ops.json", "reference_ops.json")
                          or p.name.endswith(".ops.json"))
    return out


def snapshot(src: Path, limit: int | None = None) -> dict:
    sys.path.insert(0, str(src.resolve()))
    for mod in [m for m in sys.modules if m == "akcli"
                or m.startswith("akcli.")]:
        del sys.modules[mod]
    from akcli.cli import main                       # noqa: PLC0415

    results: dict[str, dict] = {}
    ro = _readonly_cases()
    ops = _oplist_cases()
    if limit is not None:
        ro, ops = ro[:limit], ops[: max(1, limit // 4)]

    with tempfile.TemporaryDirectory(prefix="akcli-equiv-") as td:
        tmp = Path(td)
        for key, _path, argv in ro:
            results[key] = _run(main, argv, tmp)
        for oplist in ops:
            rel = oplist.relative_to(ROOT).as_posix()
            case_dir = tmp / re.sub(r"[^A-Za-z0-9_.-]", "_", rel)
            case_dir.mkdir(parents=True)
            target = case_dir / "board.kicad_sch"
            entry: dict = {"new": _run(main, ["new", str(target)], tmp)}
            if target.is_file():
                # `new` mints a random root uuid; every node uuid is a v5
                # cascade from it.  Pin it so fingerprints compare across runs.
                text = re.sub(r'\(uuid "[0-9a-f-]+"\)',
                              '(uuid "00000000-1111-4222-8333-444444444444")',
                              target.read_text(encoding="utf-8"), count=1)
                with open(target, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            argv = ["draw", str(target), "--ops", str(oplist), "--apply",
                    "--json", "--flat"]
            entry["apply1"] = _run(main, argv, tmp)
            entry["sch_sha256_1"] = (
                _sha(target.read_text(encoding="utf-8"))
                if target.is_file() else None)
            entry["apply2"] = _run(main, argv, tmp)
            entry["sch_sha256_2"] = (
                _sha(target.read_text(encoding="utf-8"))
                if target.is_file() else None)
            results[f"draw:{rel}"] = entry
            shutil.rmtree(case_dir, ignore_errors=True)
    return {"cases": results, "count": len(results)}


def compare(a: Path, b: Path) -> int:
    da = json.loads(a.read_text(encoding="utf-8"))["cases"]
    db = json.loads(b.read_text(encoding="utf-8"))["cases"]
    bad = 0
    for key in sorted(set(da) | set(db)):
        if key not in da or key not in db:
            print(f"ONLY-IN-{'B' if key not in da else 'A'}: {key}")
            bad += 1
        elif da[key] != db[key]:
            print(f"DIFF: {key}")
            for f in sorted(set(da[key]) | set(db[key])):
                if da[key].get(f) != db[key].get(f):
                    print(f"  {f}: {da[key].get(f)!r} != {db[key].get(f)!r}")
            bad += 1
    print(f"{'FAIL' if bad else 'OK'}: {bad} differing case(s) "
          f"of {len(set(da) | set(db))}")
    return 1 if bad else 0


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, help="src dir whose akcli to load")
    ap.add_argument("--out", type=Path, help="write fingerprint JSON here")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"))
    ap.add_argument("--self-check", action="store_true",
                    help="run twice in-process; fingerprints must match")
    ap.add_argument("--limit", type=int, default=None,
                    help="bound the corpus (cheap CI probe)")
    args = ap.parse_args(argv)

    if args.compare:
        return compare(*args.compare)
    src = args.src or (ROOT / "src")
    if args.self_check:
        one = snapshot(src, limit=args.limit)
        two = snapshot(src, limit=args.limit)
        if one != two:
            print("SELF-CHECK FAIL: harness output is nondeterministic")
            return 1
        print(f"SELF-CHECK OK: {one['count']} cases, deterministic")
        return 0
    snap = snapshot(src, limit=args.limit)
    text = json.dumps(snap, indent=2, sort_keys=True) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {snap['count']} cases -> {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
