"""Directory of ``opencode export`` JSON files (``{info, messages:[{info, parts}]}``).

This is the compatibility format: any OpenCode version can produce it, so it can be used as a
fallback data root (``kind: export-dir``) when a storage format is not otherwise understood.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import normalize as N
from ..models import SessionSummary
from ..paths import basename_any
from .base import StorageBackend, UnsupportedStorage


def parse_export(text: str):
    i = text.find("{")  # the CLI prints an "Exporting session:" banner before the JSON
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


class ExportDirBackend(StorageBackend):
    format_name = "opencode-export"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self._files: dict[str, Path] = {}

    def fingerprint(self) -> tuple:
        try:
            return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in self.dir.glob("*.json")))
        except OSError:
            return ()

    def describe(self) -> dict:
        if not self.dir.is_dir():
            raise UnsupportedStorage(f"{self.dir} is not a directory")
        return {"format": "opencode-export", "layout": "*.json from `opencode export`"}

    def list_sessions(self) -> list[SessionSummary]:
        out, self._files = [], {}
        if not self.dir.is_dir():
            raise UnsupportedStorage(f"{self.dir} is not a directory")
        for f in self.dir.glob("*.json"):
            j = parse_export(f.read_text(encoding="utf-8", errors="replace"))
            info = j.get("info") if isinstance(j, dict) else None
            if not isinstance(info, dict) or not info.get("id"):
                continue
            self._files[info["id"]] = f
            t = info.get("time") if isinstance(info.get("time"), dict) else {}
            out.append(SessionSummary(
                id=info["id"], title=info.get("title") or "", project_id=info.get("projectID"), directory=info.get("directory"),
                project_name=basename_any(info.get("directory")), parent_id=info.get("parentID"), version=info.get("version"),
                created=N.to_ms(t.get("created")), updated=N.to_ms(t.get("updated")),
                message_count=len(j.get("messages") or []), storage="export"))
        return out

    def load_messages(self, session_id: str) -> list[dict]:
        f = self._files.get(session_id)
        if not f:
            return []
        j = parse_export(f.read_text(encoding="utf-8", errors="replace")) or {}
        out = []
        for i, m in enumerate(j.get("messages") or []):
            e = N.parse_legacy_message(m.get("info"), m.get("parts") or [])
            e["seq"] = i
            out.append(e)
        return out
