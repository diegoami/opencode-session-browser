"""Authoritative live status from a running OpenCode server (``GET /session/status``).

Only used when a source has a ``status_url`` configured. Otherwise status is reported as
``unknown`` -- we never infer "running" from timestamps or processes.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request

_CACHE: dict[str, tuple[float, dict | None, str | None]] = {}


def fetch_status(url: str, password_env: str = "OPENCODE_SERVER_PASSWORD", ttl: float = 3.0) -> tuple[dict | None, str | None]:
    """Returns ({session_id: 'busy'|'idle'|'retry'|...}, error)."""
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    req = urllib.request.Request(url.rstrip("/") + "/session/status")
    pw = os.environ.get(password_env)
    if pw:
        tok = base64.b64encode(f"{os.environ.get('OPENCODE_SERVER_USERNAME', 'opencode')}:{pw}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=2.5) as r:
            raw = json.loads(r.read(2_000_000))
        out = {}
        for sid, v in (raw.items() if isinstance(raw, dict) else []):
            out[sid] = (v.get("type") if isinstance(v, dict) else str(v)) or "unknown"
        res = (out, None)
    except Exception as exc:  # noqa: BLE001 - any failure just means "unknown"
        res = (None, f"{type(exc).__name__}: {exc}")
    _CACHE[url] = (now, res[0], res[1])
    return res
