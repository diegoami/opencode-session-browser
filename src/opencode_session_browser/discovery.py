"""Find OpenCode data roots on this machine: native, Windows (from WSL), WSL distros (from Windows).

Nothing is hard-coded (user names, distros, home directories): everything is enumerated.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, slug
from .paths import is_windows, is_wsl, wsl_distro_name
from .source import SourceSpec

SKIP_DISTROS = re.compile(r"^(docker-desktop|docker-desktop-data|rancher-desktop.*)$", re.I)
SKIP_USERS = {"all users", "default", "default user", "public", "desktop.ini", "defaultuser0"}


@dataclass
class Distro:
    name: str
    state: str = "unknown"
    version: str | None = None
    default: bool = False
    roots: list[str] = field(default_factory=list)
    note: str | None = None


def has_data(d: str | Path) -> bool:
    """A directory counts as an OpenCode data root if it holds a db or a legacy storage tree."""
    try:
        p = Path(d)
        if not p.is_dir():
            return False
        return any(p.glob("opencode*.db")) or (p / "storage" / "session").is_dir()
    except OSError:
        return False


def _decode_wsl(b: bytes) -> str:
    if b[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(b) > 1 and b[1:2] == b"\x00"):
        return b.decode("utf-16", errors="replace").lstrip("﻿")
    return b.decode("utf-8", errors="replace")


def list_wsl_distros(timeout: float = 10.0) -> tuple[list[Distro], str | None]:
    """Installed distributions via ``wsl.exe -l -v`` (works from Windows and from inside WSL)."""
    exe = "wsl.exe"
    try:
        r = subprocess.run([exe, "-l", "-v"], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return [], "wsl.exe not found"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"wsl.exe failed: {exc}"
    text = _decode_wsl(r.stdout)
    out: list[Distro] = []
    for line in text.splitlines()[1:]:
        line = line.rstrip()
        if not line.strip():
            continue
        default = line.lstrip().startswith("*")
        parts = line.replace("*", " ", 1).split()
        if len(parts) >= 3:
            out.append(Distro(name=" ".join(parts[:-2]), state=parts[-2], version=parts[-1], default=default))
    if not out and r.returncode != 0:
        return [], f"wsl.exe -l -v exit {r.returncode}: {text.strip()[:200]}"
    return out, None


def _roots_under_home(home: Path) -> list[str]:
    return [str(c) for c in (home / ".local" / "share" / "opencode",) if has_data(c)]


def _windows_profile_candidates(profile: Path) -> list[str]:
    cands = [profile / ".local" / "share" / "opencode", profile / "AppData" / "Local" / "opencode",
             profile / "AppData" / "Roaming" / "opencode"]
    return [str(c) for c in cands if has_data(c)]


def discover_native_windows() -> list[SourceSpec]:
    out: list[SourceSpec] = []
    seen: set[str] = set()
    cands = []
    if os.environ.get("XDG_DATA_HOME"):
        cands.append(Path(os.environ["XDG_DATA_HOME"]) / "opencode")
    home = Path.home()
    cands += [home / ".local" / "share" / "opencode"]
    for var in ("LOCALAPPDATA", "APPDATA"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]) / "opencode")
    for c in cands:
        k = os.path.normcase(str(c))
        if k not in seen and has_data(c):
            seen.add(k)
            out.append(SourceSpec(id="windows" if not out else f"windows-{len(out)+1}", label="Windows" if not out else f"Windows ({c})",
                                  env="windows", root=str(c)))
    return out


def discover_native_posix() -> list[SourceSpec]:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode"
    if not has_data(base):
        return []
    if is_wsl():
        d = wsl_distro_name() or "WSL"
        return [SourceSpec(id=f"wsl-{slug(d)}", label=f"WSL: {d}", env="wsl", root=str(base), distro=d)]
    return [SourceSpec(id="linux", label="Linux", env="linux", root=str(base))]


def discover_windows_from_wsl() -> list[SourceSpec]:
    found: list[tuple[str, str]] = []
    for drive in sorted(Path("/mnt").glob("[a-z]")):
        users = drive / "Users"
        try:
            entries = [u for u in users.iterdir() if u.is_dir() and u.name.lower() not in SKIP_USERS]
        except OSError:
            continue
        for u in entries:
            for r in _windows_profile_candidates(u):
                found.append((u.name, r))
    out = []
    for i, (user, root) in enumerate(found):
        multi = len({u for u, _ in found}) > 1 or sum(1 for u, _ in found if u == user) > 1
        label = "Windows" + (f" ({user})" if multi else "")
        sid = "windows" + (f"-{slug(user)}" if multi else "") + (f"-{i+1}" if sum(1 for u, _ in found if u == user) > 1 else "")
        out.append(SourceSpec(id=sid, label=label, env="windows", root=root,
                              note="read through /mnt drive mount -> snapshot copy (WAL cannot be mapped over drvfs/9p)"))
    return out


def discover_wsl_from_windows(wake: bool) -> tuple[list[SourceSpec], list[Distro], str | None]:
    distros, err = list_wsl_distros()
    specs: list[SourceSpec] = []
    for d in distros:
        if SKIP_DISTROS.match(d.name):
            d.note = "system distribution (skipped)"
            continue
        if d.state.lower() != "running" and not wake:
            d.note = "stopped; not started (wake_wsl is disabled)"
            continue
        homes: list[Path] = []
        for prefix in (f"\\\\wsl.localhost\\{d.name}", f"\\\\wsl$\\{d.name}"):
            try:
                hdir = Path(prefix + "\\home")
                if hdir.is_dir() or Path(prefix + "\\root").is_dir():
                    homes = [p for p in hdir.iterdir() if p.is_dir()] if hdir.is_dir() else []
                    homes.append(Path(prefix + "\\root"))
                    break
            except OSError as exc:
                d.note = f"cannot access distro filesystem: {exc}"
        if not homes and not d.note:
            d.note = "distro filesystem not reachable"
        users = []
        for h in homes:
            for r in _roots_under_home(h):
                users.append((h.name, r))
        for user, r in users:
            multi = len(users) > 1
            label = f"WSL: {d.name}" + (f" ({user})" if multi else "")
            sid = f"wsl-{slug(d.name)}" + (f"-{slug(user)}" if multi else "")
            specs.append(SourceSpec(id=sid, label=label, env="wsl", root=r, distro=d.name,
                                    note="read through \\\\wsl.localhost share -> snapshot copy"))
            d.roots.append(r)
        if not users and not d.note:
            d.note = "no OpenCode data directory found under /home/* or /root"
    return specs, distros, err


@dataclass
class Discovery:
    specs: list[SourceSpec]
    distros: list[Distro]
    distro_error: str | None = None


def discover(cfg: Config) -> Discovery:
    specs: list[SourceSpec] = []
    distros: list[Distro] = []
    derr = None
    if cfg.auto_discovery:
        if is_windows():
            if cfg.discover_windows:
                specs += discover_native_windows()
            if cfg.discover_wsl:
                s, distros, derr = discover_wsl_from_windows(cfg.wake_wsl)
                specs += s
        else:
            specs += discover_native_posix()
            if cfg.discover_windows and is_wsl():
                specs += discover_windows_from_wsl()
            if is_wsl():
                distros, derr = list_wsl_distros()
                here = wsl_distro_name()
                for d in distros:
                    if d.name == here:
                        d.roots = [s.root for s in specs if s.env == "wsl"]
                    elif not SKIP_DISTROS.match(d.name):
                        d.note = "other distribution: launch the browser from native Windows to aggregate it (or add a manual source)"
    for m in cfg.sources:
        sid = slug(m.name)
        specs.append(SourceSpec(id=sid, label=m.name, env="custom", root=m.path, kind=m.kind,
                                status_url=m.status_url or cfg.status_urls.get(m.name), force_snapshot=m.force_snapshot, origin="config"))
    # unique ids
    used: dict[str, int] = {}
    for s in specs:
        n = used.get(s.id, 0)
        used[s.id] = n + 1
        if n:
            s.id = f"{s.id}-{n+1}"
        if not s.status_url:
            s.status_url = cfg.status_urls.get(s.id) or cfg.status_urls.get(s.label)
    return Discovery(specs, distros, derr)
