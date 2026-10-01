from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

from . import __version__
from .config import Config, ManualSource, load_config, slug
from .discovery import discover
from .registry import Registry
from .server import serve
from .source import SourceSpec


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="opencode-session-browser",
                                 description="Read-only browser for every OpenCode session on Windows and WSL.")
    ap.add_argument("--host", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, help="port (default 8765)")
    ap.add_argument("--source", action="append", default=[], metavar="NAME=PATH",
                    help="extra OpenCode data root (dir containing opencode.db, a .db file, a storage/ tree, or an export dir)")
    ap.add_argument("--kind", action="append", default=[], metavar="NAME=KIND", help="force kind for a --source: auto|sqlite|files|export-dir")
    ap.add_argument("--status-url", action="append", default=[], metavar="SOURCE=URL",
                    help="URL of a running `opencode serve` for authoritative live status of that source")
    ap.add_argument("--config", help="config file (default: per-user config dir)")
    ap.add_argument("--no-auto", action="store_true", help="disable automatic discovery (use only --source / config)")
    ap.add_argument("--no-windows", action="store_true", help="do not look for Windows stores when running in WSL")
    ap.add_argument("--no-wsl", action="store_true", help="do not look for WSL distributions when running on Windows")
    ap.add_argument("--no-wake-wsl", action="store_true", help="do not touch stopped WSL distributions (accessing them starts them)")
    ap.add_argument("--cache-dir", help="where snapshots and the search index live")
    ap.add_argument("--open", action="store_true", help="open the browser")
    ap.add_argument("--discover", action="store_true", help="print discovered sources with session counts as JSON and exit")
    ap.add_argument("--allow-remote", action="store_true", help="accept non-localhost Host headers (use with care)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    return ap.parse_args(argv)


def build_config(a) -> Config:
    cfg = load_config(a.config)
    kinds = dict(k.split("=", 1) for k in a.kind if "=" in k)
    for s in a.source:
        if "=" in s:
            name, path = s.split("=", 1)
        else:
            name, path = s, s
        cfg.sources.append(ManualSource(name=name, path=path, kind=kinds.get(name, "auto")))
    for s in a.status_url:
        if "=" in s:
            k, v = s.split("=", 1)
            cfg.status_urls[k] = v
    if a.host:
        cfg.host = a.host
    if a.port:
        cfg.port = a.port
    if a.no_auto:
        cfg.auto_discovery = False
    if a.no_windows:
        cfg.discover_windows = False
    if a.no_wsl:
        cfg.discover_wsl = False
    if a.no_wake_wsl:
        cfg.wake_wsl = False
    if a.cache_dir:
        cfg.cache_dir = a.cache_dir
    return cfg


def main(argv=None) -> int:
    a = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = build_config(a)
    reg = Registry(cfg)
    reg.rediscover()
    if a.discover:
        reg.refresh_all(force=True)
        print(json.dumps(reg.diagnostics(), indent=2, default=str))
        return 0
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not a.allow_remote:
        print(f"Refusing to bind to {cfg.host}: this tool exposes private conversations. Use --allow-remote to override.", file=sys.stderr)
        return 2
    print(f"opencode-session-browser {__version__}: {len(reg.sources)} source(s): " + (", ".join(s.spec.label for s in reg.sources.values()) or "none"))
    reg.start()
    httpd = serve(reg, cfg.host, cfg.port, allow_remote=a.allow_remote, verbose=a.verbose)
    url = f"http://{'127.0.0.1' if cfg.host == 'localhost' else cfg.host}:{cfg.port}/"
    print(f"Listening on {url}  (read-only; Ctrl+C to stop)")
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reg.stop()
        httpd.server_close()
    return 0
