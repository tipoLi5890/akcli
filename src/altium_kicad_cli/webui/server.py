"""``akcli view`` — ONE local server for both dashboards.

Routes
------
- ``/``                hub — the entry page that links both dashboards
- ``/calc``            calculator bench (always available)
- ``/live``            schematic watch timeline (live when a target is watched)
- ``/api/list|run|ops``            calculator registry / compute / op-list export
- ``/live/state.json``             the step timeline
- ``/live/step-*.svg``             immutable per-step sheet exports
- ``/live/events``                 Server-Sent Events: pushed on every state change
- ``/live/bom``                    BOM lines of the watched sheet (offline);
                                   ``?check=1`` adds JLCPCB stock/price — the
                                   ONE endpoint that touches the network, and
                                   only when the user explicitly clicks
- ``POST /live/note``              annotate the next step (writes ``note.txt``)
- ``POST /live/clear``             wipe the timeline (steps + SVGs)

Design notes
------------
- 127.0.0.1 only, zero third-party dependencies, HTML from package data.
- Requests whose ``Host`` header is not localhost-family are refused (403):
  a hostile page can rebind its DNS name to 127.0.0.1, but the victim's
  browser still sends the hostile name as ``Host``. POSTs additionally
  require the ``Origin`` header (when a browser sends one) to point back
  at loopback — cross-site POSTs carry the attacker's origin.
- The watcher thread never dies: any crash in the poll loop lands in
  ``state["watcher_error"]`` (rendered as a banner chip by live.html) and
  the loop retries; a successful step clears it.
- Steps publish in two phases: the SVG + part/net counts appear as soon as the
  export finishes (seconds), then KiCad's JSON ERC back-fills the same step
  (``erc_pending`` -> ``erc_err``/``erc_warn``/``erc``) with a second publish.
- kicad-cli v8/v9 report ERC positions in mm/100 despite ``coordinate_units:
  mm``; positions ship verbatim and the dashboard picks the scale that lands
  markers inside the page.
- The timeline keeps at most ``max_steps`` steps (oldest SVGs deleted);
  ``step.n`` stays monotonic across trimming AND ``/live/clear`` (via
  ``state.next_n``) so history stays honest and old cached SVG URLs can
  never alias a new step.
- Responses >1 KiB are gzipped when the client accepts it; a step SVG is
  cached as immutable only when fetched with its ``?v=<ts>`` fingerprint
  (the client always appends one) — a bare URL gets ``no-store``.
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import page
from .. import __version__
from ..calc import CALCS, compute
from ..calc.opsmap import MAPPABLE, to_oplist
from ..calc.registry import CalcError

DEFAULT_PORT = 8765
PORT_TRIES = 20          # auto-increment window when the port is taken
MAX_NOTE = 500           # characters accepted by POST /live/note


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _host_only(netloc: str) -> str:
    """The host part of a Host/authority value: port stripped, lowercased."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):                    # bracketed IPv6: [::1]:8765
        return netloc.split("]", 1)[0] + "]"
    if netloc.count(":") == 1:                    # host:port
        return netloc.rsplit(":", 1)[0]
    return netloc                                 # bare host or raw IPv6


def _is_local_host(host: str | None) -> bool:
    """True when a ``Host`` header names this machine's loopback.

    Refusing anything else defeats DNS rebinding: evil.com can resolve to
    127.0.0.1, but the browser still sends ``Host: evil.com``. An absent
    Host passes — rebinding needs a browser, and browsers always send it.
    """
    return host is None or _host_only(host) in _LOCAL_HOSTS


def _origin_ok(origin: str | None, host: str | None) -> bool:
    """POST origin policy: pass with no ``Origin`` (curl, CLI clients) or
    one pointing back at loopback; ``null`` and cross-site origins fail."""
    if origin is None:
        return True
    netloc = urlparse(origin).netloc
    if host and netloc and netloc.lower() == host.strip().lower():
        return True
    return bool(netloc) and _host_only(netloc) in _LOCAL_HOSTS


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer minus the traceback spam when a browser drops
    a connection mid-response (tab closed during an SVG fetch)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _bind(port: int, handler, tries: int = PORT_TRIES):
    """``(server, port)`` on the first free port in ``[port, port+tries)``;
    ``(None, port)`` when the whole window is taken."""
    for cand in range(port, port + tries):
        try:
            return _Server(("127.0.0.1", cand), handler), cand
        except OSError:
            continue
    return None, port


