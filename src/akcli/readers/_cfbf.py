"""Hardened OLE2 / CFBF (Compound File Binary Format) reader (SPEC §3.2).

``.SchDoc`` / ``.SchLib`` / ``.PcbDoc`` are OLE2 compound files; the schematic
payload lives in named streams (``FileHeader`` for a SchDoc, per-symbol ``Data``
streams for a SchLib, etc.).

This is a hardened, from-scratch reimplementation of the OLE2/CFBF container
logic, with every loop/allocation bounded against hostile input:

* ``len >= 512`` header guard, magic check;
* ``sector_shift`` asserted in ``{9, 12}`` and ``mini_sector_shift == 6``,
  ``mini_cutoff == 4096``;
* every FAT / miniFAT chain walk is cycle-detected (seen-set) and capped at
  :data:`safety.MAX_SECTORS` -> ``ALTIUM_FAT_CYCLE`` / ``ALTIUM_ALLOC_GUARD``;
* every sector byte-offset is range-checked -> ``ALTIUM_OOB_SECTOR``;
* DIFAT spillover (> 109 FAT sectors) is walked with the same cycle/length
  guards as every other chain (declared ``n_difat`` is the hard bound, itself
  capped by :data:`safety.MAX_SECTORS`) -> ``ALTIUM_FAT_CYCLE`` /
  ``ALTIUM_ALLOC_GUARD`` on hostile input;
* the red-black directory tree (Child/Left/Right) is walked so storage names are
  **path-qualified** (``"Components6/Data"``) and multi-storage containers do not
  collapse to a single bare-name survivor.

Public API:

* :func:`read_streams` -> ``{bare_name: bytes}`` (root-level streams; later wins
  on a bare-name collision -- used by the single-``FileHeader`` SchDoc reader);
* :func:`read_streams_qualified` -> ``{"Storage/Data": bytes}`` (collision-free;
  used by multi-storage SchLib/PcbDoc readers).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from .. import safety
from ..errors import AkcliError, fail

# --- CFBF magic + sentinel sector values ------------------------------------
OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
NOSTREAM = 0xFFFFFFFF
DIFAT_HEADER_SLOTS = 109
DIR_ENTRY_SIZE = 128

_VALID_SECTOR_SHIFTS = frozenset({9, 12})
_MINI_SECTOR_SHIFT = 6
_MINI_CUTOFF = 4096


def _load(path_or_bytes: os.PathLike | str | bytes | bytearray) -> bytes:
    """Read ``path_or_bytes`` into a bounded ``bytes`` buffer."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
        if len(data) > safety.MAX_FILE_BYTES:
            fail("ALTIUM_ALLOC_GUARD", f"input {len(data)} bytes exceeds cap")
        return data
    p = Path(path_or_bytes)
    try:
        size = p.stat().st_size
    except OSError:
        # Let the open() below raise the precise FileNotFoundError/PermissionError.
        size = 0
    if size > safety.MAX_FILE_BYTES:
        fail("ALTIUM_ALLOC_GUARD", f"file {size} bytes exceeds cap")
    with open(p, "rb") as fh:
        data = fh.read(safety.MAX_FILE_BYTES + 1)
    if len(data) > safety.MAX_FILE_BYTES:
        fail("ALTIUM_ALLOC_GUARD", f"file exceeds {safety.MAX_FILE_BYTES} bytes")
    return data


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


