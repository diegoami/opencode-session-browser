"""Strictly read-only SQLite access to an OpenCode database.

* Direct mode: ``file:...?mode=ro`` + ``PRAGMA query_only``. Never writes, never migrates.
* Snapshot mode: used when the database sits on a filesystem where SQLite WAL cannot work
  (WSL's /mnt/c drvfs/9p mount, \\\\wsl.localhost UNC shares, network drives). The db + WAL
  files are *copied* (read-only on the source side) into a private cache directory and the
  copy is queried. The OpenCode files themselves are only ever opened for reading.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

NETWORK_FS = {"9p", "drvfs", "virtiofs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "fuse.drvfs"}


def _sig(p: str | Path) -> tuple[int, int] | None:
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def needs_snapshot(path: str) -> str | None:
    """Return a human-readable reason if direct WAL access is unreliable for ``path``."""
    if os.name == "nt":
        p = str(path)
        if p.startswith("\\\\") and not p.startswith("\\\\?\\"):
            return "UNC/network path (SQLite WAL cannot be used over network shares)"
        return None
    try:
        real = os.path.realpath(path)
        best, fstype = "", ""
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, fstype = mnt, parts[2]
        if fstype in NETWORK_FS:
            return f"filesystem '{fstype}' at {best} does not support SQLite WAL shared memory"
    except OSError:
        pass
    return None


class SqliteAccess:
    def __init__(self, db_path: str, cache_dir: Path, source_id: str, force_snapshot: bool | None = None):
        self.db_path = str(db_path)
        self.wal_path = self.db_path + "-wal"
        self.lock = threading.RLock()
        self.snapshot_reason = None
        if force_snapshot is True:
            self.snapshot_reason = "forced by configuration"
        elif force_snapshot is None:
            self.snapshot_reason = needs_snapshot(self.db_path)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_id)
        self.snap_dir = Path(cache_dir) / "snapshots" / safe
        self._lock_fh = None
        self.snap_db = self.snap_dir / "opencode.db"
        self._state: dict = {}
        self.last_sync_error: str | None = None

    @property
    def mode(self) -> str:
        return "snapshot" if self.snapshot_reason else "direct"

    # -- change detection ----------------------------------------------------------
    def fingerprint(self) -> tuple:
        return (_sig(self.db_path), _sig(self.wal_path))

    # -- snapshot maintenance -------------------------------------------------------
    def _claim_dir(self) -> None:
        """Exclusive ownership of the snapshot dir; a second app instance gets its own private copy."""
        if self._lock_fh is not None:
            return
        for suffix in ("", f"-{os.getpid()}"):
            d = self.snap_dir.with_name(self.snap_dir.name + suffix)
            d.mkdir(parents=True, exist_ok=True)
            fh = open(d / ".lock", "a+")
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                continue
            self._lock_fh = fh
            self.snap_dir = d
            self.snap_db = d / "opencode.db"
            return
        raise OSError("cannot claim a snapshot directory")

    def _state_file(self) -> Path:
        return self.snap_dir / "state.json"

    def _load_state(self) -> None:
        if self._state:
            return
        try:
            self._state = json.loads(self._state_file().read_text())
            if self._state.get("src") != self.db_path or _sig(self.snap_db) is None:
                self._state = {}
            elif _sig(self.snap_db)[0] != self._state.get("db", [None])[0]:
                self._state = {}
        except (OSError, ValueError):
            self._state = {}

    def _copy(self, src: str, dst: Path) -> None:
        tmp = dst.with_name(dst.name + ".part")
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 8 * 1024 * 1024)
        os.replace(tmp, dst)

    def sync(self) -> bool:
        """Bring the private snapshot up to date. Returns True if anything was copied."""
        if self.mode != "snapshot":
            return False
        with self.lock:
            self._claim_dir()
            self._load_state()
            changed = False
            for _ in range(4):
                dsig = _sig(self.db_path)
                if dsig is None:
                    raise FileNotFoundError(self.db_path)
                wsig = _sig(self.wal_path)
                if self._state.get("db") != list(dsig):
                    self._copy(self.db_path, self.snap_db)
                    for suffix in ("-wal", "-shm"):
                        _rm(str(self.snap_db) + suffix)
                    self._state = {"src": self.db_path, "db": list(dsig), "wal": None}
                    changed = True
                if self._state.get("wal") != (list(wsig) if wsig else None):
                    _rm(str(self.snap_db) + "-shm")  # stale wal-index must never outlive its WAL
                    if wsig:
                        self._copy(self.wal_path, Path(str(self.snap_db) + "-wal"))
                    else:
                        _rm(str(self.snap_db) + "-wal")
                    self._state["wal"] = list(wsig) if wsig else None
                    changed = True
                if _sig(self.db_path) == dsig:  # main file unchanged while copying -> consistent
                    break
                self._state = {}
            self._state_file().write_text(json.dumps(self._state))
            self.last_sync_error = None
            return changed

    # -- connections ------------------------------------------------------------------
    def _open(self, path: str) -> sqlite3.Connection:
        uri = Path(path).absolute().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # surfaces I/O / lock errors now
        return conn

    @contextmanager
    def connection(self):
        with self.lock:
            if self.mode == "direct":
                try:
                    conn = self._open(self.db_path)
                except sqlite3.OperationalError as exc:
                    # WAL on an unsupported FS often shows up as "disk I/O error" / "locked".
                    self.snapshot_reason = f"direct read-only open failed ({exc}); using a snapshot copy"
                    conn = None
                if conn is not None:
                    try:
                        yield conn
                    finally:
                        conn.close()
                    return
            if not self.snap_db.exists() or self._state == {}:
                self.sync()
            conn = self._open(str(self.snap_db))
            try:
                yield conn
            finally:
                conn.close()


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
