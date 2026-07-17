"""``plan/draw --render OUT.svg`` — the look-before-apply preview channel.

The op-list is dry-applied to the same temp copy the net diff uses and
rendered with the stdlib SVG renderer, so an agent can SEE the would-be sheet
before ``--apply``. Preview failures are advisory by contract (stderr warning,
never a changed exit code), and the target is never touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from akcli import cli

DEVICE = Path(__file__).parent / "fixtures" / "kicad" / "symbols" / "Device.kicad_sym"


def _blank_sch(tmp_path: Path) -> Path:
    tgt = tmp_path / "board.kicad_sch"
    tgt.write_text('(kicad_sch (version 20231120) (generator "akcli") '
                   '(uuid "33333333-4444-5555-6666-777777777777") (paper "A4"))\n')
    return tgt


def _opsfile(tmp_path: Path, *ops_list, **envelope) -> Path:
    doc = {"protocol_version": 1, "target_format": "kicad",
           "ops": list(ops_list), **envelope}
    f = tmp_path / "ops.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return f


_R1 = {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
       "x_mil": 1000, "y_mil": 1000, "value": "1k"}
_LABELS = ({"op": "add_net_label", "name": "A", "at": "R1.1"},
           {"op": "add_net_label", "name": "B", "at": "R1.2"})


def test_plan_render_writes_preview_and_never_touches_target(tmp_path, capsys):
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    out = tmp_path / "preview.svg"
    sha_before = hashlib.sha256(tgt.read_bytes()).hexdigest()
    assert cli.main(["plan", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--render", str(out)]) == 0
    assert hashlib.sha256(tgt.read_bytes()).hexdigest() == sha_before
    svg = out.read_text(encoding="utf-8")
    assert svg.startswith("<svg ") and "</svg>" in svg
    assert f"preview: {out}" in capsys.readouterr().out
    # the netdiff temp copy is gone — only OUT.svg persists
    assert not list(tmp_path.glob(".*.netdiff.*.tmp"))


def test_render_json_payload_carries_preview(tmp_path, capsys):
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    out = tmp_path / "p.svg"
    assert cli.main(["plan", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--render", str(out),
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"]["path"] == str(out)
    assert payload["preview"]["bytes"] == len(out.read_bytes())


def test_render_without_flag_reports_null_preview(tmp_path, capsys):
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    assert cli.main(["plan", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["preview"] is None


def test_render_works_under_no_net_diff(tmp_path, capsys):
    """--no-net-diff skips the diff, but the preview gets its own dry-apply."""
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    out = tmp_path / "p.svg"
    assert cli.main(["plan", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--no-net-diff",
                     "--render", str(out), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["net_diff"] is None
    assert payload["preview"]["path"] == str(out)
    assert out.exists()


def test_render_skipped_on_refused_oplist(tmp_path, capsys):
    """A dirty dry-apply must not render the pristine before-state as a plan."""
    tgt = _blank_sch(tmp_path)
    bad = _opsfile(tmp_path, {"op": "place_component", "lib_id": "Device:R",
                              "designator": "R1", "x_mil": 0, "y_mil": 0},
                   *_LABELS)  # missing --symbols -> SYMBOL_NOT_FOUND
    out = tmp_path / "p.svg"
    rc = cli.main(["plan", str(tgt), "--ops", str(bad), "--render", str(out)])
    assert rc != 0
    assert not out.exists()
    assert "preview skipped" in capsys.readouterr().err


def test_render_failure_is_nonfatal(tmp_path, capsys, monkeypatch):
    """A broken renderer warns on stderr and never changes the draw verdict."""
    from akcli import render_svg

    def _boom(*a, **k):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(render_svg, "render", _boom)
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    out = tmp_path / "p.svg"
    assert cli.main(["plan", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--render", str(out),
                     "--json"]) == 0
    captured = capsys.readouterr()
    assert "preview render unavailable" in captured.err
    assert json.loads(captured.out)["preview"] is None
    assert not out.exists()


def test_draw_apply_render_shows_after_state(tmp_path):
    tgt = _blank_sch(tmp_path)
    ops = _opsfile(tmp_path, _R1, *_LABELS)
    out = tmp_path / "p.svg"
    assert cli.main(["draw", str(tgt), "--ops", str(ops),
                     "--symbols", str(DEVICE), "--apply",
                     "--render", str(out), "--no-erc"]) == 0
    assert out.exists()
    # the applied file and the preview describe the same sheet
    assert "R1" in out.read_text(encoding="utf-8")
    assert '"R1"' in tgt.read_text(encoding="utf-8")
