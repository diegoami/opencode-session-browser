"""Windows / Linux / WSL path helpers. Nothing here touches OpenCode data."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows() -> bool:
    return os.name == "nt"


def is_wsl() -> bool:
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def wsl_distro_name() -> str | None:
    return os.environ.get("WSL_DISTRO_NAME") if is_wsl() else None


def looks_like_windows_path(p: str | None) -> bool:
    return bool(p) and (bool(_WIN_DRIVE.match(p)) or p.startswith("\\\\") or p.startswith("//wsl"))


def path_kind(p: str | None) -> str:
    """'windows' | 'posix' | 'unknown' based on the *text* of a stored path."""
    if not p:
        return "unknown"
    if looks_like_windows_path(p):
        return "windows"
    if p.startswith("/"):
        return "posix"
    return "unknown"


def basename_any(p: str | None) -> str:
    """Last path component for either separator style (stored paths come from either OS)."""
    if not p:
        return ""
    trimmed = p.rstrip("\\/")
    if not trimmed:
        return p  # "/" or "C:\\" style root
    return re.split(r"[\\/]", trimmed)[-1]


def normalize_for_compare(p: str | None) -> str:
    """Case/separator-insensitive key for Windows paths, exact for POSIX."""
    if not p:
        return ""
    if path_kind(p) == "windows":
        return p.replace("\\", "/").rstrip("/").lower()
    return p.rstrip("/") or "/"


def windows_to_wsl(p: str) -> str | None:
    """C:\\Users\\x -> /mnt/c/Users/x (as seen from inside WSL)."""
    m = _WIN_DRIVE.match(p)
    if not m:
        return None
    rest = p[3:].replace("\\", "/")
    return f"/mnt/{p[0].lower()}/{rest}".rstrip("/") or f"/mnt/{p[0].lower()}"


def wsl_to_unc(distro: str, p: str) -> str:
    return "\\\\wsl.localhost\\" + distro + p.replace("/", "\\")


_UNC_WSL = re.compile(r"^[\\/]{2}wsl(?:\.localhost|\$)[\\/]([^\\/]+)(.*)$", re.I)


def unc_wsl_parts(p: str) -> tuple[str, str] | None:
    """\\wsl.localhost\\Ubuntu\\home\\x -> ("Ubuntu", "/home/x")."""
    m = _UNC_WSL.match(p or "")
    return (m.group(1), m.group(2).replace("\\", "/") or "/") if m else None


def local_equivalent(p: str | None) -> str | None:
    """Best-effort path usable by *this* process for an arbitrary stored path, else None."""
    if not p:
        return None
    kind = path_kind(p)
    unc = unc_wsl_parts(p)
    if unc:
        if is_wsl() and unc[0].lower() == (wsl_distro_name() or "").lower():
            return unc[1]
        return p if is_windows() else None
    if kind == "windows":
        if is_windows():
            return p
        if is_wsl():
            return windows_to_wsl(p)
        return None
    if kind == "posix":
        return None if is_windows() else p
    return None


def directory_exists(p: str | None) -> bool | None:
    """True/False when we can tell, None when the path belongs to another environment."""
    local = local_equivalent(p)
    if local is None:
        return None
    try:
        return os.path.isdir(local)
    except OSError:
        return None


def user_cache_dir() -> Path:
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "opencode-session-browser" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "opencode-session-browser"


def user_config_dir() -> Path:
    if is_windows():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "opencode-session-browser"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode-session-browser"


def shell_quote_posix(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def shell_quote_ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


if __name__ == "__main__":  # pragma: no cover
    print(sys.platform, is_wsl())
