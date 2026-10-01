import hashlib
import json
import sqlite3

import pytest

import fixtures as F
from conftest import spec
from opencode_session_browser import normalize as N
from opencode_session_browser.adapters import ExportDirBackend, FilesBackend, SqliteBackend, UnsupportedStorage
from opencode_session_browser.source import Source
from opencode_session_browser.sqlite_access import SqliteAccess


def backend(root, tmp_path, force_snapshot=None):
    return SqliteBackend(SqliteAccess(str(root / "opencode.db"), tmp_path / "c", "t", force_snapshot))


def test_detects_v2_schema(std_root, tmp_path):
    d = backend(std_root, tmp_path).describe()
    assert d["format"] == "sqlite" and "v2 (session_v2 + session_message)" in d["session_models"]
    assert d["latest_migration"] == "20260101_fixture"


def test_detects_legacy_and_dual(tmp_path):
    c = F.new_db(tmp_path / "opencode.db", "legacy"); c.close()
    assert backend(tmp_path, tmp_path).describe()["session_models"] == ["legacy (session + message + part)"]
    c = F.new_db(tmp_path / "b.db", "both"); c.close()
    b = SqliteBackend(SqliteAccess(str(tmp_path / "b.db"), tmp_path / "c", "b"))
    assert len(b.describe()["session_models"]) == 2


def test_unsupported_schema_is_reported_not_fatal(tmp_path):
    c = sqlite3.connect(tmp_path / "opencode.db"); c.execute("CREATE TABLE unrelated(x)"); c.close()
    with pytest.raises(UnsupportedStorage):
        backend(tmp_path, tmp_path).list_sessions()
    s = Source(spec("x", tmp_path), tmp_path / "c"); s.refresh()
    assert s.status == "error" and "unsupported" in s.error


def test_session_metadata(std_root, tmp_path):
    ss = {s.id: s for s in backend(std_root, tmp_path).list_sessions()}
    assert len(ss) == 5
    r = ss["ses_root0001"]
    assert (r.provider, r.model) == ("opencode", "m1") and r.outcome == "succeeded" and r.project_name == "repo"
    assert r.message_count == 5 and r.event_count == 2        # idle + unknown-type records are not conversation
    assert r.title.startswith("Main review ✔ ünïcode 日本語")
    g = ss["ses_global01"]
    assert g.archived_at and g.directory == "C:\\gone\\away"   # archived + vanished directory are kept
    assert ss["ses_child002"].parent_id == "ses_root0001"
    assert ss["ses_root0001"].storage == "v2"


def test_missing_columns_and_null_fields(tmp_path):
    c = sqlite3.connect(tmp_path / "opencode.db")
    c.executescript("CREATE TABLE session (id text primary key, title text, time_created int, time_updated int);"
                    "INSERT INTO session VALUES ('ses_min', NULL, 5, 6);")
    c.close()
    s = backend(tmp_path, tmp_path).list_sessions()[0]
    assert s.id == "ses_min" and s.title == "" and s.created == 5000 and s.model is None and s.directory is None


def test_message_ordering_by_seq(std_root, tmp_path):
    msgs = backend(std_root, tmp_path).load_messages("ses_root0001")
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4, 5, 6, 7]
    assert msgs[1]["parts"][0]["text"].startswith("first reply") and msgs[2]["parts"][0]["text"].startswith("second")


def test_part_parsing_and_tools(std_root, tmp_path):
    msgs = backend(std_root, tmp_path).load_messages("ses_root0001")
    a = msgs[1]
    assert a["kind"] == "assistant" and a["model"] == "m1" and a["provider"] == "opencode" and a["variant"] == "high"
    assert a["tokens"] == {"input": 10, "output": 5, "reasoning": 2, "cache_read": 7, "cache_write": 1, "total": None}
    assert a["cost"] == 0.01 and a["finish"] == "stop" and a["duration_ms"] == 2500
    tools = [p["tool"] for p in a["parts"] if p["type"] == "tool"]
    sh = tools[0]
    assert sh["name"] == "shell" and sh["command"] == "grep -rn needle_in_haystack ." and sh["duration_ms"] == 250
    assert "needle_in_haystack" in sh["output"] and sh["status"] == "completed"
    assert tools[1]["child_session_id"] == "ses_child001"       # subagent link recovered from task result
    assert tools[2]["status"] == "error" and tools[2]["error"] == "unknown: Web search cancelled" or "cancelled" in tools[2]["error"]
    assert any(p["type"] == "reasoning" for p in a["parts"])


