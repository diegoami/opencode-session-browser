import hashlib
import http.server
import json
import shutil
import sqlite3
import threading
import urllib.request

import pytest

import fixtures as F
from conftest import spec
from opencode_session_browser import exporters
from opencode_session_browser.registry import Registry


def two_sources(make_registry, tmp_path, std_root):
    """Same session ids in two sources (e.g. Windows + WSL copies of one repo history)."""
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(std_root / "opencode.db", other / "opencode.db")
    c = sqlite3.connect(other / "opencode.db")
    c.execute("UPDATE session_v2 SET title='WSL copy of main' WHERE id='ses_root0001'")
    c.commit(); c.close()
    return make_registry([spec("windows", std_root, env="windows", label="Windows"),
                          spec("wsl-ubuntu", other, env="wsl", label="WSL: Ubuntu", distro="Ubuntu")])


def test_multiple_sources_and_duplicate_ids(make_registry, tmp_path, std_root):
    reg = two_sources(make_registry, tmp_path, std_root)
    q = reg.query({"limit": "500"})
    assert q["total"] == 10                                      # 5 + 5, nothing merged away
    keys = {r["key"] for r in q["rows"]}
    assert {"windows~ses_root0001", "wsl-ubuntu~ses_root0001"} <= keys
    titles = {r["key"]: r["title"] for r in q["rows"]}
    assert titles["wsl-ubuntu~ses_root0001"] == "WSL copy of main" and titles["windows~ses_root0001"].startswith("Main review")
    assert reg.query({"source": "wsl-ubuntu"})["total"] == 5
    assert reg.detail("windows~ses_root0001")["source_label"] == "Windows"
    assert reg.detail("wsl-ubuntu~ses_root0001")["title"] == "WSL copy of main"


def test_source_failure_isolation(make_registry, tmp_path, std_root):
    corrupt = tmp_path / "corrupt"; corrupt.mkdir(); (corrupt / "opencode.db").write_bytes(b"this is not sqlite" * 50)
    empty = tmp_path / "empty"; empty.mkdir()
    reg = make_registry([spec("good", std_root), spec("missing", tmp_path / "nope"), spec("corrupt", corrupt), spec("empty", empty)])
    st = {s["id"]: s for s in reg.diagnostics()["sources"]}
    assert st["good"]["status"] == "ok" and st["good"]["session_count"] == 5
    assert st["missing"]["status"] == "unavailable" and "not found" in st["missing"]["error"]
    assert st["corrupt"]["status"] == "error" and st["corrupt"]["error"]
    assert st["empty"]["status"] == "unsupported"
    assert reg.query({})["total"] == 5                           # healthy source unaffected


