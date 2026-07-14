"""Optional Windows file-based JSON bridge to a running Altium instance (SPEC §3.7).

This is the **Python half** of the live driver. It never touches Altium directly;
it talks to the DelphiScript half (``scripts/altium_api.pas``) over a small, atomic,
file-based protocol so the whole module is **unit-testable on macOS/Linux with no
Altium installed** — a fake "Altium" (a thread or a pre-seeded file) just writes the
``response.json`` the poller is waiting for.

Protocol (independent reimplementation; no source copied — SPEC Appendix B):

* The two sides agree on a shared *bridge directory* (``reqdir``). Every call carves
  out a **per-run unique ``0700`` sub-directory** (``run-<hex>/``) so concurrent or
  leftover runs never collide and other users cannot read the exchange.
* A single ``.lock`` file in the bridge directory enforces **single-flight**: only
  one request may be in flight at a time (``O_CREAT | O_EXCL``); contention raises
  :class:`BridgeBusy`.
* The request is written atomically: ``request.json.tmp`` (``O_NOFOLLOW``, mode
  ``0600``) → ``fsync`` → ``os.replace`` to ``request.json``. The watcher therefore
  only ever observes a complete file.
* The Python side polls for ``response.json`` every :data:`POLL_INTERVAL_S` seconds
  up to ``timeout``; a no-show raises :class:`TimeoutError`.
* :func:`ping` performs the ``altium_ping`` handshake. The reply carries
  ``{protocol_version, altium_version}``; a ``protocol_version`` that does not equal
  :data:`~akcli.ops.PROTOCOL_VERSION` is rejected with the frozen
  ``PROTOCOL_MISMATCH`` error code.

Error policy: the only structured (``AkcliError``) failure raised here is the
required ``PROTOCOL_MISMATCH`` handshake rejection (plus whatever
:func:`~akcli.ops.validate_oplist` flags for a malformed outgoing
op-list). The two bridge-transport conditions for which the frozen ``errors``
registry has no code — a busy lock and a response time-out — are surfaced as the
stdlib-flavoured :class:`BridgeBusy` and :class:`TimeoutError` rather than by
inventing un-registered error codes.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterator

from ...errors import AkcliError
from ...ops import PROTOCOL_VERSION, validate_oplist
from ...safety import safe_path

__all__ = [
    "send",
    "ping",
    "BridgeBusy",
    "default_bridge_dir",
    "REQUEST_NAME",
    "RESPONSE_NAME",
    "LOCK_NAME",
    "POLL_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
]

# --- protocol constants ----------------------------------------------------- #
REQUEST_NAME: str = "request.json"
REQUEST_TMP_NAME: str = "request.json.tmp"
RESPONSE_NAME: str = "response.json"
LOCK_NAME: str = ".lock"
RUN_DIR_PREFIX: str = "run-"
PING_COMMAND: str = "altium_ping"

# Module-level (monkeypatchable in tests) so a fast fake can shorten the poll.
POLL_INTERVAL_S: float = 0.2
DEFAULT_TIMEOUT_S: float = 30.0

# Hard cap on a response we are willing to read into memory.
_MAX_RESPONSE_BYTES: int = 16 * 1024 * 1024

# os.O_NOFOLLOW is POSIX-only; degrade to 0 (no-op flag) on platforms without it.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)


class BridgeBusy(RuntimeError):
    """Raised when another request already holds the single-flight ``.lock``."""


# --------------------------------------------------------------------------- #
# bridge-directory discovery
# --------------------------------------------------------------------------- #
def default_bridge_dir() -> Path:
    """Resolve the default shared bridge directory.

    Honours ``AKCLI_ALTIUM_BRIDGE_DIR`` when set; otherwise a stable per-user
    location under the system temp dir. Tests always pass an explicit ``reqdir``.
    """
    env = os.environ.get("AKCLI_ALTIUM_BRIDGE_DIR")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "akcli-altium-bridge"


# --------------------------------------------------------------------------- #
# low-level atomic / no-follow IO helpers
# --------------------------------------------------------------------------- #
def _write_json_atomic(target: Path, payload: dict) -> None:
    """Atomically write ``payload`` as JSON to ``target`` (tmp → fsync → replace)."""
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = target.with_name(REQUEST_TMP_NAME if target.name == REQUEST_NAME
                           else target.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json_nofollow(path: Path) -> dict:
    """Read + JSON-parse ``path`` without following a symlink, bounded in size."""
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError(f"bridge response exceeds {_MAX_RESPONSE_BYTES} bytes")
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("bridge response root must be a JSON object")
    return obj


# --------------------------------------------------------------------------- #
# single-flight lock + per-run session
# --------------------------------------------------------------------------- #
def _ensure_base(reqdir: Path | str) -> Path:
    base = Path(reqdir)
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(base, 0o700)
    return base.resolve()


@contextlib.contextmanager
def _session(reqdir: Path | str) -> Iterator[tuple[Path, Path]]:
    """Acquire the single-flight lock and a fresh 0700 per-run dir.

    Yields ``(base, run_dir)`` and guarantees the run dir is removed and the lock
    released on exit, even on error.
    """
    base = _ensure_base(reqdir)
    lock_path = base / LOCK_NAME
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise BridgeBusy(f"bridge busy: {lock_path} already held") from exc
    except OSError as exc:  # e.g. .lock is a symlink (O_NOFOLLOW) -> treat as busy
        raise BridgeBusy(f"bridge lock unavailable: {exc}") from exc

    run_dir: Path | None = None
    try:
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
        run_name = RUN_DIR_PREFIX + uuid.uuid4().hex
        run_dir = safe_path(base, run_name)
        os.mkdir(run_dir, 0o700)
        yield base, run_dir
    finally:
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.unlink(lock_path)


# --------------------------------------------------------------------------- #
# the request/response exchange
# --------------------------------------------------------------------------- #
def _exchange(payload: dict, reqdir: Path | str, timeout: float) -> dict:
    """Write ``payload`` as a request and poll for the matching response dict."""
    if timeout <= 0:
        raise ValueError("timeout must be > 0")
    with _session(reqdir) as (base, run_dir):
        req = safe_path(base, run_dir / REQUEST_NAME)
        resp = safe_path(base, run_dir / RESPONSE_NAME)

        # Defensive: never read a stale response from a recycled dir.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(resp)

        _write_json_atomic(req, payload)

        deadline = time.monotonic() + timeout
        while True:
            try:
                return _read_json_nofollow(resp)
            except (FileNotFoundError, PermissionError):
                pass  # not answered yet, or a transient Windows sharing lock mid-replace
            except (json.JSONDecodeError, ValueError):
                pass  # mid-write / partial; retry until the deadline
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no {RESPONSE_NAME} from Altium within {timeout}s (run {run_dir.name})"
                )
            time.sleep(POLL_INTERVAL_S)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def send(op_or_oplist: dict, reqdir: Path | str, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Send a single op or a full op-list to Altium and return its response dict.

    A single op dict (``{"op": ...}``) is wrapped into a one-op op-list. The
    outgoing op-list is stamped with ``protocol_version``/``target_format``/
    ``run_id`` defaults (only when absent) and then **validated** with
    :func:`~akcli.ops.validate_oplist`; any structural problem is raised
    as an :class:`~akcli.errors.AkcliError` *before* anything is written.
    The response is the executor's list of per-op result objects (SPEC §2.4),
    returned verbatim as a dict.
    """
    if not isinstance(op_or_oplist, dict):
        raise AkcliError("OP_UNSUPPORTED", "send() requires an op or op-list dict")

    if "ops" in op_or_oplist:
        oplist = dict(op_or_oplist)
    else:
        oplist = {"ops": [op_or_oplist]}
    oplist.setdefault("protocol_version", PROTOCOL_VERSION)
    oplist.setdefault("target_format", "altium")
    oplist.setdefault("run_id", uuid.uuid4().hex)

    errs = validate_oplist(oplist)
    if errs:
        first = errs[0]
        extra = "" if len(errs) == 1 else f" (+{len(errs) - 1} more)"
        raise AkcliError(first.code, f"{first.message}{extra}")

    return _exchange(oplist, reqdir, timeout)


def ping(reqdir: Path | str | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Perform the ``altium_ping`` handshake; return ``{protocol_version, altium_version}``.

    Rejects a reply whose ``protocol_version`` is missing or does not equal
    :data:`~akcli.ops.PROTOCOL_VERSION` with the frozen
    ``PROTOCOL_MISMATCH`` error code. ``reqdir`` defaults to
    :func:`default_bridge_dir`.
    """
    if reqdir is None:
        reqdir = default_bridge_dir()

    request = {"protocol_version": PROTOCOL_VERSION, "command": PING_COMMAND}
    resp = _exchange(request, reqdir, timeout)

    rv = resp.get("protocol_version")
    if rv != PROTOCOL_VERSION:
        raise AkcliError(
            "PROTOCOL_MISMATCH",
            f"Altium bridge protocol_version {rv!r} != {PROTOCOL_VERSION} "
            f"(altium_version={resp.get('altium_version')!r})",
        )
    return resp