def _find_kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    return mac if os.path.exists(mac) else "kicad-cli"


def _registry_payload(dash: "Dash") -> dict:
    groups: dict[str, list] = {}
    for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name)):
        groups.setdefault(c.group, []).append({
            "name": c.name,
            "title": c.title,
            "reference": c.reference,
            "notes": c.notes,
            "mappable": c.name in MAPPABLE,
            "params": [{
                "name": p.name, "unit": p.unit, "help": p.help,
                "required": p.default is None,
                "default": p.default,
                "choices": list(p.choices),
                "text": p.text,
            } for p in c.params],
        })
    return {"groups": groups,
            "meta": {"count": len(CALCS), "version": __version__,
                     "watching": dash.target.name if dash.target else None}}


class _Bus:
    """Fan-out of state-change events to the SSE subscribers."""

    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=16)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict) -> None:
        data = json.dumps(payload)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:      # slow consumer: it will resync via state.json
                pass


class Dash:
    """State shared between the watcher thread and the HTTP handler."""

    def __init__(self, state_dir: Path | None, target: Path | None,
                 max_steps: int = 500) -> None:
        self.state_dir = state_dir
        self.target = target
        self.max_steps = max_steps
        self.bus = _Bus()
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict:
        state: dict = {}
        f = self.state_dir / "state.json" if self.state_dir else None
        if f and f.exists():
            try:
                raw = json.loads(f.read_text())
                if isinstance(raw, dict):
                    state = raw
            except ValueError:
                pass
        # normalize: a foreign or corrupt state.json must not crash later
        # code that trusts state["version"] / state["steps"] shapes
        if not isinstance(state.get("version"), int):
            state["version"] = 0
        steps = state.get("steps")
        state["steps"] = ([s for s in steps if isinstance(s, dict)]
                          if isinstance(steps, list) else [])
        state["file"] = self.target.name if self.target else "—"
        state["watching"] = self.target is not None
        return state

    def publish(self) -> None:
        """Bump the version, persist state.json atomically, wake SSE clients."""
        with self.lock:
            self.state["version"] += 1
            version = self.state["version"]
            if self.state_dir:
                tmp = self.state_dir / "state.json.tmp"
                tmp.write_text(json.dumps(self.state, ensure_ascii=False))
                tmp.replace(self.state_dir / "state.json")
        self.bus.publish({"version": version})

    def state_json(self) -> bytes:
        """The timeline as bytes — the in-memory state is authoritative
        (state.json on disk exists only to persist it across runs)."""
        with self.lock:
            return json.dumps(self.state, ensure_ascii=False).encode("utf-8")

    def clear(self) -> None:
        """Drop every step and its exported SVGs; version AND step numbers
        stay monotonic (``next_n`` seeds the watcher after the wipe)."""
        with self.lock:
            steps = self.state["steps"]
            if steps:
                self.state["next_n"] = steps[-1].get("n", 0) + 1
            for step in steps:
                for name in step.get("sheets") or [step.get("svg")]:
                    if name and self.state_dir:
                        (self.state_dir / name).unlink(missing_ok=True)
            self.state["steps"] = []
        self.publish()


