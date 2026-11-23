#!/usr/bin/env python3
"""Regenerate the EDIT tasks' starting schematics from their setup op-lists.

An edit task starts from an existing sheet, ``setup/board.kicad_sch``. That
file is never hand-written: it is ``akcli new`` + ``akcli draw --apply`` of the
task's ``setup/setup_ops.json``, so it is exactly what the writer produces
(unique uuids, instances blocks, embedded ``lib_symbols``) and stays clean
under ``akcli check --integrity``. The only normalization is the root-sheet
uuid, which ``akcli new`` mints at random: it is pinned per task (a UUIDv5 of
the task name) so every derived node uuid — and therefore the committed bytes
— is reproducible.

    python tools/agent_eval/build_setups.py           # rewrite the sheets
    python tools/agent_eval/build_setups.py --check   # drift gate (exit 1)

``tests/test_agent_eval.py`` runs the same comparison, so a writer change that
alters the bytes fails there first — regenerate, review the diff, commit.
"""

from __future__ import annotations

import argparse
import re
import tempfile
import uuid
from pathlib import Path

import run_eval

SETUP_OPS = "setup_ops.json"
_ROOT_UUID_RX = re.compile(r'\(uuid "[0-9a-f-]+"\)')


def setup_tasks() -> list[Path]:
    """Every task that ships a generated starting schematic."""
    return [t for t in run_eval._tasks()
            if (t / "setup" / SETUP_OPS).is_file()]


def root_uuid(task_dir: Path) -> str:
    """The task's pinned root-sheet uuid (stable across machines and runs)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"akcli-agent-eval/{task_dir.name}"))


def build(task_dir: Path) -> bytes:
    """The bytes ``setup/board.kicad_sch`` must hold for ``task_dir``."""
    with tempfile.TemporaryDirectory(prefix="akcli-eval-setup-") as td:
        sheet = Path(td) / run_eval.BOARD
        new = run_eval._akcli(["new", str(sheet)])
        if new.returncode != 0:
            raise RuntimeError(f"akcli new failed: {new.stderr.strip()}")
        # .kicad_sch bytes matter: stay in binary so no platform newline
        # translation can ever reach the committed artifact.
        text, pinned = _ROOT_UUID_RX.subn(
            f'(uuid "{root_uuid(task_dir)}")',
            sheet.read_bytes().decode("utf-8"), count=1)
        if pinned != 1:
            raise RuntimeError("blank sheet carries no root (uuid ...)")
        sheet.write_bytes(text.encode("utf-8"))
        draw = run_eval._akcli([
            "draw", str(sheet), "--ops", str(task_dir / "setup" / SETUP_OPS),
            "--apply", "--strict-nets", "--no-erc", *run_eval.SYMBOL_ARGS])
        if draw.returncode != 0:
            raise RuntimeError(
                f"{task_dir.name}: setup op-list refused (exit "
                f"{draw.returncode}): {(draw.stderr or draw.stdout).strip()}")
        return sheet.read_bytes()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="verify the committed sheets instead of rewriting")
    args = ap.parse_args()

    stale = 0
    for task in setup_tasks():
        target = task / "setup" / run_eval.BOARD
        want = build(task)
        have = target.read_bytes() if target.is_file() else None
        if have == want:
            print(f"ok     {task.name}")
        elif args.check:
            print(f"STALE  {task.name}  (run tools/agent_eval/build_setups.py)")
            stale += 1
        else:
            target.write_bytes(want)
            print(f"wrote  {task.name}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