def test_hierarchy_nesting_and_navigation(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    d = reg.detail("s~ses_grand001")
    assert [l["id"] for l in d["lineage"]] == ["ses_root0001", "ses_child002"]
    assert d["root_key"] == "s~ses_root0001"
    t = d["tree"]
    assert t["id"] == "ses_root0001" and [c["id"] for c in t["children"]] == ["ses_child001", "ses_child002"]
    assert t["children"][1]["children"][0]["id"] == "ses_grand001"
    assert reg.sources["s"].sessions["ses_root0001"].child_count == 2
    assert [c["id"] for c in reg.detail("s~ses_root0001")["children"]] == ["ses_child001", "ses_child002"]
    assert reg.query({"kind": "child"})["total"] == 3 and reg.query({"kind": "root"})["total"] == 2


def test_hierarchy_cycles_and_orphans(make_registry, tmp_path):
    root = tmp_path / "o"; root.mkdir()
    c = F.new_db(root / "opencode.db")
    F.add_session(c, "ses_a", parent="ses_b"); F.add_session(c, "ses_b", parent="ses_a"); F.add_session(c, "ses_orphan", parent="ses_gone")
    c.close()
    reg = make_registry([spec("s", root)])
    assert reg.detail("s~ses_a")["tree"]                         # terminates
    d = reg.detail("s~ses_orphan")
    assert d["lineage"][0]["missing"] and d["lineage"][0]["id"] == "ses_gone"


def test_search_filters_and_snippets(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    ids = lambda **p: {r["id"] for r in reg.query(p)["rows"]}
    assert ids(q="needle_in_haystack") == {"ses_root0001"}                    # tool command + output
    assert ids(q="needle_in_haystack", **{"in": "output"}) == {"ses_root0001"}
    assert ids(q="needle_in_haystack", **{"in": "user"}) == set()
    assert ids(q="left-pad") == {"ses_grand001"}                                # user message
    assert ids(q="ses_child001") == {"ses_child001", "ses_root0001"}      # session id (+ the parent that mentions it in a task result)
    assert ids(q="ses_grand001") == {"ses_grand001"}
    assert ids(q="C:\\gone") == {"ses_global01"}                                # path
    assert ids(q="Security") == {"ses_child002"}                                # title, case-insensitive
    assert ids(q="invalid api key") == {"ses_root0001"}                         # error text, words ANDed
    assert ids(q="cancelled", **{"in": "error"}) == {"ses_root0001"}
    assert ids(q='"first reply"') == {"ses_root0001"} and ids(q='"reply first"') == set()
    assert ids(q="websearch") == {"ses_root0001"}                               # tool name
    row = reg.query({"q": "needle_in_haystack"})["rows"][0]
    assert row["matches"] and "needle_in_haystack" in row["matches"][0]["snippet"].lower()
    assert ids(errors="1") == {"ses_root0001", "ses_child002"}                  # message errors + failed outcome
    assert ids(project="repo") >= {"ses_root0001"} and "ses_global01" not in ids(project="repo")
    assert ids(archived="only") == {"ses_global01"} and "ses_global01" not in ids(archived="exclude")
    assert ids(since=str(F.T0 + 35_000), field="updated") == {"ses_grand001", "ses_root0001"}
    assert ids(model="opencode/m1") >= {"ses_root0001"}
    assert ids(outcome="failed") == {"ses_child002"}
    assert reg.query({"sort": "title", "order": "asc"})["rows"][0]["title"] == "Explore repository"


def test_unicode_search_and_display(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    assert {r["id"] for r in reg.query({"q": "ÜNÏCODE"})["rows"]} == {"ses_root0001"}     # case-folded non-ASCII
    assert {r["id"] for r in reg.query({"q": "日本語"})["rows"]} == {"ses_root0001"}
    d = reg.detail("s~ses_root0001")
    md = exporters.export_markdown(d, reg.sources["s"].load_messages("ses_root0001"))
    assert "日本語" in md and "ünïcode" in md
    js = json.loads(exporters.export_json(d, reg.sources["s"].load_messages("ses_root0001")))
    assert js["session"]["title"].endswith("日本語") and len(js["messages"]) == 7


def test_default_order_newest_first(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    ups = [r["updated"] for r in reg.query({})["rows"]]
    assert ups == sorted(ups, reverse=True)


def test_credentials_never_exposed(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    blob = json.dumps(reg.diagnostics(), default=str) + json.dumps(reg.query({"limit": "500"}), default=str)
    for sid in reg.sources["s"].sessions:
        blob += json.dumps(reg.sources["s"].load_messages(sid), default=str) + json.dumps(reg.detail("s~" + sid), default=str)
    assert "SUPER-SECRET-TOKEN-VALUE" not in blob and "sk-live-should-be-hidden" not in blob


def test_snapshot_mode_sees_wal_content_and_updates(make_registry, tmp_path):
    root = tmp_path / "wal"; root.mkdir()
    c = F.new_db(root / "opencode.db", "v2", wal=True)           # writer stays open: data lives only in the WAL
    F.add_session(c, "ses_w1", title="in wal")
    F.add_v2(c, "ses_w1", 1, "user", {"text": "wal message"})
    assert (root / "opencode.db-wal").stat().st_size > 0
    reg = make_registry([spec("w", root, force_snapshot=True)])
    src = reg.sources["w"]
    assert src.access_mode() == "snapshot" and src.sessions["ses_w1"].title == "in wal"
    before = hashlib.sha256((root / "opencode.db").read_bytes()).hexdigest()
    F.add_session(c, "ses_w2", title="added later"); F.add_v2(c, "ses_w2", 1, "user", {"text": "later"})
    reg.refresh_all(); assert "ses_w2" in src.sessions and src.load_messages("ses_w2")[0]["parts"][0]["text"] == "later"
    assert hashlib.sha256((root / "opencode.db").read_bytes()).hexdigest() == before   # source main file untouched
    c.close()


def test_live_refresh_detects_new_sessions_and_messages(make_registry, tmp_path):
    root = tmp_path / "live"; root.mkdir()
    c = F.new_db(root / "opencode.db", "v2", wal=True)
    F.add_session(c, "ses_l1"); F.add_v2(c, "ses_l1", 1, "user", {"text": "one"})
    reg = make_registry([spec("l", root)])
    v0, fp0 = reg.version, reg.sources["l"].message_fingerprint("ses_l1")
    F.add_v2(c, "ses_l1", 2, "assistant", F.v2_assistant("two")); F.add_session(c, "ses_l2")
    reg.refresh_all(); reg.index_once()
    assert reg.version > v0 and "ses_l2" in reg.sources["l"].sessions
    assert reg.sources["l"].message_fingerprint("ses_l1") != fp0 and len(reg.sources["l"].load_messages("ses_l1")) == 2
    assert reg.query({"q": "two"})["total"] == 1
    c.close()


# ---------------- live status ----------------
def test_status_unknown_by_default_and_authoritative_when_configured(make_registry, std_root):
    reg = make_registry([spec("s", std_root)])
    assert reg.detail("s~ses_root0001")["status"] == "unknown"      # recent mtime never implies "running"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ses_root0001": {"type": "busy"}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    reg2 = make_registry([spec("s", std_root, status_url=f"http://127.0.0.1:{srv.server_port}")])
    assert reg2.detail("s~ses_root0001")["status"] == "busy"
    assert reg2.detail("s~ses_child001")["status"] == "unknown"
    assert [r["id"] for r in reg2.query({"status": "busy"})["rows"]] == ["ses_root0001"]
    srv.shutdown()
    from opencode_session_browser import status
    status._CACHE.clear()
    assert reg2.detail("s~ses_root0001")["status"] == "unknown"      # unreachable server -> unknown, not a guess


# ---------------- HTTP API ----------------
@pytest.fixture
def api(make_registry, std_root):
    from opencode_session_browser.server import serve
    reg = make_registry([spec("s", std_root, label="Fixture")])
    httpd = serve(reg, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path, headers=None, raw=False):
        r = urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}))
        body = r.read()
        return (r.status, r.headers, body) if raw else json.loads(body)
    yield get, base, reg
    httpd.shutdown(); httpd.server_close()


def test_http_api(api):
    get, base, reg = api
    assert get("/api/sessions?q=left-pad")["total"] == 1
    d = get("/api/sessions/s~ses_root0001")
    assert d["children"] and d["resume"]
    m = get("/api/sessions/s~ses_root0001/messages")
    assert m["count"] == 7 and all("raw" not in x for x in m["messages"])
    raw = get("/api/sessions/s~ses_root0001/messages/" + m["messages"][5]["id"] + "?full=1")
    assert raw["raw"]["anything"] == [1, 2, 3]
    st, hdr, body = get("/api/sessions/s~ses_root0001/export.md", raw=True)
    assert "attachment" in hdr["Content-Disposition"] and "日本語" in body.decode()
    assert json.loads(get("/api/sessions/s~ses_root0001/export.json", raw=True)[2])["messages"]
    assert get("/api/sources")["sources"][0]["status"] == "ok"
    assert b"OpenCode Session Browser" in get("/", raw=True)[2]
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/api/sessions/s~nope")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/../../etc/passwd")
    assert e.value.code in (400, 404)


def test_http_rejects_foreign_host_and_origin(api):
    get, base, reg = api
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/api/sessions", headers={"Host": "evil.example.com"})
    assert e.value.code == 403
    req = urllib.request.Request(base + "/api/refresh", method="POST", headers={"Origin": "http://evil.example.com"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    ok = urllib.request.urlopen(urllib.request.Request(base + "/api/refresh", method="POST"))
    assert json.loads(ok.read())["ok"]


def test_large_outputs_are_clipped_but_fetchable(make_registry, tmp_path):
    root = tmp_path / "big"; root.mkdir()
    c = F.new_db(root / "opencode.db"); F.add_session(c, "ses_big")
    F.add_v2(c, "ses_big", 1, "assistant", F.v2_assistant("x", tools=[F.v2_tool(output="Z" * 200_000)]))
    c.close()
    reg = make_registry([spec("b", root)])
    from opencode_session_browser.exporters import light_entry
    e = reg.sources["b"].load_messages("ses_big")[0]
    t = light_entry(e, 30000)["parts"][1]["tool"]
    assert len(t["output"]) == 30000 and t["output_truncated"] == 200_000
    assert len(e["parts"][1]["tool"]["output"]) == 200_000


def test_second_instance_gets_private_snapshot_dir(tmp_path):
    from opencode_session_browser.sqlite_access import SqliteAccess
    root = tmp_path / "r"; root.mkdir()
    F.new_db(root / "opencode.db").close()
    a = SqliteAccess(str(root / "opencode.db"), tmp_path / "cache", "s", True)
    b = SqliteAccess(str(root / "opencode.db"), tmp_path / "cache", "s", True)
    a.sync(); b.sync()
    assert a.snap_dir != b.snap_dir
    with a.connection() as ca, b.connection() as cb:
        assert ca.execute("select count(*) from project").fetchone()[0] == cb.execute("select count(*) from project").fetchone()[0] == 2
