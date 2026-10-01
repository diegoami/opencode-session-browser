"""OpenCode SQLite storage (opencode.db).

Observed generations (OpenCode 1.18.x):

* legacy:  ``session`` + ``message`` + ``part`` tables (JSON ``data`` columns)
* v2:      ``session_v2`` + ``session_message`` (typed, ``seq``-ordered JSON ``data`` records)
* newer:   ``session`` + ``session_message`` (the v2 message model without a ``session_v2`` table)

One database may contain several generations at once (the Windows store inspected while building
this holds 138 legacy sessions *and* 358 v2 sessions, the v2 table being a superset). This adapter
detects tables/columns at runtime, unions the session tables, and picks the message model
per session. Only an allow-list of tables is ever read (never account/credential tables).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import normalize as N
from ..models import SessionSummary
from ..paths import basename_any
from ..sqlite_access import SqliteAccess
from .base import StorageBackend, UnsupportedStorage

SESSION_COLS = [
    "id", "project_id", "parent_id", "fork_session_id", "slug", "directory", "path", "title", "version", "agent", "model",
    "cost", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write",
    "time_created", "time_updated", "time_archived", "time_idle", "idle_outcome", "share_url",
]
CONVERSATION_TYPES = ("user", "assistant", "system", "synthetic", "compaction")


class SqliteBackend(StorageBackend):
    format_name = "sqlite"

    def __init__(self, access: SqliteAccess):
        self.access = access
        self._schema_cache: dict[str, list[str]] | None = None

    # -- helpers -----------------------------------------------------------------------
    @staticmethod
    def schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if name.startswith("sqlite_"):
                continue
            out[name] = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
        return out

    @staticmethod
    def _select(conn, table: str, cols: list[str], present: list[str], where: str = "", params=()) -> list[dict]:
        use = [c for c in cols if c in present]
        rows = conn.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in use)} FROM "{table}" {where}', params).fetchall()
        return [{c: r[i] for i, c in enumerate(use)} for r in rows]

    def fingerprint(self) -> tuple:
        return self.access.fingerprint()

    def prepare(self) -> bool:
        return self.access.sync()

    # -- diagnostics -------------------------------------------------------------------
    def describe(self) -> dict:
        with self.access.connection() as conn:
            sch = self.schema(conn)
            self._require(sch)
            info: dict[str, Any] = {
                "format": "sqlite",
                "access_mode": self.access.mode,
                "access_reason": self.access.snapshot_reason,
                "tables_present": sorted(t for t in sch if t in (
                    "session", "session_v2", "message", "part", "session_message", "project", "event")),
                "session_models": [],
            }
            v2_rows = conn.execute("SELECT count(*) FROM session_message").fetchone()[0] if "session_message" in sch else 0
            legacy_rows = conn.execute("SELECT count(*) FROM message").fetchone()[0] if "message" in sch else 0
            info["message_rows"] = {"session_message": v2_rows, "message": legacy_rows}
            if "session_v2" in sch:
                info["session_models"].append("v2 (session_v2 + session_message)")
            if "session" in sch:
                info["session_models"].append("session + session_message" if (v2_rows and "session_v2" not in sch)
                                              else "legacy (session + message + part)")
            if "migration" in sch and "id" in sch["migration"]:
                row = conn.execute("SELECT id FROM migration ORDER BY rowid DESC LIMIT 1").fetchone()
                info["latest_migration"] = row[0] if row else None
            versions = {}
            for t in ("session_v2", "session"):
                if t in sch and "version" in sch[t]:
                    for v, n in conn.execute(f'SELECT version, count(*) FROM "{t}" GROUP BY version').fetchall():
                        versions[v] = versions.get(v, 0) + n
            info["recorded_versions"] = dict(sorted(versions.items(), key=lambda kv: -kv[1])[:8])
            return info

    @staticmethod
    def _require(sch: dict) -> None:
        if "session" not in sch and "session_v2" not in sch:
            raise UnsupportedStorage("no session/session_v2 table (tables: " + ", ".join(sorted(sch)[:12]) + ")")

    # -- sessions ----------------------------------------------------------------------
    def list_sessions(self) -> list[SessionSummary]:
        with self.access.connection() as conn:
            sch = self.schema(conn)
            self._require(sch)
            projects = {}
            if "project" in sch:
                for p in self._select(conn, "project", ["id", "worktree", "name"], sch["project"]):
                    projects[p["id"]] = p
            legacy_ids = set()
            rows: dict[str, tuple[dict, str]] = {}
            if "session_v2" in sch:
                for r in self._select(conn, "session_v2", SESSION_COLS, sch["session_v2"]):
                    rows[r["id"]] = (r, "v2")
            if "session" in sch:
                for r in self._select(conn, "session", SESSION_COLS, sch["session"]):
                    legacy_ids.add(r["id"])
                    if r["id"] not in rows:
                        rows[r["id"]] = (r, "legacy")
            v2_counts: dict[str, dict[str, int]] = {}
            uses_v2 = False
            if "session_message" in sch:
                for sid, typ, n in conn.execute("SELECT session_id, type, count(*) FROM session_message GROUP BY session_id, type"):
                    v2_counts.setdefault(sid, {})[typ] = n
                uses_v2 = bool(v2_counts)
            legacy_counts: dict[str, int] = {}
            if "message" in sch:
                for sid, n in conn.execute("SELECT session_id, count(*) FROM message GROUP BY session_id"):
                    legacy_counts[sid] = n
            out = []
            for sid, (r, storage) in rows.items():
                out.append(self._summary(r, storage, projects, sid in legacy_ids, v2_counts.get(sid), legacy_counts.get(sid), uses_v2))
            return out

    def _summary(self, r, storage, projects, in_legacy, v2c, legacy_n, uses_v2=False) -> SessionSummary:
        m = N.norm_model(N.loads(r.get("model")) if isinstance(r.get("model"), str) and r["model"].lstrip().startswith(("{", "[")) else r.get("model"))
        proj = projects.get(r.get("project_id")) or {}
        wt = proj.get("worktree")
        pname = proj.get("name") or (basename_any(wt) if wt and wt not in ("/", "\\") else None)
        if v2c:
            conv = sum(n for t, n in v2c.items() if t in CONVERSATION_TYPES)
            events = sum(v2c.values()) - conv
            storage_kind = storage if storage == "v2" else "v2"
        else:
            conv, events = legacy_n, 0
            storage_kind = "legacy" if legacy_n is not None else ("v2" if storage == "v2" or uses_v2 else "legacy")
        tok = {"input": r.get("tokens_input"), "output": r.get("tokens_output"), "reasoning": r.get("tokens_reasoning"),
               "cache_read": r.get("tokens_cache_read"), "cache_write": r.get("tokens_cache_write")}
        return SessionSummary(
            id=r["id"], title=r.get("title") or "", slug=r.get("slug"), project_id=r.get("project_id"),
            project_name=pname, project_worktree=wt, directory=r.get("directory"), subpath=r.get("path") or None,
            parent_id=r.get("parent_id") or None, fork_of=r.get("fork_session_id") or None,
            created=N.to_ms(r.get("time_created")), updated=N.to_ms(r.get("time_updated")),
            archived_at=N.to_ms(r.get("time_archived")), agent=r.get("agent"), provider=m["provider"], model=m["model"],
            variant=m["variant"], version=r.get("version"), message_count=conv if conv is not None else 0,
            event_count=events, cost=r.get("cost"), tokens={k: v for k, v in tok.items() if v is not None},
            outcome=r.get("idle_outcome"), storage=storage_kind, in_legacy_table=in_legacy,
        )

    # -- messages ----------------------------------------------------------------------
    def message_fingerprint(self, session_id: str) -> tuple | None:
        with self.access.connection() as conn:
            sch = self.schema(conn)
            if "session_message" in sch:
                row = conn.execute("SELECT count(*), max(seq), max(time_updated) FROM session_message WHERE session_id=?", (session_id,)).fetchone()
                if row and row[0]:
                    return ("v2", row[0], row[1], row[2])
            if "message" in sch:
                row = conn.execute("SELECT count(*), max(time_updated) FROM message WHERE session_id=?", (session_id,)).fetchone()
                prow = conn.execute("SELECT count(*), max(time_updated) FROM part WHERE session_id=?", (session_id,)).fetchone() if "part" in sch else (0, 0)
                return ("legacy", row[0], row[1], prow[0], prow[1])
        return None

    def load_messages(self, session_id: str) -> list[dict]:
        with self.access.connection() as conn:
            sch = self.schema(conn)
            if "session_message" in sch:
                rows = conn.execute(
                    "SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=? ORDER BY seq, time_created, id",
                    (session_id,)).fetchall()
                if rows:
                    return [self._v2_row(r) for r in rows]
            if "message" in sch:
                return self._legacy(conn, sch, session_id)
        return []

    @staticmethod
    def _v2_row(r) -> dict:
        data = N.loads(r["data"])
        e = N.parse_v2_message(r["type"], data, r["id"], r["seq"], r["time_created"])
        if data is None:  # malformed JSON: keep the raw text
            e["parts"].append({"type": "unknown", "original_type": "malformed-json", "raw": (r["data"] or "")[:4000]})
        return e

    @staticmethod
    def _legacy(conn, sch, session_id) -> list[dict]:
        msgs = conn.execute("SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id", (session_id,)).fetchall()
        parts: dict[str, list] = {}
        if "part" in sch:
            for r in conn.execute("SELECT message_id, id, data FROM part WHERE session_id=? ORDER BY id", (session_id,)):
                parts.setdefault(r["message_id"], []).append(N.loads(r["data"]) if N.loads(r["data"]) is not None else {"type": "malformed", "text": (r["data"] or "")[:2000]})
        out = []
        for i, m in enumerate(msgs):
            e = N.parse_legacy_message(N.loads(m["data"]), parts.get(m["id"], []), m["id"], m["time_created"])
            e["seq"] = i
            out.append(e)
        return out
