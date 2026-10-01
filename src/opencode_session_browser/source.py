"""A Source = one OpenCode data root (Windows profile, WSL distro home, or a manual path)."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import ExportDirBackend, FilesBackend, SqliteBackend, StorageBackend, UnsupportedStorage
from .models import SessionSummary
from .sqlite_access import SqliteAccess


@dataclass
class SourceSpec:
    id: str
    label: str
    env: str                      # windows | wsl | linux | custom
    root: str                     # directory containing opencode.db (or storage/), or a .db file
    kind: str = "auto"
    distro: str | None = None
    status_url: str | None = None
    force_snapshot: bool | None = None
    origin: str = "auto"          # auto | config | cli
    note: str | None = None


def build_backends(spec: SourceSpec, cache_dir: Path) -> list[StorageBackend]:
    root = Path(spec.root)
    out: list[StorageBackend] = []
    if spec.kind in ("auto", "sqlite"):
        dbs = [root] if root.suffix == ".db" else sorted(root.glob("opencode*.db")) if root.is_dir() else []
        # opencode.db first, then channel DBs (opencode-<channel>.db)
        dbs.sort(key=lambda p: (p.name != "opencode.db", p.name))
        for db in dbs:
            sid = spec.id if db.name == "opencode.db" else f"{spec.id}-{db.stem}"
            out.append(SqliteBackend(SqliteAccess(str(db), cache_dir, sid, spec.force_snapshot)))
    if spec.kind in ("auto", "files") and root.is_dir():
        storage = root / "storage" if (root / "storage" / "session").is_dir() else root if (root / "session").is_dir() else None
        if storage:
            out.append(FilesBackend(storage))
    if spec.kind == "export-dir":
        out.append(ExportDirBackend(root))
    return out


class Source:
    def __init__(self, spec: SourceSpec, cache_dir: Path):
        self.spec = spec
        self.cache_dir = cache_dir
        self.lock = threading.RLock()
        self.backends: list[StorageBackend] = []
        self.sessions: dict[str, SessionSummary] = {}
        self.owner: dict[str, StorageBackend] = {}
        self.status = "pending"        # pending | ok | degraded | error | unavailable | unsupported
        self.error: str | None = None
        self.backend_errors: list[str] = []
        self.last_refresh: float | None = None
        self.last_attempt: float | None = None
        self.generation = 0
        self.details: list[dict] = []
        self.versions: dict = {}
        self._fp: tuple | None = None

    @property
    def id(self) -> str:
        return self.spec.id

    def _unavailable(self, msg: str, status: str = "unavailable") -> None:
        self.status, self.error = status, msg

    def refresh(self, force: bool = False) -> bool:
        """Re-read session metadata if storage changed. Never raises; failures are recorded."""
        with self.lock:
            self.last_attempt = time.time()
            try:
                if not self.backends or self.status in ("unavailable", "error", "unsupported", "pending"):
                    self.backends = build_backends(self.spec, self.cache_dir)
                if not os.path.exists(self.spec.root):
                    self._unavailable(f"path not found: {self.spec.root}")
                    return False
                if not self.backends:
                    self._unavailable("no OpenCode database (opencode*.db) or storage/ directory under this path", "unsupported")
                    return False
                try:
                    fp = tuple(b.fingerprint() for b in self.backends)
                except OSError as exc:
                    self._unavailable(f"cannot stat storage: {exc}", "error")
                    return False
                if not force and fp == self._fp and self.status in ("ok", "degraded"):
                    return False
                sessions: dict[str, SessionSummary] = {}
                owner: dict[str, StorageBackend] = {}
                errors, details = [], []
                for b in self.backends:
                    try:
                        b.prepare()
                        details.append(b.describe())
                        for s in b.list_sessions():
                            if s.id not in sessions:       # same id in two backends of one root: first wins
                                sessions[s.id] = s
                                owner[s.id] = b
                    except UnsupportedStorage as exc:
                        errors.append(f"{b.format_name}: unsupported: {exc}")
                    except Exception as exc:  # noqa: BLE001 - isolate each backend
                        errors.append(f"{b.format_name}: {type(exc).__name__}: {exc}")
                self.backend_errors = errors
                self.details = details
                if errors and not details:
                    self.status = "error"
                    self.error = "; ".join(errors)
                    return False          # keep previously loaded sessions (stale but better than none)
                self.status = "degraded" if errors else "ok"
                self.error = "; ".join(errors) or None
                for s in sessions.values():
                    s.source_id = self.id
                kids: dict[str, int] = {}
                for s in sessions.values():
                    if s.parent_id:
                        kids[s.parent_id] = kids.get(s.parent_id, 0) + 1
                for sid, s in sessions.items():
                    s.child_count = kids.get(sid, 0)
                    old = self.sessions.get(sid)
                    if old:     # keep index-derived fields until the index catches up
                        s.error_count, s.tool_count, s.models = old.error_count, old.tool_count, old.models
                self.sessions, self.owner = sessions, owner
                self._fp = fp
                self.last_refresh = time.time()
                self.generation += 1
                vs: dict[str, int] = {}
                for s in sessions.values():
                    if s.version:
                        vs[s.version] = vs.get(s.version, 0) + 1
                self.versions = vs
                return True
            except Exception as exc:  # noqa: BLE001
                self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
                return False

    def load_messages(self, session_id: str) -> list[dict]:
        b = self.owner.get(session_id)
        if b is None:
            raise KeyError(session_id)
        return b.load_messages(session_id)

    def message_fingerprint(self, session_id: str):
        b = self.owner.get(session_id)
        return b.message_fingerprint(session_id) if b else None

    def access_mode(self) -> str | None:
        modes = {getattr(getattr(b, "access", None), "mode", None) for b in self.backends}
        modes.discard(None)
        return ",".join(sorted(modes)) or None