class _Container:
    """Parsed CFBF container: header + FAT + miniFAT + directory entries."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        if len(data) < 512:
            fail("ALTIUM_MALFORMED", f"file too short ({len(data)} bytes < 512-byte header)")
        if data[:8] != OLE_MAGIC:
            fail("ALTIUM_BAD_MAGIC", "not an OLE2/CFBF file (bad magic)")

        ssz_shift = _u16(data, 30)
        msz_shift = _u16(data, 32)
        if ssz_shift not in _VALID_SECTOR_SHIFTS:
            fail("ALTIUM_BAD_SECTOR_SHIFT", f"sector_shift {ssz_shift} not in {{9, 12}}")
        if msz_shift != _MINI_SECTOR_SHIFT:
            fail("ALTIUM_BAD_SECTOR_SHIFT", f"mini_sector_shift {msz_shift} != 6")

        self.ssz = 1 << ssz_shift
        self.msz = 1 << msz_shift
        self.per = self.ssz // 4
        self.first_dir = _u32(data, 48)
        self.mini_cutoff = _u32(data, 56)
        if self.mini_cutoff != _MINI_CUTOFF:
            fail("ALTIUM_MALFORMED", f"mini_cutoff {self.mini_cutoff} != 4096")
        self.first_minifat = _u32(data, 60)
        self.n_minifat = _u32(data, 64)
        self.first_difat = _u32(data, 68)
        self.n_difat = _u32(data, 72)

        # DIFAT spillover (> 109 FAT sectors; large PcbDocs routinely need it)
        # is walked in _build_fat() under the declared-count + cycle guards.
        # Reject only an inconsistent header: a chain start with a zero count
        # (or vice versa) is malformed, and a count that cannot fit in the file
        # is an allocation bomb.
        if (self.first_difat < ENDOFCHAIN) != (self.n_difat != 0):
            fail("ALTIUM_MALFORMED",
                 f"inconsistent DIFAT header (first={self.first_difat:#x}, "
                 f"count={self.n_difat})")
        if self.n_difat > safety.MAX_SECTORS:
            fail("ALTIUM_ALLOC_GUARD", f"DIFAT count {self.n_difat} exceeds sector cap")

        self._build_fat()
        self._build_minifat()
        self._read_directory()

    # -- sector addressing ----------------------------------------------------
    def _off(self, s: int) -> int:
        """Byte offset of sector ``s``; range-checked against the file length."""
        b = (s + 1) * self.ssz
        if b < self.ssz or b + self.ssz > len(self.data):
            fail("ALTIUM_OOB_SECTOR", f"sector {s} (byte {b}) out of bounds")
        return b

    def _sector(self, s: int) -> bytes:
        b = self._off(s)
        return self.data[b:b + self.ssz]

    def _chain(self, start: int) -> list[int]:
        """Walk a FAT chain from ``start`` with cycle + length guards."""
        out: list[int] = []
        seen: set[int] = set()
        s = start
        while s < ENDOFCHAIN and s < len(self.fat):
            if s in seen:
                fail("ALTIUM_FAT_CYCLE", f"FAT chain cycle at sector {s}")
            if len(out) >= safety.MAX_SECTORS:
                fail("ALTIUM_ALLOC_GUARD", "FAT chain exceeds sector cap")
            seen.add(s)
            out.append(s)
            s = self.fat[s]
        return out

    def _read_chain(self, start: int, size: int) -> bytes:
        b = b"".join(self._sector(s) for s in self._chain(start))
        return b[:size] if size else b

    # -- FAT / miniFAT --------------------------------------------------------
    def _build_fat(self) -> None:
        difat = list(struct.unpack_from("<109I", self.data, 76))

        # DIFAT spillover chain: each DIFAT sector holds (per - 1) FAT sector
        # pointers, and its last u32 is the next DIFAT sector. Bounded by the
        # header-declared count, a cycle set, and the global sector cap.
        s = self.first_difat
        seen: set[int] = set()
        walked = 0
        while s < ENDOFCHAIN:
            if s in seen:
                fail("ALTIUM_FAT_CYCLE", f"DIFAT chain cycle at sector {s}")
            if walked >= self.n_difat:
                fail("ALTIUM_MALFORMED",
                     f"DIFAT chain longer than declared count {self.n_difat}")
            seen.add(s)
            walked += 1
            entries = struct.unpack_from(f"<{self.per}I", self.data, self._off(s))
            difat += entries[:-1]
            s = entries[-1]
        if walked < self.n_difat:
            fail("ALTIUM_MALFORMED",
                 f"DIFAT chain ended after {walked} sectors (declared {self.n_difat})")

        fat: list[int] = []
        for fs in difat:
            if fs >= ENDOFCHAIN:
                continue
            if len(fat) > safety.MAX_SECTORS:
                fail("ALTIUM_ALLOC_GUARD", "FAT exceeds sector cap")
            fat += list(struct.unpack_from(f"<{self.per}I", self.data, self._off(fs)))
        self.fat = fat

    def _build_minifat(self) -> None:
        minifat: list[int] = []
        seen: set[int] = set()
        mf = self.first_minifat
        while mf < ENDOFCHAIN and mf < len(self.fat):
            if mf in seen:
                fail("ALTIUM_FAT_CYCLE", f"miniFAT chain cycle at sector {mf}")
            if len(minifat) > safety.MAX_SECTORS:
                fail("ALTIUM_ALLOC_GUARD", "miniFAT exceeds sector cap")
            seen.add(mf)
            minifat += list(struct.unpack_from(f"<{self.per}I", self.data, self._off(mf)))
            mf = self.fat[mf]
        self.minifat = minifat

    # -- directory ------------------------------------------------------------
    def _read_directory(self) -> None:
        dirbuf = self._read_chain(self.first_dir, 0)
        entries: list[dict] = []
        for i in range(0, len(dirbuf), DIR_ENTRY_SIZE):
            e = dirbuf[i:i + DIR_ENTRY_SIZE]
            if len(e) < DIR_ENTRY_SIZE:
                break
            if len(entries) > safety.MAX_DIR_ENTRIES:
                fail("ALTIUM_ALLOC_GUARD", "directory exceeds entry cap")
            nlen = _u16(e, 64)
            name = e[:nlen - 2].decode("utf-16-le", "replace") if nlen >= 2 else ""
            entries.append({
                "name": name,
                "etype": e[66],
                "left": _u32(e, 68),
                "right": _u32(e, 72),
                "child": _u32(e, 76),
                "start": _u32(e, 116),
                "size": struct.unpack_from("<Q", e, 120)[0],
            })
        self.entries = entries

        roots = [e for e in entries if e["etype"] == 5]
        if not roots:
            fail("ALTIUM_MALFORMED", "missing root storage entry")
        self.root = roots[0]

        if self.root["start"] < ENDOFCHAIN:
            self.ministream = self._read_chain(self.root["start"], self.root["size"])
        else:
            self.ministream = b""

    # -- mini-stream reads ----------------------------------------------------
    def _read_mini(self, start: int, size: int) -> bytes:
        out = bytearray()
        seen: set[int] = set()
        s = start
        while s < ENDOFCHAIN and s < len(self.minifat):
            if s in seen:
                fail("ALTIUM_FAT_CYCLE", f"mini-stream chain cycle at sector {s}")
            if len(out) > safety.MAX_DECODED_BYTES:
                fail("ALTIUM_ALLOC_GUARD", "mini stream exceeds decode cap")
            seen.add(s)
            out += self.ministream[s * self.msz:s * self.msz + self.msz]
            s = self.minifat[s]
        return bytes(out[:size]) if size else bytes(out)

    def _content(self, e: dict) -> bytes:
        if e["size"] < self.mini_cutoff:
            data = self._read_mini(e["start"], e["size"])
        else:
            data = self._read_chain(e["start"], e["size"])
        return data[:e["size"]]

    # -- directory-tree walk --------------------------------------------------
    def streams(self, qualified: bool) -> dict[str, bytes]:
        """Collect every stream's bytes, keyed bare or path-qualified."""
        result: dict[str, bytes] = {}
        total = 0
        seen: set[int] = set()
        # stack of (entry_index, prefix); the red-black sibling tree's Child
        # pointer descends one storage level (extending the prefix), Left/Right
        # are siblings at the same level.
        stack: list[tuple[int, str]] = [(self.root["child"], "")]
        while stack:
            idx, prefix = stack.pop()
            if idx == NOSTREAM or idx >= len(self.entries):
                continue
            if idx in seen:
                fail("ALTIUM_MALFORMED", f"directory tree cycle at entry {idx}")
            if len(seen) > safety.MAX_DIR_ENTRIES:
                fail("ALTIUM_ALLOC_GUARD", "directory walk exceeds entry cap")
            seen.add(idx)
            e = self.entries[idx]
            stack.append((e["left"], prefix))
            stack.append((e["right"], prefix))
            if e["etype"] == 1:                       # storage
                stack.append((e["child"], prefix + e["name"] + "/"))
            elif e["etype"] == 2:                     # stream
                content = self._content(e)
                total += len(content)
                if total > safety.MAX_DECODED_BYTES:
                    fail("ALTIUM_ALLOC_GUARD", "decoded streams exceed cap")
                key = (prefix + e["name"]) if qualified else e["name"]
                result[key] = content
        return result


def read_streams(path_or_bytes: os.PathLike | str | bytes | bytearray) -> dict[str, bytes]:
    """Return ``{bare_name: bytes}`` for every stream in the container.

    Bare-name keyed (last path component); on a bare-name collision the last
    stream visited wins. Adequate for single-``FileHeader`` SchDocs; use
    :func:`read_streams_qualified` for multi-storage SchLib/PcbDoc containers.
    """
    return _Container(_load(path_or_bytes)).streams(qualified=False)


def read_streams_qualified(
    path_or_bytes: os.PathLike | str | bytes | bytearray,
) -> dict[str, bytes]:
    """Return ``{"Storage/Data": bytes}`` -- path-qualified, collision-free."""
    return _Container(_load(path_or_bytes)).streams(qualified=True)


__all__ = ["read_streams", "read_streams_qualified", "AkcliError"]
