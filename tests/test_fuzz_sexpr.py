"""Seeded fuzz of the s-expression parser (zero extra dependencies).

Contract under test: for ANY input, ``sexpr.parse`` either returns an SNode
or raises a structured :class:`AkcliError` — never hangs, never exhausts
memory, never escapes with a raw ``RecursionError``/``MemoryError``. Seeds
are fixed so failures reproduce; mutations are the classic parser killers
(truncation, paren storms, quote damage, byte noise, deep nesting).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from akcli.errors import AkcliError
from akcli.readers import sexpr

FIXTURE = (Path(__file__).parent / "fixtures" / "kicad"
           / "board_v8.kicad_sch").read_text(encoding="utf-8")


def _assert_survives(text: str, note: str) -> None:
    try:
        node = sexpr.parse(text)
    except AkcliError:
        return                       # structured rejection is a pass
    assert node is not None, note


def _mutate(rng: random.Random, text: str) -> str:
    kind = rng.randrange(6)
    if kind == 0:                    # truncate anywhere
        return text[:rng.randrange(len(text))]
    if kind == 1:                    # drop a random slice
        i = rng.randrange(len(text)); j = min(len(text), i + rng.randrange(200))
        return text[:i] + text[j:]
    if kind == 2:                    # paren storm at a random point
        i = rng.randrange(len(text))
        return text[:i] + rng.choice("()") * rng.randrange(1, 64) + text[i:]
    if kind == 3:                    # quote damage
        i = rng.randrange(len(text))
        return text[:i] + '"' + text[i:]
    if kind == 4:                    # byte noise
        chars = list(text)
        for _ in range(rng.randrange(1, 24)):
            chars[rng.randrange(len(chars))] = chr(rng.randrange(1, 0x2FFF))
        return "".join(chars)
    return text[::-1]                # wholesale reversal


@pytest.mark.parametrize("seed", range(8))
def test_fuzz_mutations_never_crash(seed):
    rng = random.Random(seed)
    for i in range(40):
        _assert_survives(_mutate(rng, FIXTURE), f"seed={seed} iter={i}")


def test_fuzz_garbage_never_crashes():
    rng = random.Random(1234)
    for i in range(60):
        n = rng.randrange(0, 4000)
        garbage = "".join(chr(rng.randrange(1, 0x500)) for _ in range(n))
        _assert_survives(garbage, f"garbage iter={i}")


def test_fuzz_hostile_shapes():
    for text in (
        "(" * 10_000,                              # depth bomb
        "(a " + '"' + "x" * 3_000_000,             # unterminated huge atom
        "(a (b (c" + ")" * 5000,                   # unbalanced storm
        "", " ", "\x00", ")(",                     # trivia
        "(kicad_sch" + " (x 1)" * 100_000 + ")",   # node-count pressure
    ):
        _assert_survives(text, repr(text[:30]))
