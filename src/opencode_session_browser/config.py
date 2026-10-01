"""Configuration: optional JSON file + command-line overrides. Contains no secrets by default."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .paths import user_config_dir


@dataclass
class ManualSource:
    name: str
    path: str
    kind: str = "auto"          # auto | sqlite | files | export-dir
    status_url: str | None = None
    force_snapshot: bool | None = None


@dataclass
class Config:
    sources: list[ManualSource] = field(default_factory=list)
    auto_discovery: bool = True
    discover_windows: bool = True
    discover_wsl: bool = True
    wake_wsl: bool = True
    status_urls: dict[str, str] = field(default_factory=dict)   # source id/name -> opencode server URL
    status_password_env: str = "OPENCODE_SERVER_PASSWORD"
    poll_seconds: float = 5.0
    cache_dir: str | None = None
    opencode_cmd: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765


def default_config_path() -> Path:
    env = os.environ.get("OPENCODE_SESSION_BROWSER_CONFIG")
    return Path(env) if env else user_config_dir() / "config.json"


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else default_config_path()
    cfg = Config()
    if not p.is_file():
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read config {p}: {exc}")
    for s in raw.get("sources", []):
        if isinstance(s, dict) and s.get("path"):
            cfg.sources.append(ManualSource(
                name=s.get("name") or Path(s["path"]).name, path=s["path"], kind=s.get("kind", "auto"),
                status_url=s.get("status_url"), force_snapshot=s.get("force_snapshot")))
    for k in ("auto_discovery", "discover_windows", "discover_wsl", "wake_wsl", "poll_seconds", "cache_dir",
              "opencode_cmd", "host", "port", "status_password_env"):
        if k in raw:
            setattr(cfg, k, raw[k])
    cfg.status_urls = dict(raw.get("status_urls", {}))
    return cfg


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "source"
