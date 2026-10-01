"""Private search index + per-session statistics.

Stored in the application's own cache directory (never inside OpenCode's data). Built in the
background from the adapters' normalised transcripts; sessions are re-indexed only when their
fingerprint changes. Search is case-insensitive substring matching (Unicode-aware via Python
lower-casing at index time), AND across words.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 2
DOC_CAP = 6000
KINDS = ("user", "assistant", "system", "tool", "output", "error")


def _like(term: str) -> str:
    return "%" + term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def parse_query(q: str) -> list[str]:
    return [(a or b).strip() for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "") if (a or b).strip()]


def docs_for(entries: list[dict]) -> list[tuple]:
    docs = []
    for e in entries:
        mid, k = e["id"], e["kind"]
        for p in e["parts"]:
            t = p["type"]
            if t == "text" and p.get("text"):
                kind = {"user": "user", "assistant": "assistant"}.get(k, "system")
                docs.append((mid, kind, None, p["text"][:DOC_CAP]))
            elif t == "tool":
                tool = p["tool"]
                inp = tool.get("command") or (json.dumps(tool["input"], ensure_ascii=False) if tool.get("input") else "")
                docs.append((mid, "tool", tool["name"], (tool["name"] + "\n" + inp)[:DOC_CAP]))
                if tool.get("output"):
                    docs.append((mid, "output", tool["name"], tool["output"][:DOC_CAP]))
                if tool.get("error"):
                    docs.append((mid, "error", tool["name"], tool["error"][:DOC_CAP]))
            elif t == "error" and p.get("error"):
                docs.append((mid, "error", None, p["error"][:DOC_CAP]))
            elif t == "event" and p.get("text"):
                docs.append((mid, "system", None, p["text"]))
    return docs


class SearchIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._wlock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=OFF")
            self._local.c = c
        return c

    def _init(self) -> None:
        c = self._conn()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver != SCHEMA_VERSION:
            c.executescript("DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS idx_sessions;")
            c.executescript(f"""
                CREATE TABLE docs(source TEXT, session TEXT, mid TEXT, kind TEXT, tool TEXT, text TEXT, lc TEXT);
                CREATE INDEX docs_s ON docs(source, session);
                CREATE TABLE idx_sessions(source TEXT, session TEXT, fp TEXT, errors INT, tools INT, models TEXT, tool_names TEXT,
                                          PRIMARY KEY(source, session));
                PRAGMA user_version={SCHEMA_VERSION};""")
            c.commit()

    # -- writing -----------------------------------------------------------------------
    def fingerprint_of(self, source: str, session: str) -> str | None:
        r = self._conn().execute("SELECT fp FROM idx_sessions WHERE source=? AND session=?", (source, session)).fetchone()
        return r[0] if r else None

    def put(self, source: str, session: str, fp: str, entries: list[dict], stats: dict) -> None:
        rows = [(source, session, mid, kind, tool, text, text.lower()) for mid, kind, tool, text in docs_for(entries)]
        with self._wlock:
            c = self._conn()
            c.execute("DELETE FROM docs WHERE source=? AND session=?", (source, session))
            c.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO idx_sessions VALUES (?,?,?,?,?,?,?)",
                      (source, session, fp, stats["error_count"], stats["tool_count"], json.dumps(stats["models"]),
                       json.dumps(stats["tools"])))
            c.commit()

    def prune(self, source: str, keep: set[str]) -> None:
        with self._wlock:
            c = self._conn()
            have = {r[0] for r in c.execute("SELECT session FROM idx_sessions WHERE source=?", (source,))}
            for sid in have - keep:
                c.execute("DELETE FROM docs WHERE source=? AND session=?", (source, sid))
                c.execute("DELETE FROM idx_sessions WHERE source=? AND session=?", (source, sid))
            c.commit()

    # -- reading -----------------------------------------------------------------------
    def stats(self, source: str) -> dict[str, dict]:
        out = {}
        for r in self._conn().execute("SELECT * FROM idx_sessions WHERE source=?", (source,)):
            out[r["session"]] = {"error_count": r["errors"], "tool_count": r["tools"], "models": json.loads(r["models"] or "[]"),
                                 "tools": json.loads(r["tool_names"] or "{}")}
        return out

    def indexed_count(self, source: str) -> int:
        return self._conn().execute("SELECT count(*) FROM idx_sessions WHERE source=?", (source,)).fetchone()[0]

    def matching_sessions(self, term: str, kinds: list[str] | None) -> set[tuple[str, str]]:
        sql = "SELECT DISTINCT source, session FROM docs WHERE lc LIKE ? ESCAPE '\\'"
        params: list = [_like(term)]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params += kinds
        return {(r[0], r[1]) for r in self._conn().execute(sql, params)}

    def snippets(self, source: str, session: str, term: str, kinds: list[str] | None, limit: int = 3) -> list[dict]:
        sql = "SELECT mid, kind, tool, text FROM docs WHERE source=? AND session=? AND lc LIKE ? ESCAPE '\\'"
        params: list = [source, session, _like(term)]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params += kinds
        out = []
        for r in self._conn().execute(sql + " LIMIT ?", params + [limit]):
            text = r["text"]
            i = text.lower().find(term.lower())
            i = max(i, 0)
            a, b = max(0, i - 60), min(len(text), i + len(term) + 100)
            out.append({"message_id": r["mid"], "kind": r["kind"], "tool": r["tool"],
                        "snippet": ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else ""),
                        "start": i - a, "length": len(term)})
        return out
