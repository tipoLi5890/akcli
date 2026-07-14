"""Tests for the wrdata -> tidy CSV transform (sim/wave.py).

The offline tests run a captured, checked-in ngspice ``wrdata`` sample through
:func:`rewrite_wrdata` (no engine needed).  One gated round-trip actually drives
libngspice, writes a real wrdata file and rewrites it, to prove the literal
sample matches the format the engine really emits.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from akcli.errors import AkcliError
from akcli.sim import wave

# A VERBATIM two-vector transient wrdata sample: ngspice repeats the time
# (scale) column before each vector, so every row is [t, v(out), t, v(in)].
_WRDATA_2VEC = """\
0.000000000000000e+00 0.000000000000000e+00 0.000000000000000e+00 1.000000000000000e+00
5.000000000000000e-06 2.300000000000000e-02 5.000000000000000e-06 1.000000000000000e+00
1.000000000000000e-05 4.500000000000000e-02 1.000000000000000e-05 1.000000000000000e+00
1.500000000000000e-05 6.600000000000000e-02 1.500000000000000e-05 1.000000000000000e+00
"""

# A single-vector sample (stride 2, one [t, v] pair per row).
_WRDATA_1VEC = """\
0.000000000000000e+00 0.000000000000000e+00
1.000000000000000e-06 4.800000000000000e-03
2.000000000000000e-06 9.500000000000000e-03
"""

# A complex AC sample: ngspice writes [freq, real, imag] per vector (stride 3).
_WRDATA_AC_COMPLEX = """\
1.000000000000000e+01 9.900000000000000e-01 -1.000000000000000e-02
1.000000000000000e+02 8.700000000000000e-01 -4.900000000000000e-01
1.000000000000000e+03 1.300000000000000e-01 -3.400000000000000e-01
"""


def _read_csv(path: Path) -> list[list[str]]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# --------------------------------------------------------------------------- #
# offline transform against the captured sample
# --------------------------------------------------------------------------- #
def test_two_vector_wrdata_becomes_single_time_plus_named_columns(tmp_path):
    src = tmp_path / "wave.data"
    dst = tmp_path / "wave.csv"
    src.write_text(_WRDATA_2VEC)
    wave.rewrite_wrdata(src, dst, ["v(out)", "v(in)"])
    rows = _read_csv(dst)
    assert rows[0] == ["time", "v(out)", "v(in)"]
    assert len(rows) == 5                       # header + 4 data rows
    assert rows[1] == ["0.000000000000000e+00", "0.000000000000000e+00",
                       "1.000000000000000e+00"]
    # value tokens are copied verbatim (no reformat / precision loss)
    assert rows[4][1] == "6.600000000000000e-02"
    # the duplicated time column is collapsed: no row has 4 fields
    assert all(len(r) == 3 for r in rows)


def test_single_vector_wrdata(tmp_path):
    src = tmp_path / "w.data"
    dst = tmp_path / "w.csv"
    src.write_text(_WRDATA_1VEC)
    wave.rewrite_wrdata(src, dst, ["v(out)"])
    rows = _read_csv(dst)
    assert rows[0] == ["time", "v(out)"]
    assert rows[1] == ["0.000000000000000e+00", "0.000000000000000e+00"]
    assert rows[2] == ["1.000000000000000e-06", "4.800000000000000e-03"]


def test_complex_ac_wrdata_takes_real_part(tmp_path):
    src = tmp_path / "ac.data"
    dst = tmp_path / "ac.csv"
    src.write_text(_WRDATA_AC_COMPLEX)
    wave.rewrite_wrdata(src, dst, ["vout"])
    rows = _read_csv(dst)
    assert rows[0] == ["time", "vout"]
    # scale (freq) from col0, value (real) from col1; imag dropped
    assert rows[1] == ["1.000000000000000e+01", "9.900000000000000e-01"]
    assert rows[3] == ["1.000000000000000e+03", "1.300000000000000e-01"]


def test_blank_lines_are_ignored(tmp_path):
    src = tmp_path / "w.data"
    dst = tmp_path / "w.csv"
    src.write_text("\n" + _WRDATA_1VEC + "\n\n")
    wave.rewrite_wrdata(src, dst, ["v(out)"])
    assert len(_read_csv(dst)) == 4             # header + 3 rows, blanks skipped


def test_empty_vectors_raises(tmp_path):
    src = tmp_path / "w.data"
    src.write_text(_WRDATA_1VEC)
    with pytest.raises(AkcliError) as ei:
        wave.rewrite_wrdata(src, tmp_path / "w.csv", [])
    assert ei.value.code == "BAD_CONFIG"


def test_field_count_mismatch_raises(tmp_path):
    # 3 fields per row cannot come from 2 vectors (stride would be 1.5).
    src = tmp_path / "w.data"
    src.write_text("1.0 2.0 3.0\n")
    with pytest.raises(AkcliError) as ei:
        wave.rewrite_wrdata(src, tmp_path / "w.csv", ["a", "b"])
    assert ei.value.code == "BAD_CONFIG"


def test_ragged_rows_raise(tmp_path):
    src = tmp_path / "w.data"
    src.write_text("1.0 2.0\n3.0 4.0 5.0 6.0\n")   # first row sets stride=2
    with pytest.raises(AkcliError) as ei:
        wave.rewrite_wrdata(src, tmp_path / "w.csv", ["a"])
    assert ei.value.code == "BAD_CONFIG"


def test_stride_one_is_rejected_not_indexerror(tmp_path):
    # 2 fields for 2 vectors divides evenly (2%2==0) but implies stride=1 — an
    # impossible layout (each vector needs scale+value). Must be a loud
    # BAD_CONFIG, not a raw IndexError off the end of the row.
    src = tmp_path / "w.data"
    src.write_text("1.0e-6 2.0e-3\n2.0e-6 4.0e-3\n")
    with pytest.raises(AkcliError) as ei:
        wave.rewrite_wrdata(src, tmp_path / "w.csv", ["v(a)", "v(b)"])
    assert ei.value.code == "BAD_CONFIG"
    assert "stride" in str(ei.value)


def test_ac_scale_column_labelled_frequency(tmp_path):
    # For an AC capture the scale is frequency, not time (module docstring).
    src = tmp_path / "ac.data"
    dst = tmp_path / "ac.csv"
    src.write_text(_WRDATA_AC_COMPLEX)
    wave.rewrite_wrdata(src, dst, ["vout"], scale="frequency")
    rows = _read_csv(dst)
    assert rows[0] == ["frequency", "vout"]
    assert rows[1][0] == "1.000000000000000e+01"


# --------------------------------------------------------------------------- #
# gated live round-trip: a real libngspice run writes a real wrdata file, which
# rewrite_wrdata then collapses — proving the literal sample above is faithful.
# --------------------------------------------------------------------------- #
_HAVE_NGSPICE = __import__(
    "akcli.sim.engine", fromlist=["available"]
).available() is not None


@pytest.mark.skipif(not _HAVE_NGSPICE, reason="libngspice not available")
def test_live_wrdata_round_trip(tmp_path):
    from akcli.sim import engine

    deck = "\n".join([
        "* wave round-trip",
        "V1 IN 0 dc 1 ac 1",
        "R1 IN OUT 10k",
        "C1 OUT 0 100n",
        ".end",
    ]) + "\n"
    out = tmp_path / "wave.data"
    res = engine.run(
        deck,
        ["tran 5u 100u", f"wrdata {out} v(out) v(in)"],
        timeout=30,
        workdir=tmp_path,
    )
    assert res.ok, res.error or res.log
    # locate the wrdata file (engine reports new files in wave_files)
    produced = [p for p in res.wave_files if Path(p).name.startswith("wave")]
    src = Path(produced[0]) if produced else out
    assert src.exists(), res.log
    dst = tmp_path / "wave.csv"
    wave.rewrite_wrdata(src, dst, ["v(out)", "v(in)"])
    rows = _read_csv(dst)
    assert rows[0] == ["time", "v(out)", "v(in)"]
    assert len(rows) > 2
    # v(in) is a DC 1V source -> every value column reads ~1
    assert all(abs(float(r[2]) - 1.0) < 1e-6 for r in rows[1:])
    # time is monotonically non-decreasing
    times = [float(r[0]) for r in rows[1:]]
    assert times == sorted(times)
