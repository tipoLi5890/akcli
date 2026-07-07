#!/usr/bin/env python3
"""calc-view — a local web UI for `akcli calc` (localhost only, zero deps).

Serves a single-page dashboard: pick any of the registered calculators in a
grouped sidebar, fill the generated form (engineering notation accepted),
and get the results **with the formal reference** plus a physical-style SVG
illustration of what is being computed.

Run:
    python3 tools/calc-view/server.py            # http://127.0.0.1:8766
    python3 tools/calc-view/server.py --port N --no-browser
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "src"))

from altium_kicad_cli.calc import CALCS, compute  # noqa: E402
from altium_kicad_cli.calc.registry import CalcError  # noqa: E402


def _registry_payload() -> dict:
    groups: dict[str, list] = {}
    for c in sorted(CALCS.values(), key=lambda c: (c.group, c.name)):
        groups.setdefault(c.group, []).append({
            "name": c.name,
            "title": c.title,
            "reference": c.reference,
            "notes": c.notes,
            "params": [{
                "name": p.name, "unit": p.unit, "help": p.help,
                "required": p.default is None,
                "default": p.default,
                "choices": list(p.choices),
                "text": p.text,
            } for p in c.params],
        })
    return {"groups": groups}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            try:
                body = (_HERE / "index.html").read_bytes()
            except OSError:
                self._send(404, b"index.html missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif url.path == "/api/list":
            self._json(200, _registry_payload())
        elif url.path == "/api/run":
            q = parse_qs(url.query)
            name = (q.pop("name", [""]))[0]
            raw = {k: v[0] for k, v in q.items() if v and v[0] != ""}
            if name not in CALCS:
                self._json(400, {"error": f"unknown calculator {name!r}"})
                return
            try:
                self._json(200, compute(name, raw))
            except CalcError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # keep the page alive on any math error
                self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # quiet
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"calc-view: {len(CALCS)} calculators at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
