"""Synthetic OpenCode storage builders. No real conversations are used anywhere."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

T0 = 1_790_000_000_000

V2_SESSION = """CREATE TABLE session_v2 (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text,
 fork_session_id text, fork_boundary text, slug text NOT NULL, directory text NOT NULL, path text, title text, version text NOT NULL,
 share_url text, summary_additions integer, summary_deletions integer, summary_files integer, summary_diffs text, metadata text,
 cost real DEFAULT 0 NOT NULL, tokens_input integer DEFAULT 0 NOT NULL, tokens_output integer DEFAULT 0 NOT NULL,
 tokens_reasoning integer DEFAULT 0 NOT NULL, tokens_cache_read integer DEFAULT 0 NOT NULL, tokens_cache_write integer DEFAULT 0 NOT NULL,
 revert text, permission text, agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL,
 time_compacting integer, time_archived integer, time_suspended integer, resume_attempts integer DEFAULT 0, time_idle integer,
 time_viewed integer, idle_outcome text)"""
LEGACY_SESSION = """CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text, slug text NOT NULL,
 directory text NOT NULL, path text, title text NOT NULL, version text NOT NULL, share_url text, summary_additions integer,
 summary_deletions integer, summary_files integer, summary_diffs text, metadata text, cost real DEFAULT 0 NOT NULL,
 tokens_input integer DEFAULT 0 NOT NULL, tokens_output integer DEFAULT 0 NOT NULL, tokens_reasoning integer DEFAULT 0 NOT NULL,
 tokens_cache_read integer DEFAULT 0 NOT NULL, tokens_cache_write integer DEFAULT 0 NOT NULL, revert text, permission text,
 agent text, model text, time_created integer NOT NULL, time_updated integer NOT NULL, time_compacting integer, time_archived integer)"""
COMMON = """
CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text, time_created integer, time_updated integer);
CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
CREATE TABLE credential (id text PRIMARY KEY, value text NOT NULL);
CREATE TABLE migration (id TEXT PRIMARY KEY, time_completed INTEGER NOT NULL);
"""


def j(o) -> str:
    return json.dumps(o, ensure_ascii=False)


def new_db(path: Path, generation: str = "v2", wal: bool = False) -> sqlite3.Connection:
    """generation: v2 (session_v2) | legacy (session only) | both"""
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
    c.executescript(COMMON)
    if generation in ("v2", "both"):
        c.execute(V2_SESSION)
    if generation in ("legacy", "both"):
        c.execute(LEGACY_SESSION)
    c.execute("INSERT INTO migration VALUES ('20260101_fixture', 1)")
    c.execute("INSERT INTO credential VALUES ('c1', 'SUPER-SECRET-TOKEN-VALUE')")
    c.execute("INSERT INTO project VALUES ('p1', '/home/u/repo', 'git', NULL, 1, 1)")
    c.execute("INSERT INTO project VALUES ('global', '/', NULL, NULL, 1, 1)")
    c.commit()
    return c


def add_session(c, sid, *, table="session_v2", project="p1", parent=None, title="A title", directory="/home/u/repo", created=T0,
                updated=None, model=None, archived=None, outcome=None, version="1.18.34", agent="build", cost=0.0):
    cols = ["id", "project_id", "parent_id", "slug", "directory", "title", "version", "agent", "model", "time_created",
            "time_updated", "time_archived", "cost"]
    vals = [sid, project, parent, "slug-" + sid[-4:], directory, title, version, agent,
            j(model) if model else None, created, updated or created, archived, cost]
    if table == "session_v2":
        cols.append("idle_outcome")
        vals.append(outcome)
    c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    c.commit()


def add_v2(c, sid, seq, mtype, data, t=None, mid=None):
    c.execute("INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) VALUES (?,?,?,?,?,?,?)",
              (mid or f"msg_{sid}_{seq:03d}", sid, mtype, seq, t or T0 + seq * 1000, t or T0 + seq * 1000,
               data if isinstance(data, str) else j(data)))
    c.commit()


def v2_assistant(text="hello", tools=(), error=None, model=("opencode", "m1")):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    content += list(tools)
    d = {"time": {"created": T0, "completed": T0 + 2500}, "agent": "build",
         "model": {"id": model[1], "providerID": model[0], "variant": "high"}, "content": content,
         "finish": "stop", "cost": 0.01, "tokens": {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 7, "write": 1}}}
    if error:
        d["error"] = error
    return d


def v2_tool(name="shell", cmd="ls -la", output="file1\nfile2", status="completed", error=None, call="call_1", meta=None):
    st = {"status": status, "input": {"command": cmd} if cmd is not None else {"filePath": "/x"}, "content": [{"type": "text", "text": output}]}
    if meta:
        st["metadata"] = meta
    if error:
        st["error"] = error
    return {"type": "tool", "id": call, "name": name, "state": st, "time": {"created": T0, "ran": T0 + 100, "completed": T0 + 350}}


def legacy_message(c, sid, mid, role, t, parts=(), **info):
    d = {"role": role, "time": {"created": t}, **info}
    c.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, t, t, j(d)))
    for i, p in enumerate(parts):
        c.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (f"{mid}_p{i:02d}", mid, sid, t, t, j(p) if not isinstance(p, str) else p))
    c.commit()


def make_standard(path: Path) -> Path:
    """A v2 store with a parent -> child -> grandchild chain, tool calls, errors and Unicode."""
    c = new_db(path, "v2")
    add_session(c, "ses_root0001", title="Main review ✔ ünïcode 日本語", updated=T0 + 50_000, model={"id": "m1", "providerID": "opencode"}, outcome="succeeded", cost=0.5)
    add_session(c, "ses_child001", parent="ses_root0001", title="Explore repository", created=T0 + 1000, updated=T0 + 20_000)
    add_session(c, "ses_child002", parent="ses_root0001", title="Security review", created=T0 + 2000, updated=T0 + 30_000, outcome="failed")
    add_session(c, "ses_grand001", parent="ses_child002", title="Inspect dependency", created=T0 + 3000, updated=T0 + 40_000)
    add_session(c, "ses_global01", project="global", title="Global thing", directory="C:\\gone\\away", updated=T0 + 5000, archived=T0 + 6000)
    add_v2(c, "ses_root0001", 1, "user", {"text": "Please review the repo — ünïcode ✔ 日本語", "time": {"created": T0 + 1}})
    add_v2(c, "ses_root0001", 3, "assistant", v2_assistant("second (written out of order)"), t=T0 + 10)
    add_v2(c, "ses_root0001", 2, "assistant", v2_assistant("first reply with `code`", tools=[
        {"type": "reasoning", "text": "thinking hard"},
        v2_tool(cmd="grep -rn needle_in_haystack .", output="a.py:1:needle_in_haystack"),
        v2_tool(name="task", cmd=None, output='<task id="ses_child001" state="completed">done</task>', call="call_task", meta=None),
        v2_tool(name="websearch", cmd=None, output="", status="error", error={"type": "unknown", "message": "Web search cancelled"}, call="call_err"),
    ]), t=T0 + 5)
    add_v2(c, "ses_root0001", 4, "assistant", v2_assistant(None, error={"type": "provider.error", "message": "Invalid API key."}))
    add_v2(c, "ses_root0001", 5, "idle", {"outcome": "failed", "time": {"created": T0 + 20}})
    add_v2(c, "ses_root0001", 6, "mystery-new-type", {"anything": [1, 2, 3], "api_key": "sk-live-should-be-hidden"})
    add_v2(c, "ses_root0001", 7, "system", {"text": "instructions changed", "description": "Instr", "time": {"created": T0 + 30}})
    add_v2(c, "ses_child001", 1, "user", {"text": "explore it", "time": {"created": T0 + 1000}})
    add_v2(c, "ses_child001", 2, "assistant", v2_assistant("explored"))
    add_v2(c, "ses_child002", 1, "user", {"text": "audit", "time": {"created": T0 + 2000}})
    add_v2(c, "ses_grand001", 1, "user", {"text": "inspect dependency left-pad", "time": {"created": T0 + 3000}})
    add_v2(c, "ses_global01", 1, "user", {"text": "hello global", "time": {"created": T0 + 5000}})
    c.close()
    return path