def test_assistant_error_and_events(std_root, tmp_path):
    msgs = backend(std_root, tmp_path).load_messages("ses_root0001")
    err = msgs[3]
    assert "Invalid API key" in err["error"] and any(p["type"] == "error" for p in err["parts"])
    idle = msgs[4]
    assert idle["kind"] == "event" and idle["error"]  # failed outcome surfaces as error indication


def test_unknown_type_preserved_and_secrets_redacted(std_root, tmp_path):
    m = backend(std_root, tmp_path).load_messages("ses_root0001")[5]
    assert m["kind"] == "unknown" and m["parts"][0]["type"] == "unknown" and m["original_type"] == "mystery-new-type"
    assert m["raw"]["anything"] == [1, 2, 3] and m["raw"]["api_key"] == "[redacted]"


def test_malformed_json_record(tmp_path):
    c = F.new_db(tmp_path / "opencode.db", "v2")
    F.add_session(c, "ses_bad")
    F.add_v2(c, "ses_bad", 1, "user", "{not json at all")
    F.add_v2(c, "ses_bad", 2, "assistant", '{"content": [{"type":"tool"}, 5, null, {"type":"text"}], "time": 3}')
    F.add_v2(c, "ses_bad", 3, "user", '{"text": "ok"}')
    c.close()
    msgs = backend(tmp_path, tmp_path).load_messages("ses_bad")
    assert len(msgs) == 3 and msgs[0]["parts"][-1]["original_type"] == "malformed-json"
    assert msgs[2]["parts"][0]["text"] == "ok"


