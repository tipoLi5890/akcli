"""Offline unit tests for :mod:`akcli.drivers.altium_live.bridge` (SPEC §3.7).

The bridge is the Python half of an optional Windows live driver, but it is designed
to be exercised with **no Altium installed**: a fake "Altium" — a background thread
that watches the bridge directory and writes ``response.json`` — stands in for the
DelphiScript half. These tests cover the request round-trip, the single-flight lock,
the ``altium_ping`` handshake and ``PROTOCOL_MISMATCH`` rejection, validation of the
outgoing op-list, and the response time-out. None of it requires Windows or Altium.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from akcli.drivers.altium_live import bridge
from akcli.errors import AkcliError


# --------------------------------------------------------------------------- #
# fake "Altium": watch reqdir for run-*/request.json, write response.json
# --------------------------------------------------------------------------- #
class FakeAltium(threading.Thread):
    """Stand-in for the DelphiScript half.

    Polls ``base`` for any ``run-*/request.json``, hands it to ``responder`` and
    writes whatever that returns into the same run dir as ``response.json`` (atomic
    write to mimic the real script and avoid partial reads). Serves ``max_requests``
    requests then stops.
    """

    def __init__(self, base: Path, responder, max_requests: int = 1, poll: float = 0.01):
        super().__init__(daemon=True)
        self.base = Path(base)
        self.responder = responder
        self.max_requests = max_requests
        self.poll = poll
        self.seen_requests: list[dict] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _atomic_write(self, path: Path, payload: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def run(self) -> None:
        served = 0
        handled: set[Path] = set()
        while not self._stop_event.is_set() and served < self.max_requests:
            if not self.base.exists():
                time.sleep(self.poll)
                continue
            for req in sorted(self.base.glob(f"{bridge.RUN_DIR_PREFIX}*/{bridge.REQUEST_NAME}")):
                if req in handled:
                    continue
                try:
                    request = json.loads(req.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                handled.add(req)
                self.seen_requests.append(request)
                response = self.responder(request)
                if response is not None:
                    self._atomic_write(req.parent / bridge.RESPONSE_NAME, response)
                served += 1
                if served >= self.max_requests:
                    break
            time.sleep(self.poll)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Keep the bridge's own poll snappy so tests stay fast.
    monkeypatch.setattr(bridge, "POLL_INTERVAL_S", 0.01)


@pytest.fixture
def reqdir(tmp_path: Path) -> Path:
    d = tmp_path / "bridge"
    return d


def _start_fake(base: Path, responder, **kw) -> FakeAltium:
    fake = FakeAltium(base, responder, **kw)
    fake.start()
    return fake


def _good_oplist() -> dict:
    return {
        "protocol_version": 1,
        "target_format": "altium",
        "ops": [
            {"op": "place_component", "lib_id": "Device:R", "designator": "R1",
             "x_mil": 1000, "y_mil": 1000},
        ],
    }


# --------------------------------------------------------------------------- #
# request round-trip
# --------------------------------------------------------------------------- #
def test_send_round_trip(reqdir):
    def responder(request):
        # Echo a per-op result list keyed back to the request's run_id.
        return {
            "run_id": request.get("run_id"),
            "results": [
                {"op_index": 0, "op": "place_component", "status": "ok",
                 "created_uuids": ["abc"], "error_code": None, "message": ""},
            ],
        }

    fake = _start_fake(reqdir, responder)
    try:
        resp = bridge.send(_good_oplist(), reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert resp["results"][0]["status"] == "ok"
    assert resp["results"][0]["created_uuids"] == ["abc"]

    # The fake saw exactly our op-list, stamped with protocol_version + a run_id.
    assert len(fake.seen_requests) == 1
    seen = fake.seen_requests[0]
    assert seen["protocol_version"] == bridge.PROTOCOL_VERSION
    assert seen["target_format"] == "altium"
    assert seen["ops"][0]["designator"] == "R1"
    assert isinstance(seen["run_id"], str) and seen["run_id"]
    assert resp["run_id"] == seen["run_id"]


def test_send_single_op_is_wrapped(reqdir):
    captured = {}

    def responder(request):
        captured.update(request)
        return {"results": []}

    fake = _start_fake(reqdir, responder)
    try:
        bridge.send({"op": "add_junction", "at": [100, 100]}, reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert captured["ops"] == [{"op": "add_junction", "at": [100, 100]}]
    assert captured["protocol_version"] == bridge.PROTOCOL_VERSION


# --------------------------------------------------------------------------- #
# single-flight lock
# --------------------------------------------------------------------------- #
def test_lock_blocks_concurrent_request(reqdir):
    base = bridge._ensure_base(reqdir)
    # Hold the lock as if another request were in flight.
    lock = base / bridge.LOCK_NAME
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with pytest.raises(bridge.BridgeBusy):
            bridge.send(_good_oplist(), reqdir, timeout=1.0)
    finally:
        os.close(fd)
        os.unlink(lock)


def test_lock_released_and_rundir_cleaned_after_success(reqdir):
    fake = _start_fake(reqdir, lambda req: {"results": []})
    try:
        bridge.send(_good_oplist(), reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    base = Path(reqdir)
    assert not (base / bridge.LOCK_NAME).exists()  # lock released
    assert list(base.glob(f"{bridge.RUN_DIR_PREFIX}*")) == []  # run dir cleaned up


def test_lock_released_after_failure(reqdir):
    # A timeout (no responder) must still release the lock + clean the run dir.
    with pytest.raises(TimeoutError):
        bridge.send(_good_oplist(), reqdir, timeout=0.2)
    base = Path(reqdir)
    assert not (base / bridge.LOCK_NAME).exists()
    assert list(base.glob(f"{bridge.RUN_DIR_PREFIX}*")) == []


# --------------------------------------------------------------------------- #
# per-run dir is private (0700) on POSIX
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_run_dir_is_0700(reqdir):
    seen_mode = {}

    def responder(request):
        # Inspect the run dir's mode while the request is live.
        for d in Path(reqdir).glob(f"{bridge.RUN_DIR_PREFIX}*"):
            seen_mode["mode"] = d.stat().st_mode & 0o777
        return {"results": []}

    fake = _start_fake(reqdir, responder)
    try:
        bridge.send(_good_oplist(), reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert seen_mode.get("mode") == 0o700


# --------------------------------------------------------------------------- #
# ping handshake
# --------------------------------------------------------------------------- #
def test_ping_handshake(reqdir):
    def responder(request):
        assert request["command"] == bridge.PING_COMMAND
        return {"protocol_version": 1, "altium_version": "24.11.1"}

    fake = _start_fake(reqdir, responder)
    try:
        resp = bridge.ping(reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert resp["protocol_version"] == bridge.PROTOCOL_VERSION
    assert resp["altium_version"] == "24.11.1"


def test_ping_protocol_mismatch_rejected(reqdir):
    def responder(request):
        return {"protocol_version": 2, "altium_version": "25.0.0"}

    fake = _start_fake(reqdir, responder)
    try:
        with pytest.raises(AkcliError) as ei:
            bridge.ping(reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_ping_missing_protocol_version_rejected(reqdir):
    fake = _start_fake(reqdir, lambda req: {"altium_version": "24.0.0"})
    try:
        with pytest.raises(AkcliError) as ei:
            bridge.ping(reqdir, timeout=5.0)
    finally:
        fake.stop()
        fake.join(timeout=5)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_ping_default_dir_via_env(tmp_path, monkeypatch):
    base = tmp_path / "envbridge"
    monkeypatch.setenv("AKCLI_ALTIUM_BRIDGE_DIR", str(base))
    assert bridge.default_bridge_dir() == base

    fake = _start_fake(base, lambda req: {"protocol_version": 1, "altium_version": "x"})
    try:
        resp = bridge.ping(timeout=5.0)  # no reqdir -> default_bridge_dir()
    finally:
        fake.stop()
        fake.join(timeout=5)
    assert resp["altium_version"] == "x"


# --------------------------------------------------------------------------- #
# outgoing op-list validation (before any write)
# --------------------------------------------------------------------------- #
def test_send_rejects_invalid_op_before_writing(reqdir):
    invoked = {"n": 0}

    def responder(request):
        invoked["n"] += 1
        return {"results": []}

    fake = _start_fake(reqdir, responder)
    try:
        with pytest.raises(AkcliError) as ei:
            bridge.send({"op": "no_such_op", "foo": 1}, reqdir, timeout=2.0)
        time.sleep(0.1)  # give the fake a chance to (not) see anything
    finally:
        fake.stop()
        fake.join(timeout=5)

    assert ei.value.code == "OP_UNSUPPORTED"
    assert invoked["n"] == 0  # never reached the wire
    # No request was ever written.
    assert list(Path(reqdir).glob(f"{bridge.RUN_DIR_PREFIX}*")) == []


def test_send_rejects_protocol_mismatch_in_oplist(reqdir):
    bad = _good_oplist()
    bad["protocol_version"] = 2  # explicit, must not be overridden
    with pytest.raises(AkcliError) as ei:
        bridge.send(bad, reqdir, timeout=2.0)
    assert ei.value.code == "PROTOCOL_MISMATCH"


def test_send_rejects_non_dict(reqdir):
    with pytest.raises(AkcliError) as ei:
        bridge.send([1, 2, 3], reqdir, timeout=2.0)  # type: ignore[arg-type]
    assert ei.value.code == "OP_UNSUPPORTED"


# --------------------------------------------------------------------------- #
# response time-out
# --------------------------------------------------------------------------- #
def test_send_times_out_without_responder(reqdir):
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        bridge.send(_good_oplist(), reqdir, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0  # bounded by the timeout, not hung


def test_zero_timeout_rejected(reqdir):
    with pytest.raises(ValueError):
        bridge.send(_good_oplist(), reqdir, timeout=0)
