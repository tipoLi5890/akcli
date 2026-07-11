"""Seeded fuzz of the OLE2/CFBF + Altium record readers (stdlib only).

Contract: for ANY bytes, ``altium_sch.read`` either returns a Schematic or
raises a structured :class:`AkcliError` (``ALTIUM_*`` codes) — never a raw
struct/index/key error, never a hang, never unbounded allocation. Mutations
target the classic container killers: header damage (magic, sector shift),
FAT surgery (cycles, out-of-bounds chains), truncation, and byte noise deep
in the directory/record streams.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from altium_kicad_cli.errors import AkcliError
from altium_kicad_cli.readers import altium_sch

FIXTURE = (Path(__file__).parent / "fixtures" / "t_junction.SchDoc").read_bytes()


def _assert_survives(data: bytes, note: str) -> None:
    try:
        sch = altium_sch.read(data)
    except AkcliError:
        return                        # structured rejection is a pass
    assert sch is not None, note


def _mutate(rng: random.Random, data: bytes) -> bytes:
    buf = bytearray(data)
    kind = rng.randrange(6)
    if kind == 0:                     # truncate anywhere (header included)
        return bytes(buf[:rng.randrange(len(buf))])
    if kind == 1:                     # header field damage (first 512 bytes)
        for _ in range(rng.randrange(1, 8)):
            buf[rng.randrange(min(512, len(buf)))] = rng.randrange(256)
        return bytes(buf)
    if kind == 2:                     # FAT/DIFAT surgery: 4-byte sector refs
        for _ in range(rng.randrange(1, 12)):
            off = rng.randrange(0, len(buf) - 4)
            buf[off:off + 4] = rng.choice(
                (b"\xff\xff\xff\xff", b"\xfe\xff\xff\xff",
                 b"\x00\x00\x00\x00", rng.randrange(2**32).to_bytes(4, "little"))
            )
        return bytes(buf)
    if kind == 3:                     # random byte noise anywhere
        for _ in range(rng.randrange(1, 40)):
            buf[rng.randrange(len(buf))] = rng.randrange(256)
        return bytes(buf)
    if kind == 4:                     # duplicate a slice (grow / self-reference)
        i = rng.randrange(len(buf))
        j = min(len(buf), i + rng.randrange(1, 4096))
        return bytes(buf[:j] + buf[i:j] + buf[j:])
    return bytes(buf[::-1])           # wholesale reversal


@pytest.mark.parametrize("seed", range(8))
def test_fuzz_cfbf_mutations_never_crash(seed):
    rng = random.Random(seed)
    for i in range(30):
        _assert_survives(_mutate(rng, FIXTURE), f"seed={seed} iter={i}")


def test_fuzz_cfbf_garbage_and_hostile_shapes():
    rng = random.Random(99)
    magic = FIXTURE[:8]
    for i, data in enumerate([
        b"", b"\x00" * 512, magic,                       # trivia
        magic + b"\x00" * 504,                           # header, no body
        magic + b"\xff" * 4096,                          # all-FF FAT world
        FIXTURE[:512] + FIXTURE[512:][::-1],             # body reversed
    ]):
        _assert_survives(data, f"hostile {i}")
    for i in range(40):
        n = rng.randrange(0, 8192)
        _assert_survives(bytes(rng.randrange(256) for _ in range(n)),
                         f"garbage {i}")