class _Watcher(threading.Thread):
    """Poll the schematic's mtime; on change append a step to the timeline."""

    def __init__(self, dash: Dash) -> None:
        super().__init__(daemon=True)
        self.dash = dash
        self.target = dash.target
        self.kicad_cli = _find_kicad_cli()
        self.auto_revert = os.environ.get("AUTO_REVERT") == "1"
        m = self._stat_mtime()
        self._last = m or 0.0
        self._pending = m is not None   # emit an initial baseline step

    # ---------------- helpers ----------------
    def _stat_mtime(self) -> float | None:
        """The target's mtime, or None while it doesn't exist (editors
        replace files via unlink+rename, so brief absence is normal)."""
        try:
            return self.target.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _export_svgs(self, n: int) -> tuple[list[str], list[str]]:
        """Export every sheet; returns ([step files], [sheet labels]).

        --no-background-color matters: the theme background is a page-sized
        rect, which would defeat the dashboard's getBBox() content crop.
        """
        with tempfile.TemporaryDirectory() as td:
            try:
                r = subprocess.run(
                    [self.kicad_cli, "sch", "export", "svg",
                     "--exclude-drawing-sheet", "--no-background-color",
                     "-o", td, str(self.target)],
                    capture_output=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                with self.dash.lock:
                    self.dash.state["watcher_error"] = \
                        "kicad-cli svg export timed out (60s)"
                self.dash.publish()
                return [], []
            svgs = sorted(Path(td).glob("*.svg"))
            if r.returncode != 0 or not svgs:
                return [], []
            files, names = [], []
            stem = self.target.stem
            for k, src in enumerate(svgs):
                name = f"step-{n}.svg" if k == 0 else f"step-{n}-{k + 1}.svg"
                shutil.copy(src, self.dash.state_dir / name)
                files.append(name)
                label = src.stem
                if label.startswith(stem):
                    label = label[len(stem):].lstrip("-_ ") or "root"
                names.append(label)
            return files, names

    def _wait_stable(self, quiet: float = 1.2, max_wait: float = 8.0) -> float:
        """Wait until the mtime stops changing (collapse write bursts);
        the file vanishing mid-save just extends the wait."""
        deadline = time.time() + max_wait
        m = self._stat_mtime()
        while time.time() < deadline:
            time.sleep(quiet)
            m2 = self._stat_mtime()
            if m2 is not None and m2 == m:
                return m
            if m2 is not None:
                m = m2
        return m or 0.0

    def _erc_detail(self) -> tuple[int | None, int | None, list[dict]]:
        """(errors, warnings, violations) from KiCad's JSON ERC report."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "erc.json"
            try:
                subprocess.run(
                    [self.kicad_cli, "sch", "erc", "--format", "json",
                     "--output", str(out), str(self.target)],
                    capture_output=True, timeout=90,
                )
                rep = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                return None, None, []
        err = warn = 0
        viol: list[dict] = []
        for sheet in rep.get("sheets", []):
            for v in sheet.get("violations", []):
                sev = v.get("severity", "")
                if sev == "error":
                    err += 1
                elif sev == "warning":
                    warn += 1
                item = (v.get("items") or [{}])[0]
                pos = item.get("pos") or {}
                viol.append({
                    "severity": sev,
                    "type": v.get("type", ""),
                    "description": v.get("description", ""),
                    "item": item.get("description", ""),
                    "x": pos.get("x"), "y": pos.get("y"),
                })
        return err, warn, viol[:200]

    def _akcli_counts(self) -> tuple[int | None, int | None]:
        """(components, nets) via the in-process reader (no subprocess)."""
        try:
            from ..readers import kicad as kreader
            sch = kreader.read_sch(str(self.target))
            return len(sch.components), len(sch.nets)
        except Exception:
            return None, None

    def _take_note(self) -> str:
        note = self.target.parent / "note.txt"
        if note.exists():
            text = note.read_text(encoding="utf-8").strip()
            note.unlink()
            return text
        return ""

    def _revert_kicad(self) -> None:
        """Best-effort File>Revert in an open KiCad editor (en/zh menus)."""
        script = '''
        tell application "Schematic Editor" to activate
        delay 0.3
        tell application "System Events"
          tell process "eeschema"
            repeat with pair in {{"File", "Revert"}, {"檔案", "還原"}, {"檔案", "回復"}}
              try
                click menu item (item 2 of pair) of menu (item 1 of pair) of menu bar item (item 1 of pair) of menu bar 1
                delay 0.4
                try
                  click button 1 of window 1
                end try
                return
              end try
            end repeat
          end tell
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=10)
        except Exception:
            pass  # advisory only

    def _trim(self) -> None:
        """Under dash.lock: enforce max_steps, deleting the dropped SVGs."""
        if self.dash.max_steps <= 0:
            return
        steps = self.dash.state["steps"]
        while len(steps) > self.dash.max_steps:
            old = steps.pop(0)
            for name in old.get("sheets") or [old.get("svg")]:
                if name:
                    (self.dash.state_dir / name).unlink(missing_ok=True)

    # ---------------- loop ----------------
    def _poll_once(self) -> None:
        """One poll: when the target changed (or the baseline is pending),
        export + publish a step. Exceptions propagate to run()'s guard."""
        dash = self.dash
        m = self._stat_mtime()
        if m is None or (m == self._last and not self._pending):
            return
        m = self._wait_stable()
        with dash.lock:
            n = (dash.state["steps"][-1]["n"] + 1 if dash.state["steps"]
                 else int(dash.state.get("next_n") or 1))
        files, names = self._export_svgs(n)
        if files:
            parts, nets = self._akcli_counts()
            # Hold the pending note for a step with content: an
            # intermediate empty write must not consume it.
            note = self._take_note() if parts else ""
            step = {
                "n": n, "svg": files[0],
                "sheets": files, "sheet_names": names,
                "time": datetime.now().strftime("%H:%M:%S"),
                "ts": time.time(),
                "note": note or ("baseline" if self._pending else ""),
                "erc_pending": True,
                "parts": parts, "nets": nets,
            }
            # phase 1: SVG + counts appear immediately
            with dash.lock:
                dash.state.pop("watcher_error", None)   # we recovered
                dash.state["steps"].append(step)
                self._trim()
            dash.publish()
            # phase 2: ERC back-fills the same step
            err, warn, viol = self._erc_detail()
            with dash.lock:
                step.pop("erc_pending", None)
                step.update(erc_err=err, erc_warn=warn, erc=viol)
            dash.publish()
            print(f"step {n} @ {step['time']} "
                  f"erc={err}E/{warn}W parts={parts} nets={nets}",
                  flush=True)
            if self.auto_revert and not self._pending:
                self._revert_kicad()
        self._last, self._pending = m, False

    def run(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception as exc:    # the dashboard must outlive any crash
                with self.dash.lock:
                    self.dash.state["watcher_error"] = \
                        f"{type(exc).__name__}: {exc}"
                try:
                    self.dash.publish()
                except Exception:       # e.g. state.json write failed too
                    pass
                time.sleep(2.0)         # back off; the next poll retries
                continue
            time.sleep(1.0)


def _make_handler(dash: Dash):
    class Handler(BaseHTTPRequestHandler):
        # ------------- plumbing -------------
        def _send(self, code: int, body: bytes, ctype: str,
                  cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if len(body) > 1024 and "gzip" in (
                    self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 5)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _guard(self, *, post: bool = False) -> bool:
            """False (with a 403 already sent) unless the request is local:
            non-loopback ``Host`` = DNS rebinding, foreign ``Origin`` on a
            POST = cross-site request forgery."""
            host = self.headers.get("Host")
            if not _is_local_host(host):
                self._json(403, {"error": "forbidden: non-local Host"})
                return False
            if post and not _origin_ok(self.headers.get("Origin"), host):
                self._json(403, {"error": "forbidden: cross-origin POST"})
                return False
            return True

        # ------------- GET -------------
        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send(200, page("hub.html"), "text/html; charset=utf-8")
            elif path == "/calc":
                self._send(200, page("calc.html"), "text/html; charset=utf-8")
            elif path == "/live":
                self._send(200, page("live.html"), "text/html; charset=utf-8")
            elif path == "/api/list":
                self._json(200, _registry_payload(dash))
            elif path in ("/api/run", "/api/ops"):
                self._calc(url)
            elif path == "/live/state.json":
                self._send(200, dash.state_json(),
                           "application/json; charset=utf-8")
            elif path == "/live/bom":
                self._live_bom(parse_qs(url.query))
            elif path == "/live/events":
                self._sse()
            else:
                name = path[len("/live/"):] if path.startswith("/live/") else ""
                # only the files the watcher writes; no directory traversal
                if (dash.state_dir is not None
                        and name.startswith("step-") and name.endswith(".svg")
                        and "/" not in name and ".." not in name
                        and (dash.state_dir / name).exists()):
                    # immutable only under a ?v= fingerprint: a bare URL
                    # could be re-served with fresh bytes after a clear
                    cache = ("public, max-age=31536000, immutable"
                             if parse_qs(url.query).get("v") else "no-store")
                    self._send(200, (dash.state_dir / name).read_bytes(),
                               "image/svg+xml; charset=utf-8", cache=cache)
                else:
                    self._send(404, b"not found", "text/plain")

        def _calc(self, url) -> None:
            q = parse_qs(url.query)
            name = (q.pop("name", [""]))[0]
            raw = {k: v[0] for k, v in q.items() if v and v[0] != ""}
            if name not in CALCS:
                self._json(400, {"error": f"unknown calculator {name!r}"})
                return
            try:
                doc = compute(name, raw)
                if url.path == "/api/ops":
                    doc = to_oplist(name, doc)
                self._json(200, doc)
            except CalcError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # keep the page alive on any math error
                self._json(400, {"error": f"{type(exc).__name__}: {exc}"})

        def _live_bom(self, q: dict) -> None:
            """BOM of the watched sheet; ``check=1`` adds catalog data."""
            if dash.target is None or not dash.target.exists():
                self._json(409, {"error": "not watching a schematic"})
                return
            from ..parts import bom_jlc
            from ..readers import kicad as kreader
            try:
                sch = kreader.read_sch(str(dash.target))
            except Exception as exc:
                self._json(400, {"error": f"read failed: {exc}"})
                return
            if (q.get("check") or ["0"])[0] == "1":
                from ..parts import search as parts_search
                try:
                    lines = bom_jlc.check(
                        sch, cache_dir=parts_search.default_cache_dir())
                    self._json(200, {"checked": True,
                                     "lines": [ln.to_dict() for ln in lines],
                                     "totals": bom_jlc.totals(lines)})
                except parts_search.JlcNetworkError as exc:
                    self._json(502, {"error": f"network: {exc.message}"})
                except Exception as exc:   # a BOM bug must not kill the page
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            else:
                # prefer the public name; older bom_jlc only had the
                # underscore-private one
                collect = (getattr(bom_jlc, "collect_lines", None)
                           or bom_jlc._collect_lines)
                try:
                    lines = collect(sch)
                    self._json(200, {"checked": False,
                                     "lines": [ln.to_dict() for ln in lines]})
                except Exception as exc:
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _sse(self) -> None:
            """Push {"version": N} whenever the timeline changes."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = dash.bus.subscribe()
            try:
                with dash.lock:
                    hello = json.dumps({"version": dash.state["version"]})
                self.wfile.write(f"data: {hello}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # keep-alive
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                dash.bus.unsubscribe(q)

        # ------------- POST -------------
        def do_POST(self):  # noqa: N802
            if not self._guard(post=True):
                return
            path = urlparse(self.path).path
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = self.rfile.read(length).decode("utf-8", "replace").strip()
            if path == "/live/note":
                if dash.target is None:
                    self._json(409, {"error": "not watching a schematic"})
                    return
                (dash.target.parent / "note.txt").write_text(
                    body[:MAX_NOTE] + "\n", encoding="utf-8")
                self._json(200, {"ok": True})
            elif path == "/live/clear":
                dash.clear()
                self._json(200, {"ok": True})
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):  # quiet
            pass

    return Handler


def serve(
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    target: str | Path | None = None,
    state_dir: str | Path | None = None,
    max_steps: int = 500,
) -> int:
    tgt = Path(target).resolve() if target else None
    sdir: Path | None = None
    if tgt or state_dir:
        sdir = Path(state_dir) if state_dir else Path(
            tempfile.mkdtemp(prefix="akcli-view-"))
        sdir.mkdir(parents=True, exist_ok=True)

    dash = Dash(sdir, tgt, max_steps=max_steps)
    if tgt:
        _Watcher(dash).start()

    srv, cand = _bind(port, _make_handler(dash))
    if srv is None:
        print(f"ERROR: no free port in {port}–{port + PORT_TRIES - 1}")
        return 1
    if cand != port:
        print(f"view: port {port} busy, using {cand}")

    url = f"http://127.0.0.1:{cand}"
    print(f"view: hub at {url}  ·  {len(CALCS)} calculators at {url}/calc")
    if tgt:
        print(f"view: watching {tgt}  ->  {url}/live")
        print(f"view: state in {sdir}  (keep ≤{max_steps or '∞'} steps)")
    print("view: Ctrl-C to stop")
    if open_browser:
        # watching -> land on the timeline, not the hub
        webbrowser.open(url + ("/live" if tgt else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
