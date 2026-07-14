"""Tests for the isolated libngspice runner (``sim.engine``).

Discovery and argv validation run everywhere; the end-to-end ``run()`` cases are
gated on an actually-loadable libngspice (present in the KiCad bundle on the dev
machine) and are real simulations, not mocks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from akcli.sim import engine

_HAVE_NGSPICE = engine.available() is not None
_needs_engine = pytest.mark.skipif(
    not _HAVE_NGSPICE, reason="libngspice not available on this machine"
)


# --- discovery --------------------------------------------------------------


def test_available_off_returns_none(monkeypatch):
    for tok in ("0", "off", "OFF", "none", "false", ""):
        monkeypatch.setenv("AKCLI_NGSPICE", tok)
        assert engine.available() is None


def test_available_bogus_override_returns_none(monkeypatch):
    monkeypatch.setenv("AKCLI_NGSPICE", "/no/such/path/libngspice.999.dylib")
    assert engine.available() is None


@_needs_engine
def test_available_path_override_is_honored(monkeypatch):
    monkeypatch.delenv("AKCLI_NGSPICE", raising=False)
    real = engine.available()
    assert real is not None
    monkeypatch.setenv("AKCLI_NGSPICE", real)
    assert engine.available() == real


# --- Windows install version sort (item 9) ----------------------------------


def test_kicad_version_key_sorts_numerically():
    paths = [
        "C:/Program Files/KiCad/9.0/bin/libngspice.dll",
        "C:/Program Files/KiCad/10.0/bin/libngspice.dll",
        "C:/Program Files/KiCad/8.0/bin/libngspice.dll",
    ]
    newest = sorted(paths, key=engine._kicad_version_key, reverse=True)[0]
    assert "/10.0/" in newest  # 10.0 beats 9.0 (a string sort would pick 9.0)


def test_kicad_version_key_unparseable_sorts_lowest():
    assert engine._kicad_version_key("C:/no/version/here.dll") == (-1,)
    assert engine._kicad_version_key(
        "C:/Program Files/KiCad/10.0/bin/x.dll"
    ) == (10, 0)


# --- fatal-line classification (item 4) -------------------------------------


def test_is_fatal_line_flags_parse_errors_but_not_meas_failed():
    assert engine._is_fatal_line("Error: circuit not parsed.")
    assert engine._is_fatal_line("fatal error during analysis")
    # a WHEN/edge measure that never crossed is assertion-level, NOT fatal
    assert not engine._is_fatal_line(
        "stdout meas tran nope WHEN v(a)=99 RISE=1 failed!"
    )
    assert not engine._is_fatal_line("stdout vpeak = 4.9e-01 at= 5.6e-02")


# --- PYTHONPATH propagation to the child (relative-entry fix) ---------------


def test_run_absolutizes_relative_pythonpath_entries(tmp_path, monkeypatch):
    # The child runs with cwd=workdir (a tempdir); a relative sys.path entry
    # (e.g. 'src' from `PYTHONPATH=src python -m ...`) would resolve against the
    # tempdir there and the import would fail. run() must absolutize every entry.
    import os
    import subprocess

    captured: dict = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kw):
        captured["env"] = kw.get("env", {})
        return _Done()

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(engine.sys, "path", ["relsrc", "also/rel", "/already/abs"])

    engine.run("* deck\n.end\n", ["run"], timeout=5, workdir=tmp_path)

    entries = [p for p in captured["env"]["PYTHONPATH"].split(os.pathsep) if p]
    assert entries, "child PYTHONPATH must carry the parent import path"
    assert all(os.path.isabs(p) for p in entries), entries
    # the relative marker was made absolute, not forwarded verbatim
    assert "relsrc" not in entries
    assert os.path.abspath("relsrc") in entries


# --- child-mode argv validation ---------------------------------------------


def test_main_rejects_wrong_argc():
    assert engine.main([]) == 2
    assert engine.main(["only-one"]) == 2
    assert engine.main(["a", "b", "c"]) == 2


# --- end-to-end (live) ------------------------------------------------------

_DIVIDER = """\
* resistive divider operating point
V1 in 0 dc 3
R1 in out 10k
R2 out 0 1.1k
.op
.end
"""


def _find_value(lines, name):
    pat = re.compile(re.escape(name) + r"\s*=\s*([-+0-9.eE]+)")
    for ln in lines:
        m = pat.search(ln)
        if m:
            return float(m.group(1))
    return None


@_needs_engine
def test_run_divider_operating_point(tmp_path):
    res = engine.run(_DIVIDER, ["run", "print v(out)"], timeout=30, workdir=tmp_path)
    assert res.ok, res.log
    assert res.error is None
    vout = _find_value(res.meas_lines, "v(out)")
    assert vout is not None, res.log
    assert abs(vout - 0.29730) < 1e-3, f"v(out)={vout!r}\n{res.log}"


_WRDATA = """\
* transient with a wrdata dump
V1 in 0 dc 1
R1 in out 1k
C1 out 0 1u
.tran 1u 100u
.end
"""


@_needs_engine
def test_run_collects_wrdata_files(tmp_path):
    res = engine.run(
        _WRDATA, ["run", "wrdata wave.data v(out)"], timeout=30, workdir=tmp_path
    )
    assert res.ok, res.log
    assert any(p.endswith("wave.data") for p in res.wave_files), res.wave_files


_RUNAWAY = """\
* an infinite control loop the parent must kill on timeout
V1 in 0 dc 1
R1 in 0 1k
.control
while 1
end
.endc
.end
"""


@_needs_engine
def test_run_timeout(tmp_path):
    res = engine.run(_RUNAWAY, ["run"], timeout=1, workdir=tmp_path)
    assert res.ok is False
    assert res.error is not None
    assert "timeout" in res.error


_BAD = """\
* bad deck: diode references a model that was never defined
V1 a 0 dc 1
D1 a 0 NOSUCHMODEL
.op
.end
"""


@_needs_engine
def test_run_bad_deck(tmp_path):
    res = engine.run(_BAD, ["run"], timeout=30, workdir=tmp_path)
    assert (not res.ok) or (res.error is not None) or ("error" in res.log.lower())


# item 4: a deck that will not parse must yield ok=False with the error visible,
# never the old false-ok.
_UNPARSEABLE = """\
* nonsense element that ngspice cannot parse
Z1 this is not a valid device line
.op
.end
"""


@_needs_engine
def test_run_unparseable_deck_is_not_ok(tmp_path):
    res = engine.run(_UNPARSEABLE, ["run"], timeout=30, workdir=tmp_path)
    assert res.ok is False
    assert res.error
    assert "parsed" in res.error.lower() or "error" in res.error.lower()


# item 5: a relative workdir must be resolved before the child (cwd=workdir) is
# spawned, else it looks under workdir/workdir and dies.
@_needs_engine
def test_run_relative_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = engine.run(_DIVIDER, ["run", "print v(out)"],
                     timeout=30, workdir=Path("case2"))
    assert res.ok, res.log
    vout = _find_value(res.meas_lines, "v(out)")
    assert vout is not None and abs(vout - 0.29730) < 1e-3
