"""ngspice ``wrdata`` -> tidy CSV: the offline text layer of ``akcli sim`` waves.

ngspice's ``wrdata <file> v(a) v(b) ...`` writes a *headerless*, whitespace-
separated table in which **the scale column (time, or frequency for AC) is
repeated before every vector**.  A two-vector transient therefore looks like::

    0.0000e+00  0.0000e+00  0.0000e+00  1.0000e+00
    1.0000e-06  4.8000e-03  1.0000e-06  1.0000e+00
    ...
    ^time       ^v(out)     ^time(dup)  ^v(in)

:func:`rewrite_wrdata` collapses that into a single ``time`` column followed by
one named column per vector — a clean CSV any spreadsheet or plotting tool can
read.  It is a pure text transform (no ngspice, no numeric reformatting: the
value tokens are copied verbatim so no precision is lost), so it is fully
offline-testable against a captured ``wrdata`` sample.

The repeated scale column means each row carries ``stride * len(vectors)``
fields, where ``stride`` is 2 for real analyses (``scale value``) and 3 for a
complex AC analysis (``scale real imag``); the first value field of each group
(``value``/``real``) is taken as that vector's column and the very first field
of the row is taken as the shared scale.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..errors import fail


def rewrite_wrdata(src: Path, dst: Path, vectors: list[str],
                   scale: str = "time") -> None:
    """Rewrite an ngspice ``wrdata`` file ``src`` to a tidy CSV at ``dst``.

    ``vectors`` names the columns in the exact order they were passed to
    ``wrdata`` (e.g. ``["v(out)", "v(in)"]``); the output header is
    ``<scale>,<vectors...>``.  ``scale`` names the shared first column —
    ``"time"`` for transient/op runs, ``"frequency"`` for an AC analysis (whose
    scale is frequency, per the module docstring).  Blank lines are ignored.
    Raises ``AkcliError('BAD_CONFIG', ...)`` when ``vectors`` is empty, a data
    row's field count is not an exact multiple of ``len(vectors)``, or the
    inferred stride is < 2 (each vector needs at least a scale+value pair — a
    stride of 1 means the row carries fewer real vectors than ``vectors`` names)
    — so a mismatched vector list is a loud error rather than a silently
    truncated or crashing CSV.
    """
    if not vectors:
        fail("BAD_CONFIG", "rewrite_wrdata: 'vectors' must be non-empty")

    src = Path(src)
    rows: list[list[str]] = []
    stride: int | None = None
    for lineno, raw in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        if stride is None:
            if len(fields) % len(vectors) != 0:
                fail("BAD_CONFIG",
                     f"rewrite_wrdata: {src} line {lineno} has {len(fields)} "
                     f"field(s), not a multiple of the {len(vectors)} vector(s) "
                     f"{vectors} — wrong vector list?")
            stride = len(fields) // len(vectors)
            if stride < 2:
                # wrdata repeats the scale before *every* vector, so a real
                # layout is always stride>=2 ('scale value', or 'scale real imag'
                # for complex AC). stride==1 means fewer vectors were actually
                # written than 'vectors' names (e.g. a misspelled vector dropped
                # by ngspice); indexing i*stride+1 would then run off the row.
                fail("BAD_CONFIG",
                     f"rewrite_wrdata: {src} line {lineno} has {len(fields)} "
                     f"field(s) for {len(vectors)} vector(s) — inferred stride "
                     f"{stride} (< 2); each vector needs a scale+value pair. "
                     f"Wrong vector list {vectors}?")
        elif len(fields) != stride * len(vectors):
            fail("BAD_CONFIG",
                 f"rewrite_wrdata: {src} line {lineno} has {len(fields)} "
                 f"field(s), expected {stride * len(vectors)} (ragged wrdata)")
        # shared scale value = first field; each vector's value = first field of
        # its stride-wide group (index i*stride + 1 in a 'scale value ...' group).
        scale_val = fields[0]
        values = [fields[i * stride + 1] for i in range(len(vectors))]
        rows.append([scale_val, *values])

    dst = Path(dst)
    with dst.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([scale, *vectors])
        writer.writerows(rows)