def test_legacy_messages_and_parts(tmp_path):
    c = F.new_db(tmp_path / "opencode.db", "legacy")
    F.add_session(c, "ses_old", table="session")
    F.legacy_message(c, "ses_old", "msg_b", "assistant", F.T0 + 5, [
        {"type": "step-start"}, {"type": "reasoning", "text": "hm"},
        {"type": "tool", "tool": "bash", "callID": "c1", "state": {"status": "completed", "input": {"command": "echo hi"},
         "output": "hi", "metadata": {}, "time": {"start": F.T0, "end": F.T0 + 40}}},
        {"type": "step-finish", "reason": "stop", "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}}, "cost": 0.1},
        {"type": "new-part-kind", "x": 1}],
        modelID="m", providerID="p", error={"name": "APIError", "data": {"message": "boom", "statusCode": 500}})
    F.legacy_message(c, "ses_old", "msg_a", "user", F.T0 + 1, [{"type": "text", "text": "hi"}, "{broken"], model={"providerID": "p", "modelID": "m"})
    c.close()
    msgs = backend(tmp_path, tmp_path).load_messages("ses_old")
    assert [m["kind"] for m in msgs] == ["user", "assistant"]            # time order, not insertion order
    a = msgs[1]
    assert a["error"] == "APIError: HTTP 500: boom"
    types = [p["type"] for p in a["parts"]]
    assert "step" in types and "unknown" in types and "error" in types
    tool = next(p["tool"] for p in a["parts"] if p["type"] == "tool")
    assert tool["command"] == "echo hi" and tool["duration_ms"] == 40
    assert msgs[0]["provider"] == "p" and msgs[0]["parts"][1]["type"] == "unknown"
    assert backend(tmp_path, tmp_path).list_sessions()[0].storage == "legacy"


def test_dual_generation_prefers_v2_but_falls_back(tmp_path):
    c = F.new_db(tmp_path / "opencode.db", "both")
    F.add_session(c, "ses_both", table="session_v2"); F.add_session(c, "ses_both", table="session")
    F.add_v2(c, "ses_both", 1, "user", {"text": "from v2"})
    F.add_session(c, "ses_legonly", table="session")
    F.legacy_message(c, "ses_legonly", "m1", "user", F.T0, [{"type": "text", "text": "from legacy"}])
    c.close()
    b = backend(tmp_path, tmp_path)
    ss = {s.id: s for s in b.list_sessions()}
    assert ss["ses_both"].in_legacy_table is True and ss["ses_legonly"].storage == "legacy"
    assert b.load_messages("ses_both")[0]["parts"][0]["text"] == "from v2"
    assert b.load_messages("ses_legonly")[0]["parts"][0]["text"] == "from legacy"
    c = sqlite3.connect(tmp_path / "opencode.db")  # v2-only detection
    assert ss["ses_both"].in_legacy_table


def test_v2_only_flag(std_root, tmp_path):
    assert all(s.in_legacy_table is False for s in backend(std_root, tmp_path).list_sessions())


def test_files_adapter(tmp_path):
    st = tmp_path / "storage"
    (st / "session" / "p1").mkdir(parents=True); (st / "message" / "ses_f1").mkdir(parents=True); (st / "part" / "msg_1").mkdir(parents=True)
    (st / "project").mkdir()
    (st / "project" / "p1.json").write_text(json.dumps({"id": "p1", "worktree": "/w/proj"}))
    (st / "session" / "p1" / "ses_f1.json").write_text(json.dumps({"id": "ses_f1", "projectID": "p1", "title": "Файл ✓", "directory": "/w/proj",
                                                                    "time": {"created": 10, "updated": 20}, "parentID": "ses_p"}))
    (st / "session" / "p1" / "ses_bad.json").write_text("{truncated")
    (st / "message" / "ses_f1" / "msg_1.json").write_text(json.dumps({"id": "msg_1", "role": "user", "time": {"created": 11}}))
    (st / "message" / "ses_f1" / "msg_0.json").write_text(json.dumps({"id": "msg_0", "role": "assistant", "time": {"created": 10}, "modelID": "m"}))
    (st / "part" / "msg_1" / "prt_1.json").write_text(json.dumps({"type": "text", "text": "привет"}))
    b = FilesBackend(st)
    (s,) = b.list_sessions()
    assert s.title == "Файл ✓" and s.project_name == "proj" and s.parent_id == "ses_p" and s.message_count == 2 and s.storage == "files"
    msgs = b.load_messages("ses_f1")
    assert [m["id"] for m in msgs] == ["msg_0", "msg_1"] and msgs[1]["parts"][0]["text"] == "привет"


def test_export_adapter(tmp_path):
    exp = {"info": {"id": "ses_e1", "title": "Exported", "directory": "C:\\x\\y", "time": {"created": 1, "updated": 2}},
           "messages": [{"info": {"role": "user", "id": "m1", "time": {"created": 1}}, "parts": [{"type": "text", "text": "yo"}]}]}
    (tmp_path / "a.json").write_text("Exporting session: ses_e1\n" + json.dumps(exp))
    (tmp_path / "junk.json").write_text("[1,2,3]")
    b = ExportDirBackend(tmp_path)
    (s,) = b.list_sessions()
    assert s.id == "ses_e1" and s.project_name == "y"
    assert b.load_messages("ses_e1")[0]["parts"][0]["text"] == "yo"


def test_normalize_helpers():
    assert N.to_ms(1_700_000_000) == 1_700_000_000_000 and N.to_ms("x") is None and N.to_ms(None) is None
    assert N.norm_model("openai/gpt-x") == {"provider": "openai", "model": "gpt-x", "variant": None}
    assert N.error_summary({"name": "E", "data": {"message": "m", "statusCode": 4}}) == "E: HTTP 4: m"
    big = "A" * 10000
    assert "elided" in N.elide_blobs({"d": big})["d"] and N.elide_blobs({"d": "short"})["d"] == "short"
    assert N.redact({"headers": {"Authorization": "Bearer x", "ok": 1}})["headers"]["Authorization"] == "[redacted]"


def test_read_only_never_modifies_storage(std_root, tmp_path, make_registry):
    db = std_root / "opencode.db"
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns)
    reg = make_registry([spec("s", std_root)])
    reg.query({}); reg.query({"q": "needle"})
    for k in list(reg.sources["s"].sessions): reg.detail("s~" + k); reg.sources["s"].load_messages(k)
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns) == before
    assert not list(std_root.glob("*-wal")) and not list(std_root.glob("*-shm")) and not list(std_root.glob("*.part"))
    with backend(std_root, tmp_path).access.connection() as conn:   # even a deliberate write is refused
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM session_v2")
