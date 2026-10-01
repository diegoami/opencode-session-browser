"""Tiny local HTTP server (stdlib only). Read-only API + static UI. No telemetry, no external calls."""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import exporters
from .registry import Registry

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)


def make_handler(reg: Registry, allow_remote: bool, verbose: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "opencode-session-browser"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log query strings (may contain search terms)
            if verbose:
                sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

        # -- helpers --
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return allow_remote or bool(LOCAL_HOSTS.match(self.headers.get("Host", "")))

        # -- routing --
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            try:
                self._route(urlparse(self.path))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001
                try:
                    self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                except OSError:
                    pass

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            u = urlparse(self.path)
            origin = self.headers.get("Origin")
            if origin and not LOCAL_HOSTS.match(urlparse(origin).netloc):
                return self._send(403, b"forbidden origin", "text/plain")
            if u.path == "/api/refresh":     # only "write" in the app: re-read sources / copy snapshots
                reg.refresh_all(force=True, wait=True)
                return self._json({"ok": True, "version": reg.version})
            self._send(404, b"not found", "text/plain")

        def _route(self, u):
            path = unquote(u.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if path == "/api/sessions":
                return self._json(reg.query(q))
            if path == "/api/sources":
                return self._json(reg.diagnostics())
            if path == "/api/changes":
                out = {"version": reg.version, "indexing": reg.indexing}
                if q.get("key"):
                    f = reg.get(q["key"])
                    out["message_fingerprint"] = repr(f[0].message_fingerprint(f[1].id)) if f else None
                return self._json(out)
            m = re.match(r"^/api/sessions/([^/]+)(?:/(messages|export\.json|export\.md))?(?:/([^/]+))?$", path)
            if m:
                key, sub, mid = m.groups()
                found = reg.get(key)
                if not found:
                    return self._json({"error": "session not found", "key": key}, 404)
                src, s = found
                if sub is None:
                    return self._json(reg.detail(key))
                limit = int(q.get("clip", 30000))
                if sub == "messages":
                    entries = src.load_messages(s.id)
                    if mid:
                        e = next((e for e in entries if e["id"] == mid), None)
                        if e is None:
                            return self._json({"error": "message not found"}, 404)
                        return self._json(e if q.get("full") else exporters.light_entry(e, 0))
                    return self._json({"fingerprint": repr(src.message_fingerprint(s.id)), "count": len(entries),
                                       "messages": [exporters.light_entry(e, limit) for e in entries]})
                entries = src.load_messages(s.id)
                detail = reg.detail(key)
                fname = re.sub(r"[^A-Za-z0-9._-]+", "_", s.id)
                if sub == "export.json":
                    body = exporters.export_json(detail, entries).encode("utf-8")
                    return self._send(200, body, "application/json; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{fname}.json"'})
                body = exporters.export_markdown(detail, entries).encode("utf-8")
                return self._send(200, body, "text/markdown; charset=utf-8", {"Content-Disposition": f'attachment; filename="{fname}.md"'})
            return self._static(path)

        def _static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

    return Handler


def serve(reg: Registry, host: str, port: int, allow_remote: bool = False, verbose: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(reg, allow_remote, verbose))
    httpd.daemon_threads = True
    return httpd
