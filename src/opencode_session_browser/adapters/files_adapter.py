"""Pre-SQLite OpenCode storage: ``storage/{session,message,part,project}/**.json``.

Not observed on the machine this was built on (all installs there are SQLite); implemented from
the documented historical layout and covered by synthetic fixtures only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import normalize as N
from ..models import SessionSummary
from ..paths import basename_any
from .base import StorageBackend, UnsupportedStorage


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


class FilesBackend(StorageBackend):
    format_name = "json-files"

    def __init__(self, storage_dir: str | Path):
        self.root = Path(storage_dir)
        self._index: dict[str, Path] = {}

    def fingerprint(self) -> tuple:
        sig = []
        for sub in ("session", "message", "part"):
            d = self.root / sub
            try:
                sig.append((sub, d.stat().st_mtime_ns, sum(1 for _ in os.scandir(d))))
            except OSError:
                sig.append((sub, None, 0))
        return tuple(sig)

    def describe(self) -> dict:
        if not (self.root / "session").is_dir():
            raise UnsupportedStorage(f"{self.root} has no session/ directory")
        return {"format": "json-files", "layout": "storage/{session,message,part,project}/*.json"}

    def list_sessions(self) -> list[SessionSummary]:
        sdir = self.root / "session"
        if not sdir.is_dir():
            raise UnsupportedStorage(f"{self.root} has no session/ directory")
        projects = {}
        pdir = self.root / "project"
        if pdir.is_dir():
            for f in pdir.glob("*.json"):
                j = _read(f)
                if isinstance(j, dict) and j.get("id"):
                    projects[j["id"]] = j
        out, self._index = [], {}
        for proj_dir in sorted(p for p in sdir.iterdir() if p.is_dir()):
            for f in proj_dir.glob("*.json"):
                j = _read(f)
                if not isinstance(j, dict) or not j.get("id"):
                    continue  # partial write / corrupt record: skip this one file only
                self._index[j["id"]] = f
                t = j.get("time") if isinstance(j.get("time"), dict) else {}
                proj = projects.get(j.get("projectID") or proj_dir.name, {})
                wt = proj.get("worktree")
                mdir = self.root / "message" / j["id"]
                try:
                    n = sum(1 for _ in os.scandir(mdir))
                except OSError:
                    n = 0
                out.append(SessionSummary(
                    id=j["id"], title=j.get("title") or "", slug=j.get("slug"), project_id=j.get("projectID") or proj_dir.name,
                    project_name=proj.get("name") or (basename_any(wt) if wt and wt not in ("/", "\\") else None),
                    project_worktree=wt, directory=j.get("directory"), parent_id=j.get("parentID"),
                    created=N.to_ms(t.get("created")), updated=N.to_ms(t.get("updated")),
                    archived_at=N.to_ms(t.get("archived")), version=j.get("version"), message_count=n, storage="files",
                    in_legacy_table=None))
        return out

    def message_fingerprint(self, session_id: str):
        try:
            d = self.root / "message" / session_id
            return ("files", d.stat().st_mtime_ns, sum(1 for _ in os.scandir(d)))
        except OSError:
            return None

    def load_messages(self, session_id: str) -> list[dict]:
        mdir = self.root / "message" / session_id
        if not mdir.is_dir():
            return []
        msgs = []
        for f in mdir.glob("*.json"):
            j = _read(f)
            if isinstance(j, dict):
                msgs.append(j)
        msgs.sort(key=lambda m: ((m.get("time") or {}).get("created") or 0, m.get("id") or ""))
        out = []
        for i, m in enumerate(msgs):
            parts = []
            pdir = self.root / "part" / str(m.get("id"))
            if pdir.is_dir():
                for pf in sorted(pdir.glob("*.json")):
                    pj = _read(pf)
                    if pj is not None:
                        parts.append(pj)
            e = N.parse_legacy_message(m, parts, m.get("id"))
            e["seq"] = i
            out.append(e)
        return out
